"""测试夹具：每个用例一套独立的 SQLite 库。

要点是**每例换一个库文件**并重置引擎单例。db.py 里的引擎是模块级单例，
如果测试之间共用，前一个用例写进去的预约会在后一个用例里变成「冲突」，
于是测试时灵时不灵 —— 而且这种失败看起来像业务 bug。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture
async def isolated_db(tmp_path, monkeypatch):
    """干净的库 + 种子数据。"""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("LAB_DATABASE_URL", url)
    monkeypatch.setenv("LAB_APP_MODE", "mock")

    from lagent import db as db_module
    from lagent.config import reset_settings_cache

    reset_settings_cache()
    await db_module.dispose_engine()
    await db_module.init_db()

    from lagent.seed import seed

    await seed(force=True)

    yield db_module

    await db_module.dispose_engine()
    reset_settings_cache()


@pytest.fixture
async def catalog(isolated_db):
    """设备目录 (名称, 类别)，供 Mock 模型识别设备。"""
    from lagent.agent.tools import load_catalog

    return await load_catalog()


@pytest.fixture
def mock_client(catalog):
    from lagent.agent.llm import MockLLMClient

    return MockLLMClient(catalog)


@pytest.fixture
def agent(mock_client):
    from lagent.agent.graph import LabBookingAgent
    from lagent.agent.state import SessionStore

    return LabBookingAgent(mock_client, store=SessionStore())
