"""并发与唯一性：整套「并发安全」说法必须由这些断言撑着。

如果这里全绿，才敢在简历上写「并发安全」；如果只做了功能测试，
那写的其实是「我希望能并发安全」。
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from lagent.clock import now_local
from lagent.db import session_scope
from lagent.domain.booking import cancel_reservation, create_reservation, list_reservations
from lagent.models import Reservation
from lagent.schemas import ReservationOut

# 紫外可见分光光度计：不需要资质、单次 4 小时、分析楼 301（工作日 08:00-22:00）
EQUIPMENT_ID = 2
START = dt.time(10, 0)
END = dt.time(11, 0)


def target_day() -> dt.date:
    """用固定的远期日期，避免与种子数据（明天）或评测残留相互干扰。"""
    return now_local().date() + dt.timedelta(days=30)


async def _book(user_id: int, start=START, end=END, day=None):
    return await create_reservation(
        user_id=user_id,
        equipment_id=EQUIPMENT_ID,
        date_=day or target_day(),
        start=start,
        end=end,
        purpose="测试",
    )


class TestConcurrentBooking:
    async def test_only_one_wins_for_same_slot(self, isolated_db):
        """★ 核心断言：N 个并发抢同一时段，恰好 1 个成功。"""
        results = await asyncio.gather(*[_book(2) for _ in range(30)])
        ok = [r for r in results if r.ok]
        assert len(ok) == 1, f"期望恰好 1 个成功，实际 {len(ok)}"

        losers = [r for r in results if not r.ok]
        # 其余都应当被明确告知「被别人占了」，而不是模糊报错
        assert all(r.conflict_with is not None or "占用" in r.message for r in losers)

    async def test_only_one_row_persisted(self, isolated_db):
        await asyncio.gather(*[_book(2) for _ in range(30)])
        async with session_scope() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(Reservation)
                    .where(
                        Reservation.equipment_id == EQUIPMENT_ID,
                        Reservation.date == target_day(),
                    )
                )
            ).scalar()
        assert count == 1, "并发下写了多行，唯一索引没生效"

    async def test_distinct_slots_all_succeed(self, isolated_db):
        """不同时段之间不该互相误伤。"""
        slots = [(dt.time(9, 0), dt.time(10, 0)), (dt.time(11, 0), dt.time(12, 0)),
                 (dt.time(13, 0), dt.time(14, 0)), (dt.time(15, 0), dt.time(16, 0))]
        results = await asyncio.gather(
            *[_book(2, start=s, end=e) for s, e in slots for _ in range(5)]
        )
        ok = [r for r in results if r.ok]
        assert len(ok) == len(slots)

    async def test_retry_count_is_reported(self, isolated_db):
        """重试次数要如实暴露：它是「唯一索引真的拦下了并发写入」的旁证。"""
        results = await asyncio.gather(*[_book(2) for _ in range(30)])
        assert all(r.retries >= 0 for r in results)
        assert max(r.retries for r in results) <= 5

    async def test_sequential_duplicate_is_rejected(self, isolated_db):
        first = await _book(2)
        assert first.ok
        second = await _book(2)
        assert not second.ok

    async def test_cancelled_slot_can_be_rebooked(self, isolated_db):
        """部分唯一索引的意义：取消之后这个时段必须能重新订回来。

        如果索引没带 status 条件（普通唯一约束），取消掉的记录会永远占着坑，
        用户会看到「明明没人用却约不上」——这是最容易在设计期忽略、上线后
        被投诉的那类问题。
        """
        first = await _book(2)
        assert first.ok

        cancelled = await cancel_reservation(
            reservation_id=first.reservation.id, user_id=2, reason="测试释放"
        )
        assert cancelled.ok

        again = await _book(2)
        assert again.ok, "取消后时段没有释放，部分唯一索引可能写错了"


class TestDatabaseInvariant:
    async def test_unique_index_rejects_active_duplicate(self, isolated_db):
        """绕过应用层直接插两行，数据库也必须拒绝。"""
        with pytest.raises(IntegrityError):
            async with session_scope() as session:
                for _ in range(2):
                    session.add(
                        Reservation(
                            user_id=2,
                            equipment_id=EQUIPMENT_ID,
                            date=target_day(),
                            start_time=START,
                            end_time=END,
                            status="confirmed",
                            purpose="直接插入",
                        )
                    )
                await session.flush()

    async def test_index_allows_same_slot_on_other_day(self, isolated_db):
        day = target_day()
        assert (await _book(2, day=day)).ok
        assert (await _book(2, day=day + dt.timedelta(days=1))).ok


class TestWriteValidation:
    """写前校验：不信任调用方（尤其是模型）给的参数。"""

    async def test_exceeds_max_hours(self, isolated_db):
        outcome = await _book(2, start=dt.time(9, 0), end=dt.time(15, 0))
        assert not outcome.ok
        assert "最长" in outcome.message

    async def test_training_required_but_missing(self, isolated_db):
        # 高速离心机：需要「离心」资质；张伟只有「光谱」
        outcome = await create_reservation(
            user_id=1,
            equipment_id=6,
            date_=target_day(),
            start=dt.time(10, 0),
            end=dt.time(11, 0),
        )
        assert not outcome.ok
        assert "资质" in outcome.message

    async def test_training_present_passes(self, isolated_db):
        outcome = await create_reservation(
            user_id=2,  # 李娜资质齐全
            equipment_id=6,
            date_=target_day(),
            start=dt.time(10, 0),
            end=dt.time(11, 0),
        )
        assert outcome.ok

    async def test_end_before_start(self, isolated_db):
        outcome = await _book(2, start=dt.time(15, 0), end=dt.time(9, 0))
        assert not outcome.ok
        assert "晚于开始时间" in outcome.message

    async def test_outside_open_hours(self, isolated_db):
        # 分析楼 301 工作日 08:00-22:00，卡在 07:00
        outcome = await _book(2, start=dt.time(7, 0), end=dt.time(8, 0))
        assert not outcome.ok
        assert "开放时间" in outcome.message

    async def test_past_time_rejected(self, isolated_db):
        outcome = await _book(2, day=now_local().date() - dt.timedelta(days=1))
        assert not outcome.ok
        assert "过去" in outcome.message

    async def test_unknown_equipment(self, isolated_db):
        outcome = await create_reservation(
            user_id=2, equipment_id=9999, date_=target_day(), start=START, end=END
        )
        assert not outcome.ok
        assert "不存在" in outcome.message

    async def test_maintenance_equipment_rejected(self, isolated_db):
        async with session_scope() as session:
            from lagent.models import Equipment

            item = cast(Equipment, await session.get(Equipment, EQUIPMENT_ID))
            item.status = "maintenance"
        outcome = await _book(2)
        assert not outcome.ok
        assert "maintenance" in outcome.message


class TestCancel:
    async def test_cancel_own_slot(self, isolated_db):
        first = await _book(2)
        outcome = await cancel_reservation(reservation_id=first.reservation.id, user_id=2)
        assert outcome.ok
        assert cast(ReservationOut, outcome.reservation).status == "cancelled"

    async def test_cannot_cancel_others(self, isolated_db):
        first = await _book(2)
        outcome = await cancel_reservation(reservation_id=first.reservation.id, user_id=1)
        assert not outcome.ok
        assert "自己" in outcome.message

    async def test_cancel_twice_is_rejected(self, isolated_db):
        first = await _book(2)
        assert (await cancel_reservation(reservation_id=first.reservation.id, user_id=2)).ok
        second = await cancel_reservation(reservation_id=first.reservation.id, user_id=2)
        assert not second.ok
        assert "无需取消" in second.message

    async def test_cancel_unknown_id(self, isolated_db):
        outcome = await cancel_reservation(reservation_id=999999, user_id=2)
        assert not outcome.ok

    async def test_version_advances(self, isolated_db):
        first = await _book(2)
        outcome = await cancel_reservation(reservation_id=first.reservation.id, user_id=2)
        assert outcome.reservation is not None
        async with session_scope() as session:
            row = cast(Reservation, await session.get(Reservation, first.reservation.id))
            assert row.version == 2

    async def test_concurrent_cancel_only_one_succeeds(self, isolated_db):
        """乐观锁：并发取消同一个预约，只能有一个成功。"""
        first = await _book(2)
        results = await asyncio.gather(
            *[
                cancel_reservation(reservation_id=first.reservation.id, user_id=2)
                for _ in range(10)
            ]
        )
        assert len([r for r in results if r.ok]) == 1


class TestListing:
    async def test_list_by_user(self, isolated_db):
        await _book(2)
        async with session_scope() as session:
            rows = await list_reservations(session, user_id=2)
        assert rows
        assert all(r.equipment_name for r in rows), "设备名应被预加载，不能留空"

    async def test_list_by_date(self, isolated_db):
        await _book(2)
        async with session_scope() as session:
            rows = await list_reservations(session, date_=target_day())
        assert len(rows) == 1
