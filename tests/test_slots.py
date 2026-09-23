"""占用格（reservation_slots）：区间不重叠不变式的落地验证。

这一组测试对应本项目的核心教训 —— 唯一索引建在
``(equipment_id, date, start_time)`` 上**保护不了区间不重叠**：

    * 同开始时间 → 拦得住
    * 部分重叠（13:00-15:00 vs 14:00-16:00）→ 拦不住，实测双双成功
    * 包含关系（15:00-17:00 vs 15:30-16:00）→ 拦不住

现在改成把区间拆成 30 分钟的格、唯一索引打 ``(equipment_id, date, slot_index)``，
任何相交都必然撞索引。下面把三种相交形态**逐一钉死**，另外钉住两个
最容易写反的边界：**首尾相接不算相交**、**取消要释放格**。
"""

from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy import func, select

from lagent.agent.tools import tool_create_reservation
from lagent.clock import now_local
from lagent.db import session_scope
from lagent.domain.booking import cancel_reservation, granularity_minutes
from lagent.models import ACTIVE_STATUSES, Reservation, ReservationSlot

# 用一周后的日期，远离种子数据（种子占用的是"明天"下午）
DAY = now_local().date() + dt.timedelta(days=7)
SPECTRO = 1  # 荧光光谱仪，max_hours=4，李娜（用户 2）有资质
USER = 2


async def book(start: dt.time, end: dt.time, *, equipment_id: int = SPECTRO, user_id: int = USER):
    return await tool_create_reservation(
        user_id=user_id, equipment_id=equipment_id, date_=DAY,
        start=start, end=end, purpose="占用格测试",
    )


async def active_count(equipment_id: int = SPECTRO) -> int:
    async with session_scope() as session:
        return (
            await session.execute(
                select(func.count()).select_from(Reservation).where(
                    Reservation.equipment_id == equipment_id,
                    Reservation.date == DAY,
                    Reservation.status.in_(ACTIVE_STATUSES),
                )
            )
        ).scalar() or 0


async def slot_rows(equipment_id: int = SPECTRO) -> list[int]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ReservationSlot)
                .where(ReservationSlot.equipment_id == equipment_id,
                       ReservationSlot.date == DAY)
                .order_by(ReservationSlot.slot_index)
            )
        ).scalars().all()
        return [r.slot_index for r in rows]


class TestIntervalsDoNotOverlap:
    """三种相交形态都必须被拦下。"""

    async def test_identical_interval_conflicts(self, isolated_db):
        assert (await book(dt.time(9, 0), dt.time(11, 0))).ok
        second = await book(dt.time(9, 0), dt.time(11, 0))
        assert not second.ok
        assert await active_count() == 1

    async def test_partial_overlap_conflicts(self, isolated_db):
        """★ 曾经漏掉的那一种：开始时间不同，索引看不见。"""
        assert (await book(dt.time(13, 0), dt.time(15, 0))).ok
        second = await book(dt.time(14, 0), dt.time(16, 0))
        assert not second.ok, "部分重叠必须被拒绝"
        assert second.conflict_with is not None
        assert await active_count() == 1

    async def test_containment_conflicts(self, isolated_db):
        """★ 大区间包住小区间，同样必须被拒（两个方向都要）。"""
        assert (await book(dt.time(9, 0), dt.time(13, 0))).ok
        assert not (await book(dt.time(10, 0), dt.time(11, 0))).ok, "被包含的请求应被拒"
        assert await active_count() == 1

    async def test_containing_request_is_rejected_too(self, isolated_db):
        """反方向：先占小区间，再来一个包住它的长请求。"""
        assert (await book(dt.time(9, 0), dt.time(10, 0))).ok
        assert not (await book(dt.time(8, 30), dt.time(11, 0))).ok, "包含它的请求应被拒"
        assert await active_count() == 1

    async def test_adjacent_intervals_are_fine(self, isolated_db):
        """★ 首尾相接不算相交 —— 半开区间 [start, end) 的边界。

        14:00-16:00 与 16:00-18:00 是同一天的连续两段，必须**都能约上**。
        这里如果写成闭区间，第二个会被误拒；如果用 <= 判断重叠就会退化成
        「整天只能约一段」。
        """
        assert (await book(dt.time(14, 0), dt.time(16, 0))).ok
        second = await book(dt.time(16, 0), dt.time(18, 0))
        assert second.ok, f"首尾相接被误判为冲突：{second.message}"
        assert await active_count() == 2

    async def test_concurrent_overlapping_only_one_wins(self, isolated_db):
        """并发撞区间：恰好 1 个成功（这是压测要证的同一件事）。"""
        results = await asyncio.gather(
            *[book(dt.time(13, 0), dt.time(15, 0)) for _ in range(10)],
            *[book(dt.time(14, 0), dt.time(16, 0)) for _ in range(10)],
        )
        winners = [r for r in results if r.ok]
        assert len(winners) == 1, f"应当恰好 1 个成功，实际 {len(winners)}"
        assert await active_count() == 1


class TestSlotBookkeeping:
    """占用格与预约的对应关系要准确 —— 多占漏占都会出问题。"""

    async def test_slot_rows_match_interval(self, isolated_db):
        # 2 小时 / 30 分钟 = 4 格；13:00 起 → 26,27,28,29
        await book(dt.time(13, 0), dt.time(15, 0))
        assert await slot_rows() == [26, 27, 28, 29]

    async def test_half_hour_takes_one_slot(self, isolated_db):
        await book(dt.time(16, 0), dt.time(16, 30))
        assert await slot_rows() == [32]

    async def test_cancel_releases_slots(self, isolated_db):
        """★ 取消不释放格 = 这个时段永远订不回来。"""
        first = await book(dt.time(9, 0), dt.time(11, 0))
        assert first.ok
        assert await slot_rows() == [18, 19, 20, 21]

        cancelled = await cancel_reservation(
            reservation_id=first.reservation.id, user_id=USER, reason="测试"
        )
        assert cancelled.ok
        assert await slot_rows() == [], "取消后占用格必须释放"

        again = await book(dt.time(9, 0), dt.time(11, 0))
        assert again.ok, f"取消后应能重新预约：{again.message}"

    async def test_failed_booking_leaves_no_slot_rows(self, isolated_db):
        """撞车失败的事务必须完全回滚，不能留下半个占用格。"""
        assert (await book(dt.time(9, 0), dt.time(11, 0))).ok
        assert not (await book(dt.time(10, 0), dt.time(12, 0))).ok
        assert await slot_rows() == [18, 19, 20, 21]


class TestGranularity:
    """粒度对齐：不对齐会让占用格算漏，等于漏保护。"""

    async def test_granularity_is_30(self):
        assert granularity_minutes() == 30

    async def test_misaligned_start_is_rejected(self, isolated_db):
        outcome = await book(dt.time(9, 15), dt.time(10, 0))
        assert not outcome.ok
        assert "整数倍" in outcome.message

    async def test_misaligned_end_is_rejected(self, isolated_db):
        outcome = await book(dt.time(9, 0), dt.time(10, 20))
        assert not outcome.ok
        assert "整数倍" in outcome.message

    async def test_misaligned_leaves_nothing_behind(self, isolated_db):
        await book(dt.time(9, 15), dt.time(10, 0))
        assert await active_count() == 0
        assert await slot_rows() == []


class TestSeedRegistersSlots:
    """演示数据也必须登记占用格，否则它自己就是绕过不变式的后门。"""

    async def test_seed_demo_reservations_have_slots(self, isolated_db):
        tomorrow = now_local().date() + dt.timedelta(days=1)
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(ReservationSlot).where(ReservationSlot.date == tomorrow)
                )
            ).scalars().all()
        # 种子里两条演示预约：14:00-16:00（4 格）+ 16:30-18:00（3 格）
        assert len(rows) == 7

    async def test_cannot_book_over_seeded_demo_slot(self, isolated_db):
        """种子占用的时段，新请求必须撞上 —— 证明演示数据真的进了判定。"""
        tomorrow = now_local().date() + dt.timedelta(days=1)
        outcome = await tool_create_reservation(
            user_id=USER, equipment_id=SPECTRO, date_=tomorrow,
            start=dt.time(14, 30), end=dt.time(15, 0), purpose="撞种子",
        )
        assert not outcome.ok
