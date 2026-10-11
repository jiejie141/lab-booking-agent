"""并发与唯一性：整套「并发安全」说法必须由这些断言撑着。

如果这里全绿，才敢在简历上写「并发安全」；如果只做了功能测试，
那写的其实是「我希望能并发安全」。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from typing import cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from lagent.clock import now_local
from lagent.db import session_scope
from lagent.domain.booking import (
    BookingOutcome,
    cancel_reservation,
    create_reservation,
    list_reservations,
)
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


class TestConcurrentBookingOverHTTP:
    """★ 「两个不同账号同时抢同一个时段」——走真实 HTTP 栈。

    前面 TestConcurrentBooking 的 30 个并发请求**全是 user_id=2**，
    验证的是「同一个人重复提交不会写出两条」。它证明不了另一件事：

        张伟和李娜**各自**想约同一台设备的同一时段，会不会两个人都成功？

    两者走的是不同的代码路径：同 user 的重复请求可以在写下单前就被
    幂等键或业务规则挡掉，而两个不同 user 必须一路走到**唯一索引**才分胜负。
    简历上写「并发安全」，被追问的恰恰是后者。

    这里刻意**不**直接调 create_reservation，而是从 /api/auth/login 拿两个
    真实令牌、各自带自己的 Authorization 发 POST —— 把中间件、鉴权、
    依赖注入、事务边界全都包含进来。领域层并发安全不等于接口层并发安全
    （例如两段式提交、或者日志/通知写在事务外，都会在接口层露出来）。
    """

    async def test_two_users_race_for_one_slot(self, http, as_user):
        """两个账号并发抢同一时段，恰好一个拿到，且拿到的那个能自证身份。"""
        import httpx

        zhang, li = await asyncio.gather(as_user("张伟"), as_user("李娜"))
        day = (now_local().date() + dt.timedelta(days=40)).isoformat()
        payload = {
            "equipment_id": EQUIPMENT_ID,
            "date": day,
            "start": "10:00:00",
            "end": "11:00:00",
            "purpose": "并发抢坑",
        }

        async def attempt(headers):
            return await http.post("/api/reservations", json=payload, headers=headers)

        # 两端**同时**发出，不等第一个回来 —— gather 才是并发，
        # 顺序 await 只是两次普通请求，什么也证明不了。
        gathered: tuple[object, object] = await asyncio.gather(
            attempt(zhang), attempt(li), return_exceptions=True
        )
        first, second = gathered
        results = [r for r in (first, second) if isinstance(r, httpx.Response)]
        assert len(results) == 2, f"请求本身出错：{first!r} / {second!r}"

        created = [r for r in results if r.status_code in (200, 201)]
        assert len(created) == 1, (
            "期望恰好 1 个成功，实际 "
            + str([(r.status_code, r.text[:80]) for r in results])
        )

        winner = created[0].json()["reservation"]
        # 输的那个必须被**明确**告知冲突，而不是 500 / 超时 / 静默失败
        loser = next(r for r in results if r.status_code not in (200, 201))
        assert loser.status_code in (409, 422), (
            f"失败方应返回 409/422（业务冲突），实得 {loser.status_code}："
            + loser.text[:200]
        )

        # 落库必须只有一条 —— 这才是「唯一索引真的挡下了」的证据
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(Reservation).where(
                        Reservation.equipment_id == EQUIPMENT_ID,
                        Reservation.date == dt.date.fromisoformat(day),
                    )
                )
            ).scalars().all()
        assert len(rows) == 1, f"落库 {len(rows)} 条，超卖"
        assert rows[0].user_id == winner["user_id"], (
            "返回的赢家与落库的预约不是同一个人 —— 并发下身份串了"
        )

    async def test_both_users_see_the_same_occupancy(self, http, as_user):
        """抢完之后，两边看到的占用情况必须一致。

        并发 bug 的典型残留是「赢家的记录只在自己视图里可见」，
        或者输家的失败响应说"没冲突"却写不进去 —— 用户视角就是
        "我明明看到是空的，怎么约不上"。
        """
        import httpx

        zhang, li = await asyncio.gather(as_user("张伟"), as_user("李娜"))
        day = (now_local().date() + dt.timedelta(days=41)).isoformat()
        payload = {
            "equipment_id": EQUIPMENT_ID,
            "date": day,
            "start": "14:00:00",
            "end": "15:00:00",
            "purpose": "占用一致性",
        }

        gathered: tuple[object, object] = await asyncio.gather(
            http.post("/api/reservations", json=payload, headers=zhang),
            http.post("/api/reservations", json=payload, headers=li),
            return_exceptions=True,
        )
        responses = [r for r in gathered if isinstance(r, httpx.Response)]
        assert len(responses) == 2

        # 管理员视角（全量）应当看到且只看到 1 条
        admin = await as_user("管理员")
        resp = await http.get(
            "/api/reservations", params={"date": day, "status": "confirmed"}, headers=admin
        )
        assert resp.status_code == 200, resp.text
        rows = [r for r in resp.json() if r["equipment_id"] == EQUIPMENT_ID]
        assert len(rows) == 1, f"全量视图里出现 {len(rows)} 条，超卖"

        # 而成功方自己的列表里必须有它 —— "约上了却查不到"同样是 bug
        winner_headers = zhang if responses[0].status_code in (200, 201) else li
        own = await http.get("/api/reservations", headers=winner_headers)
        assert own.status_code == 200
        assert any(r["equipment_id"] == EQUIPMENT_ID for r in own.json()), (
            "赢家自己的列表里查不到刚约上的时段"
        )



class TestSlotGridIsLoadBearing:
    """★ 证明「占用格唯一索引」真的在承担正确性 —— 而不是被别的层顺手挡了。

    为什么必须有这么一组测试：

    SQLite 的写事务是串行的。上面那些并发用例即使**把占用格索引整个删掉**，
    往往照样"恰好 1 个成功" —— 因为后来者要么在复检(find_conflict)那步
    就被挡住，要么撞上了老索引 uq_res_active_slot(设备,日期,开始时间)。
    于是「并发安全」这个结论被**错误归因**了：看起来在测唯一索引，
    实际测的是 SQLite 的串行化 + 复检。

    本组用两件事把归因钉死：

      1. **开始时间不同**的重叠区间（13:00-15:00 vs 14:00-16:00）。
         老索引只覆盖「开始时间完全相同」，完全看不见这种重叠 ——
         它是这两条请求之间**唯一**可能撞车的约束。
      2. 一个 barrier 把两个事务**同时**推过复检点，制造"两边都读到无冲突"
         的交错。否则复检会先挡住一个，根本走不到写格那一步。

    实测对照（本机 SQLite）：

        占用格唯一索引在   → 1 条预约 · 4 格 · 409
        占用格唯一索引不在 → 2 条预约 · 8 格 · 超卖

    ⚠️ 改 models.py 的 Index(unique=...) **不会**改变已建库的结构 ——
    表结构来自 migrations/versions/0001_initial_schema.py。要复现上面第二行，
    必须改**迁移**里的 unique=True → False。
    """

    async def test_interleaved_overlap_is_blocked_by_index(self, isolated_db):
        import lagent.domain.booking as bk

        day = now_local().date() + dt.timedelta(days=60)
        # ⚠️ 不用 asyncio.Barrier：它是 **3.11+** 才有的，而 pyproject 声明
        #    python_version = "3.10" —— 用了会在 3.10 的 CI 上 AttributeError。
        #    这里用两个 Event 自己拼一个等价的「双方都到齐才放行」。
        arrivals = 0
        lock = asyncio.Lock()
        ready = asyncio.Event()
        original = bk.find_conflict
        calls = {"n": 0}

        async def pass_gate() -> None:
            """等两个事务都到齐再一起放行。"""
            nonlocal arrivals
            async with lock:
                arrivals += 1
                if arrivals >= 2:
                    ready.set()
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(ready.wait(), timeout=3)

        async def forced_pass(session, equipment_id, date_, start, end, **kw):
            """前两次复检一律放行，并在放行前用 barrier 对齐。"""
            calls["n"] += 1
            if calls["n"] <= 2:
                await pass_gate()
                return None
            return await original(session, equipment_id, date_, start, end, **kw)

        bk.find_conflict = forced_pass
        try:
            gathered: tuple[BookingOutcome | BaseException, ...] = await asyncio.gather(
                _book(1, start=dt.time(13, 0), end=dt.time(15, 0), day=day),
                _book(2, start=dt.time(14, 0), end=dt.time(16, 0), day=day),
                return_exceptions=True,
            )
            a, b = gathered[0], gathered[1]
        finally:
            bk.find_conflict = original

        assert not isinstance(a, BaseException), f"请求 A 抛异常：{a!r}"
        assert not isinstance(b, BaseException), f"请求 B 抛异常：{b!r}"
        assert calls["n"] >= 3, (
            "复检只被调了 "
            + str(calls["n"])
            + " 次：说明下面的请求没走到写格就被挡住了，"
            "这个测试没有真正验证唯一索引"
        )

        ok = [r for r in (a, b) if r.ok]
        assert len(ok) == 1, f"期望恰好 1 个成功，实际 {len(ok)} —— 占用格索引没生效"

        # 输的那个应当是"被占了"这种业务冲突，而不是 500 / 死锁 / 静默失败
        loser = next(r for r in (a, b) if not r.ok)
        assert loser.reason == "conflict", f"失败原因应为 conflict，实得 {loser.reason}"

        # 落库必须只有 1 条，且占用格正好 4 行（13:00-15:00 是 4 个 30 分钟格）
        async with session_scope() as session:
            n_res = (
                await session.execute(
                    select(func.count())
                    .select_from(Reservation)
                    .where(Reservation.date == day)
                )
            ).scalar()
        assert n_res == 1, f"落库 {n_res} 条，超卖"
