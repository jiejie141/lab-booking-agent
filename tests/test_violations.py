"""违约：「约了不来」的判定与后果（P1-8）。

这一组要证明的**不是**"系统能扣分别人"，而是它在**该犹豫的时候会犹豫**：
判定只能建立在门禁流水上，没有流水就不判；默认只记录不处罚；
判错了有人能推翻。惩罚性功能做错方向的代价是冤枉一个守规矩的人，
而他自己往往说不清为什么约不上 —— 所以这里的每一条都围着"不误伤"写。

反过来的那一半同样重要：门禁确实在跑、人确实没来，系统就得判得出来。
只犹豫不判定的黑名单等于没有黑名单。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from lagent.db import session_scope
from lagent.models import (
    ACCESS_DENIED,
    ACCESS_GRANTED,
    DIRECTION_IN,
    DIRECTION_OUT,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    AccessEvent,
    Reservation,
)

ZHANGWEI, LINA, ADMIN = 1, 2, 3
UV = 2          # 紫外可见分光光度计（分析楼 301）
LAB_301 = 1
LAB_205 = 2     # 生物楼 205（另一个实验室，用于验证判定范围不串门）
START = dt.time(10, 0)
END = dt.time(12, 0)


def local_now() -> dt.datetime:
    from lagent.clock import now_local

    return now_local()


def yesterday() -> dt.date:
    return local_now().date() - dt.timedelta(days=1)


async def make_reservation(
    *,
    user_id: int = ZHANGWEI,
    equipment_id: int = UV,
    date: dt.date | None = None,
    status: str = STATUS_EXPIRED,
    **overrides,
) -> int:
    """直接插一条**已经过去**的预约。

    不走 ``create_reservation`` 是刻意的：那个函数会拒绝过去的时间
    （"该时间点已经过去"），而这里要造的正是已经发生的预约。
    """
    async with session_scope() as session:
        res = Reservation(
            user_id=user_id,
            equipment_id=equipment_id,
            date=date or yesterday(),
            start_time=START,
            end_time=END,
            status=status,
            purpose="违约测试",
            version=1,
            **overrides,
        )
        session.add(res)
        await session.flush()
        return int(res.id)


async def record_event(
    *,
    user_id: int,
    lab_id: int = LAB_301,
    at: dt.datetime,
    result: str = ACCESS_GRANTED,
    direction: str = DIRECTION_IN,
) -> None:
    async with session_scope() as session:
        session.add(
            AccessEvent(
                occurred_at=at,
                user_id=user_id,
                lab_id=lab_id,
                gate_id="G1",
                direction=direction,
                result=result,
                reason_code="",
            )
        )


async def judge(now: dt.datetime | None = None) -> tuple[int, int]:
    """跑一次判定，返回 ``(判定数, 判不了数)``。"""
    from lagent.domain.violations import find_no_show_candidates, mark_no_shows

    now = now or local_now()
    async with session_scope() as session:
        candidates, undecidable = await find_no_show_candidates(session, now=now)
        marked = await mark_no_shows(
            session, [c.reservation_id for c in candidates], now=now
        )
        return marked, undecidable


async def count_violations(user_id: int, *, now: dt.datetime | None = None) -> int:
    from lagent.domain.violations import count_violations

    async with session_scope() as session:
        return await count_violations(session, user_id, now=now)


async def stamp_violations(user_id: int, count: int, *, days_ago: int = 1) -> list[int]:
    """直接把若干条预约标成违约（跳过判定，用于测**后果**）。"""
    ids: list[int] = []
    for _ in range(count):
        ids.append(
            await make_reservation(
                user_id=user_id,
                no_show_at=local_now() - dt.timedelta(days=days_ago),
            )
        )
    return ids


def enable_blocking(monkeypatch):
    """打开处罚开关。默认是关的 —— 这个开关本身就是被测对象之一。"""
    from lagent.config import reset_settings_cache

    monkeypatch.setenv("LAB_NOSHOW_BLOCKING_ENABLED", "true")
    reset_settings_cache()


# ==========================================================================
# 一、判不了的，就别判
# ==========================================================================
class TestUndecidableIsNotJudged:
    async def test_a_lab_without_any_door_record_is_never_judged(self, isolated_db):
        """★ 这一条是整个模块的命根子。

        没接门禁的实验室，流水里一条记录都没有。此时"没有入场记录"
        既可能是人没来，也可能是读卡器没通电。**猜前者会把所有人都判成违约。**
        """
        await make_reservation()
        marked, undecidable = await judge()

        assert marked == 0, "没有门禁流水就不该判定 —— 猜一次就是冤枉一个人"
        assert undecidable == 1
        assert await count_violations(ZHANGWEI) == 0

    async def test_undecidable_count_is_reported_not_swallowed(self, isolated_db):
        """判不了的数量要报出来：它衡量的是"门禁有没有在跑"。

        一个实验室长期大量判不了，等于在说它压根没接门禁。
        这个数字不出现，就只能等有人被冤枉了才知道。
        """
        for _ in range(3):
            await make_reservation()
        marked, undecidable = await judge()

        assert marked == 0
        assert undecidable == 3

    async def test_a_door_record_from_another_lab_does_not_make_it_decidable(
        self, isolated_db
    ):
        """别把"别的实验室有流水"当成"这个实验室的门禁在跑"。

        范围搞宽了，这个安全阀就形同虚设 —— 一个接了门禁的房间
        会让整栋楼的预约全部变成可判定。
        """
        await make_reservation()  # 设备在分析楼 301
        await record_event(
            user_id=LINA,
            lab_id=LAB_205,  # 生物楼 205：门禁在跑，但跑的不是这个房间
            at=dt.datetime.combine(yesterday(), dt.time(10, 30)),
        )
        marked, _ = await judge()
        assert marked == 0


# ==========================================================================
# 二、判得出的，要判得准
# ==========================================================================
class TestJudgement:
    async def test_no_entry_during_the_slot_is_a_violation(self, isolated_db):
        """门禁当天在跑，但这个人的时段里没有他进门的记录 —— 这才叫没来。"""
        await make_reservation()
        # 同一天、同一实验室，但**另一个人**进过门：证明门禁在工作
        await record_event(
            user_id=LINA, at=dt.datetime.combine(yesterday(), dt.time(9, 0))
        )
        marked, undecidable = await judge()

        assert marked == 1
        assert undecidable == 0
        assert await count_violations(ZHANGWEI) == 1

    async def test_entering_on_time_is_not_a_violation(self, isolated_db):
        await make_reservation()
        await record_event(
            user_id=ZHANGWEI, at=dt.datetime.combine(yesterday(), dt.time(10, 5))
        )
        marked, _ = await judge()
        assert marked == 0

    async def test_arriving_late_within_grace_is_not_a_violation(self, isolated_db):
        """迟到但不算没来 —— 门禁放行了却判他违约，是最难解释的矛盾。

        判定用的宽限与门禁的入场宽限是同一个量级，两边必须一致。
        """
        from lagent.config import get_settings

        grace = get_settings().noshow_grace_minutes
        await make_reservation()
        await record_event(
            user_id=ZHANGWEI,
            at=dt.datetime.combine(yesterday(), END) + dt.timedelta(minutes=grace - 1),
        )
        marked, _ = await judge()
        assert marked == 0

    async def test_being_denied_at_the_door_is_not_being_present(self, isolated_db):
        """被门口拦下（凭证过期/无资质）不等于到场。

        把"刷了卡"当成"来了"，会让人靠一张无效凭证洗掉违约记录。
        """
        await make_reservation()
        await record_event(
            user_id=ZHANGWEI,
            at=dt.datetime.combine(yesterday(), dt.time(10, 30)),
            result=ACCESS_DENIED,
        )
        marked, _ = await judge()
        assert marked == 1

    async def test_walking_out_is_not_being_present(self, isolated_db):
        """只有**进门**记录算到场；出门记录说明他来过但那不是这次的预约。"""
        await make_reservation()
        await record_event(
            user_id=ZHANGWEI,
            at=dt.datetime.combine(yesterday(), dt.time(10, 30)),
            direction=DIRECTION_OUT,
        )
        marked, _ = await judge()
        assert marked == 1

    async def test_a_cancelled_reservation_is_never_a_violation(self, isolated_db):
        """取消不是违约。约了又主动取消，恰恰是该被鼓励的行为。"""
        await make_reservation(status=STATUS_CANCELLED)
        await record_event(
            user_id=LINA, at=dt.datetime.combine(yesterday(), dt.time(9, 0))
        )
        marked, _ = await judge()
        assert marked == 0

    async def test_todays_reservation_is_not_judged_yet(self, isolated_db):
        """今天刚过期的时段可能是"迟到"，不是"没来"。明天再判。"""
        await make_reservation(date=local_now().date())
        await record_event(
            user_id=LINA, at=dt.datetime.combine(local_now().date(), dt.time(9, 0))
        )
        marked, _ = await judge()
        assert marked == 0

    async def test_judging_twice_does_not_double_count(self, isolated_db):
        """判定必须幂等：清扫任务一天跑好几次是常态。"""
        await make_reservation()
        await record_event(
            user_id=LINA, at=dt.datetime.combine(yesterday(), dt.time(9, 0))
        )
        assert (await judge())[0] == 1
        assert (await judge())[0] == 0
        assert await count_violations(ZHANGWEI) == 1


# ==========================================================================
# 三、后果：默认只记录，处罚要显式打开
# ==========================================================================
class TestConsequence:
    async def test_violations_are_recorded_even_when_blocking_is_off(
        self, isolated_db, http, as_user
    ):
        """默认不拦人，但**必须照记**。

        "记了没生效"和"什么都没发现"在界面上必须是两回事，
        否则刚上线的那段时间里管理员会以为系统瞎了。
        """
        await stamp_violations(ZHANGWEI, 3)
        headers = await as_user("张伟")

        resp = await http.post(
            "/api/reservations",
            json={
                "equipment_id": UV,
                "date": (local_now().date() + dt.timedelta(days=2)).isoformat(),
                "start": "10:00",
                "end": "12:00",
                "purpose": "违约测试",
            },
            headers=headers,
        )
        assert resp.status_code == 201, resp.text

        got = await http.get(f"/api/users/{ZHANGWEI}/violations", headers=headers)
        assert got.status_code == 200
        body = got.json()
        assert body["count"] == 3
        assert body["blocked"] is False
        assert body["over_threshold"] is True
        assert body["blocking_enabled"] is False

    async def test_blocking_stops_booking_once_enabled(
        self, isolated_db, http, as_user, monkeypatch
    ):
        enable_blocking(monkeypatch)
        await stamp_violations(ZHANGWEI, 3)
        headers = await as_user("张伟")

        resp = await http.post(
            "/api/reservations",
            json={
                "equipment_id": UV,
                "date": (local_now().date() + dt.timedelta(days=2)).isoformat(),
                "start": "10:00",
                "end": "12:00",
                "purpose": "违约测试",
            },
            headers=headers,
        )
        # 403 而不是 409/422：他填的时段没问题，是他此刻没有预约资格。
        assert resp.status_code == 403, resp.text
        assert "未到场" in resp.json()["detail"]
        assert "阈值" in resp.json()["detail"]

    async def test_below_threshold_nobody_is_blocked(
        self, isolated_db, http, as_user, monkeypatch
    ):
        enable_blocking(monkeypatch)
        await stamp_violations(ZHANGWEI, 2)
        headers = await as_user("张伟")

        resp = await http.post(
            "/api/reservations",
            json={
                "equipment_id": UV,
                "date": (local_now().date() + dt.timedelta(days=2)).isoformat(),
                "start": "10:00",
                "end": "12:00",
                "purpose": "违约测试",
            },
            headers=headers,
        )
        assert resp.status_code == 201, resp.text

    async def test_old_violations_fall_out_of_the_window(self, isolated_db, monkeypatch):
        """两年前的一次失误不能永远压着一个人 —— 那不是管理，是记仇。"""
        from lagent.config import get_settings

        window = get_settings().noshow_window_days
        await stamp_violations(ZHANGWEI, 1, days_ago=window + 5)
        assert await count_violations(ZHANGWEI) == 0

        await stamp_violations(ZHANGWEI, 1, days_ago=window - 5)
        assert await count_violations(ZHANGWEI) == 1

    async def test_the_threshold_comes_from_settings(self, isolated_db, monkeypatch):
        """阈值可配：不同院系对"几次算屡犯"的容忍度不一样。"""
        monkeypatch.setenv("LAB_NOSHOW_BLOCK_THRESHOLD", "2")
        from lagent.config import get_settings, reset_settings_cache

        reset_settings_cache()
        assert get_settings().noshow_block_threshold == 2
        reset_settings_cache()


# ==========================================================================
# 四、豁免：判错了要有人能推翻
# ==========================================================================
class TestPardon:
    async def test_pardon_lifts_the_block(self, isolated_db, http, as_user, monkeypatch):
        enable_blocking(monkeypatch)
        ids = await stamp_violations(ZHANGWEI, 3)
        admin_headers = await as_user("管理员")

        resp = await http.post(
            f"/api/reservations/{ids[0]}/pardon", headers=admin_headers
        )
        assert resp.status_code == 200, resp.text

        assert await count_violations(ZHANGWEI) == 2

        headers = await as_user("张伟")
        booking = await http.post(
            "/api/reservations",
            json={
                "equipment_id": UV,
                "date": (local_now().date() + dt.timedelta(days=2)).isoformat(),
                "start": "10:00",
                "end": "12:00",
                "purpose": "违约测试",
            },
            headers=headers,
        )
        assert booking.status_code == 201, booking.text

    async def test_pardon_keeps_the_original_judgement(self, isolated_db, http, as_user):
        """豁免**不抹掉**判定事实：系统确实判过，是人推翻了。

        抹掉它等于说"系统从没这么认为过"，那么下一次排查
        "门禁是不是在误判"时就永远查不到这批记录。
        """
        ids = await stamp_violations(ZHANGWEI, 1)
        admin_headers = await as_user("管理员")
        await http.post(f"/api/reservations/{ids[0]}/pardon", headers=admin_headers)

        async with session_scope() as session:
            row = await session.get(Reservation, ids[0])
            assert row is not None
            assert row.no_show_at is not None, "判定事实必须留着"
            assert row.pardoned_at is not None

    async def test_pardoning_twice_is_not_an_error(self, isolated_db, http, as_user):
        """管理员连点两下不该看到错误 —— 那只会让他不敢再用这个按钮。"""
        ids = await stamp_violations(ZHANGWEI, 1)
        admin_headers = await as_user("管理员")
        first = await http.post(
            f"/api/reservations/{ids[0]}/pardon", headers=admin_headers
        )
        second = await http.post(
            f"/api/reservations/{ids[0]}/pardon", headers=admin_headers
        )
        assert first.status_code == 200
        assert second.status_code == 200

    async def test_pardoning_a_normal_reservation_is_404(
        self, isolated_db, http, as_user
    ):
        """没判过违约的预约谈不上豁免。404 而不是静默成功 ——
        静默成功会让管理员以为自己刚刚做了什么。
        """
        rid = await make_reservation()
        admin_headers = await as_user("管理员")
        resp = await http.post(f"/api/reservations/{rid}/pardon", headers=admin_headers)
        assert resp.status_code == 404

    async def test_only_admins_can_pardon(self, isolated_db, http, as_user):
        """豁免是"撤销系统的判定"，不能让被判定的人自己点。"""
        ids = await stamp_violations(ZHANGWEI, 1)
        headers = await as_user("张伟")
        resp = await http.post(f"/api/reservations/{ids[0]}/pardon", headers=headers)
        assert resp.status_code == 403


# ==========================================================================
# 五、可见性：谁看得到自己的违约记录
# ==========================================================================
class TestVisibility:
    async def test_a_user_sees_their_own_record(self, isolated_db, http, as_user):
        await stamp_violations(ZHANGWEI, 1)
        headers = await as_user("张伟")

        got = await http.get(f"/api/users/{ZHANGWEI}/violations", headers=headers)
        assert got.status_code == 200
        assert got.json()["count"] == 1
        assert len(got.json()["records"]) == 1

    async def test_asking_for_someone_else_falls_back_to_you(
        self, isolated_db, http, as_user
    ):
        """与预约列表同一条规矩：传别人的 id 不报错，而是改写成自己。

        越权读取在服务端就断了，不依赖前端自觉。
        """
        await stamp_violations(LINA, 3)
        await stamp_violations(ZHANGWEI, 1)
        headers = await as_user("张伟")

        got = await http.get(f"/api/users/{LINA}/violations", headers=headers)
        assert got.status_code == 200
        # 拿到的不是李娜的 3 条，而是自己的 1 条
        assert got.json()["user_id"] == ZHANGWEI
        assert got.json()["count"] == 1

    async def test_an_admin_can_read_anyone(self, isolated_db, http, as_user):
        await stamp_violations(LINA, 2)
        admin_headers = await as_user("管理员")

        got = await http.get(f"/api/users/{LINA}/violations", headers=admin_headers)
        assert got.status_code == 200
        assert got.json()["user_id"] == LINA
        assert got.json()["count"] == 2

    async def test_reading_violations_needs_a_token(self, isolated_db, http):
        resp = await http.get(f"/api/users/{ZHANGWEI}/violations")
        assert resp.status_code == 401


# ==========================================================================
# 六、清扫接线
# ==========================================================================
class TestSweepWiring:
    async def test_the_sweep_task_judges_and_reports(self, isolated_db):
        from lagent.sweep import sweep_no_shows

        await make_reservation()
        await record_event(
            user_id=LINA, at=dt.datetime.combine(yesterday(), dt.time(9, 0))
        )
        processed, _detail = await sweep_no_shows()

        assert processed == 1
        assert await count_violations(ZHANGWEI) == 1

    async def test_the_sweep_task_says_when_it_could_not_decide(self, isolated_db):
        """判不了要写进 detail —— 否则"门禁没接"这件事永远没人知道。"""
        from lagent.sweep import sweep_no_shows

        await make_reservation()
        processed, detail = await sweep_no_shows()

        assert processed == 0
        assert "判不了" in detail

    async def test_the_sweep_task_audits_each_judgement(self, isolated_db):
        """逐条审计：一个人来问"凭什么不让我约"时，要能翻出每一条依据。"""
        from lagent.models import AuditLog
        from lagent.sweep import sweep_no_shows

        await make_reservation()
        await record_event(
            user_id=LINA, at=dt.datetime.combine(yesterday(), dt.time(9, 0))
        )
        await sweep_no_shows()

        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "violation.no_show")
                )
            ).scalars().all()
        assert len(rows) == 1
        assert rows[0].actor_id == ZHANGWEI


# ==========================================================================
# 七、配置边界
# ==========================================================================
class TestConfig:
    @pytest.mark.parametrize(
        "name,value",
        [
            ("LAB_NOSHOW_GRACE_MINUTES", "0"),
            ("LAB_NOSHOW_GRACE_MINUTES", "120"),
            ("LAB_NOSHOW_WINDOW_DAYS", "1"),
            ("LAB_NOSHOW_WINDOW_DAYS", "3650"),
            ("LAB_NOSHOW_BLOCK_THRESHOLD", "1"),
            ("LAB_NOSHOW_BLOCK_THRESHOLD", "100"),
        ],
    )
    def test_in_range_values_are_accepted(self, name, value, monkeypatch):
        from lagent.config import get_settings, reset_settings_cache

        monkeypatch.setenv(name, value)
        reset_settings_cache()
        try:
            get_settings()
        finally:
            reset_settings_cache()

    @pytest.mark.parametrize(
        "name,value",
        [
            ("LAB_NOSHOW_GRACE_MINUTES", "121"),
            ("LAB_NOSHOW_WINDOW_DAYS", "0"),
            ("LAB_NOSHOW_BLOCK_THRESHOLD", "0"),
        ],
    )
    def test_out_of_range_values_are_rejected(self, name, value, monkeypatch):
        from pydantic import ValidationError

        from lagent.config import get_settings, reset_settings_cache

        monkeypatch.setenv(name, value)
        reset_settings_cache()
        try:
            with pytest.raises(ValidationError):
                get_settings()
        finally:
            reset_settings_cache()
