"""eval / loadtest 的数据库隔离。

这两个命令是项目的「证据」：一个量准确率，一个量并发安全。
证据必须可复现 —— 如果它们跑在开发库上，就会被之前的操作污染：
手工点一单，评测所需的时段就被占了，通过率莫名其妙掉一格，
看起来像代码回归，其实只是脏数据。实测踩过这个坑，所以单独立测。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select

from lagent.clock import now_local
from lagent.config import get_settings, reset_settings_cache
from lagent.db import (
    dispose_engine,
    init_db,
    isolated_database,
    session_scope,
)
from lagent.models import Reservation
from lagent.seed import seed


async def _count_reservations() -> int:
    """数当前配置指向的那个库里有几条预约。"""
    async with session_scope() as session:
        return (await session.execute(select(func.count()).select_from(Reservation))).scalar() or 0


@pytest.fixture
async def dev_db(tmp_path, monkeypatch):
    """模拟一个「开发库」，并把它固定为当前配置指向的库。"""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'dev.db').as_posix()}"
    monkeypatch.setenv("LAB_DATABASE_URL", url)
    monkeypatch.setenv("LAB_APP_MODE", "mock")
    reset_settings_cache()
    await dispose_engine()
    await init_db()
    await seed(force=True)
    yield url
    await dispose_engine()
    reset_settings_cache()


class TestIsolatedDatabase:
    async def test_switches_to_target_url(self, dev_db, tmp_path):
        other = f"sqlite+aiosqlite:///{(tmp_path / 'other.db').as_posix()}"
        async with isolated_database(other):
            assert get_settings().database_url == other

    async def test_restores_previous_url(self, dev_db):
        async with isolated_database("sqlite+aiosqlite:///./whatever.db"):
            pass
        assert get_settings().database_url == dev_db

    async def test_restores_url_on_exception(self, dev_db):
        with pytest.raises(RuntimeError):
            async with isolated_database("sqlite+aiosqlite:///./whatever.db"):
                raise RuntimeError("模拟中途失败")
        assert get_settings().database_url == dev_db

    async def test_writes_in_scratch_do_not_leak(self, dev_db, tmp_path):
        """沙箱库里的写入绝不能出现在开发库里。"""
        from lagent.agent.tools import tool_create_reservation

        baseline = await _count_reservations()

        scratch = f"sqlite+aiosqlite:///{(tmp_path / 'scratch.db').as_posix()}"
        async with isolated_database(scratch):
            await init_db()
            await seed(force=True)
            before = await _count_reservations()
            outcome = await tool_create_reservation(
                user_id=2,
                equipment_id=1,
                date_=now_local().date() + dt.timedelta(days=3),
                start=dt.time(9, 0),
                end=dt.time(10, 0),
                purpose="隔离探针",
            )
            assert outcome.ok
            assert await _count_reservations() == before + 1, "沙箱库里的写入没生效"

        assert await _count_reservations() == baseline, "沙箱库的写入泄漏到开发库了"
