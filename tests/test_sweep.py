"""后台清扫（P1-2）：把「没人管就会一直占着」的状态收回来。

## 这个文件在证什么

清扫任务最容易写成「看起来很忙、其实什么都没解决」。所以这里的断言
**一律落在用户能感知的后果**上，而不是"状态字段变了"：

* 忘刷出场的人被收尾之后，**他必须能重新进实验室**（座位真的放开了）——
  只断言 ``status == 'used'`` 是不够的，那可能只是改了个字段；
* 明天的预约**不能被顺手清掉**（过度清扫比不扫更糟，而且很难被发现）；
* 归档失败时**一行都不能删**（"删了但没归档"是唯一真正会丢数据的组合）；
* 一个任务失败**不能拖停其它任务**。

最后一组是「任务框架」本身的验收，与业务无关，但缺了它前面所有断言都不可靠 ——
一个异常就让整个循环静默退出的框架，在测试里根本不会露出来。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import select, update

from lagent.db import session_scope
from lagent.models import (
    ACTION_SWEEP_FORCE_CHECKOUT,
    ACTION_SWEEP_RESERVATION_EXPIRED,
    ACTIVE_STATUSES,
    AuditLog,
    EntryPermit,
    Laboratory,
    Reservation,
    ReservationSlot,
)
from lagent.sweep import (
    FORCED_GATE_OUT,
    SweepResult,
    SweepRunner,
    SweepTask,
    count_pending,
    sweep_archive_events,
    sweep_expired_reservations,
    sweep_stale_permits,
)

# 演示账号（与 conftest.SEED 一致）
ZHANGWEI, LINA, ADMIN = 1, 2, 3
# 分析楼 301（工作日 08:00-22:00），种子里的 1 号楼
LAB_SPEC = 1


def at(hour: int, minute: int = 0, day: dt.date | None = None) -> dt.datetime:
    return dt.datetime.combine(day or dt.date(2026, 9, 24), dt.time(hour, minute))


# ==========================================================================
# 任务 1：过期预约
# ==========================================================================
class TestExpiredReservations:
    async def test_past_reservation_becomes_expired_and_frees_its_slots(self, isolated_db):
        """往日预约：状态转 expired，**占用格一并删掉**。

        只改状态不删格子的话，「已过期」的预约还占着时段，
        `uq_equipment_slot` 会让同一时段约不回来 —— 这正是要解开的"假占用"。
        """
        from lagent.domain.booking import attach_slots

        yesterday = dt.date(2026, 9, 23)
        async with session_scope() as session:
            res = Reservation(
                user_id=LINA,
                equipment_id=1,
                date=yesterday,
                start_time=dt.time(9, 0),
                end_time=dt.time(11, 0),
                status="confirmed",
            )
            session.add(res)
            await session.flush()
            await attach_slots(session, res)
            res_id = res.id

        async with session_scope() as session:
            assert (
                await session.scalar(
                    select(ReservationSlot.id).where(ReservationSlot.reservation_id == res_id)
                )
            ) is not None

        changed, _ = await sweep_expired_reservations(now=at(10, 0))
        assert changed == 1

        async with session_scope() as session:
            row = await session.get(Reservation, res_id)
            assert row is not None and row.status == "expired"
            slots = (
                await session.execute(
                    select(ReservationSlot.id).where(ReservationSlot.reservation_id == res_id)
                )
            ).all()
        assert slots == [], "过期的预约不该继续占着占用格"

    async def test_todays_finished_reservation_is_swept_too(self, isolated_db):
        """★ 今天已经用完的也要清。

        只判 `date < 今天` 是个很自然的写法，代价是今天上午的预约会一直挂到明天；
        而「今天还剩几个时段」这类统计恰恰最关心当天。
        """
        today = dt.date(2026, 9, 24)
        async with session_scope() as session:
            session.add(
                Reservation(
                    user_id=LINA,
                    equipment_id=1,
                    date=today,
                    start_time=dt.time(8, 0),
                    end_time=dt.time(9, 0),
                    status="confirmed",
                )
            )
        changed, _ = await sweep_expired_reservations(now=at(12, 0))
        assert changed == 1

    async def test_future_reservation_is_left_alone(self, isolated_db):
        """反面对照：还没到时间的预约**一条都不能动**。

        过度清扫比不扫更糟：它把用户真金白银约到的时段悄悄删掉，
        而且要到用户去实验室才发现。

        断言按 **id** 收口，而不是按日期捞：种子数据本身就有「明天」的预约，
        按日期比对会把它们算进来 —— 那样这个用例会因为**数据变换了**而红，
        而不是因为清扫出错了。
        """
        future = dt.date(2026, 10, 20)
        async with session_scope() as session:
            res = Reservation(
                user_id=LINA,
                equipment_id=1,
                date=future,
                start_time=dt.time(9, 0),
                end_time=dt.time(11, 0),
                status="confirmed",
            )
            session.add(res)
            await session.flush()
            res_id = res.id

        changed, _ = await sweep_expired_reservations(now=at(23, 0))
        assert changed == 0
        async with session_scope() as session:
            row = await session.get(Reservation, res_id)
        assert row is not None and row.status == "confirmed"

    async def test_already_cancelled_reservation_keeps_its_status(self, isolated_db):
        """已取消的**不该被改写**：那是用户自己的操作痕迹，不是过期。"""
        async with session_scope() as session:
            session.add(
                Reservation(
                    user_id=LINA,
                    equipment_id=1,
                    date=dt.date(2026, 9, 20),
                    start_time=dt.time(9, 0),
                    end_time=dt.time(10, 0),
                    status="cancelled",
                    cancel_reason="用户取消",
                )
            )
        changed, _ = await sweep_expired_reservations(now=at(12, 0))
        assert changed == 0
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(Reservation).where(Reservation.date == dt.date(2026, 9, 20))
                )
            ).scalar_one()
        assert row.status == "cancelled" and row.cancel_reason == "用户取消"

    async def test_sweeping_twice_processes_nothing_the_second_time(self, isolated_db):
        """幂等：第二轮必须是 0。否则多副本轮转会把同一个数字反复报出来。"""
        async with session_scope() as session:
            session.add(
                Reservation(
                    user_id=LINA,
                    equipment_id=1,
                    date=dt.date(2026, 9, 20),
                    start_time=dt.time(9, 0),
                    end_time=dt.time(10, 0),
                    status="confirmed",
                )
            )
        first, _ = await sweep_expired_reservations(now=at(12, 0))
        second, _ = await sweep_expired_reservations(now=at(12, 0))
        assert (first, second) == (1, 0)

    async def test_expiring_leaves_a_summary_in_the_audit_trail(self, isolated_db):
        """留痕是**一条汇总**，不是逐条 —— 逐条只会把审计表灌满噪声。"""
        async with session_scope() as session:
            session.add(
                Reservation(
                    user_id=LINA,
                    equipment_id=1,
                    date=dt.date(2026, 9, 20),
                    start_time=dt.time(9, 0),
                    end_time=dt.time(10, 0),
                    status="confirmed",
                )
            )
        await sweep_expired_reservations(now=at(12, 0))
        async with session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(AuditLog).where(
                            AuditLog.action == ACTION_SWEEP_RESERVATION_EXPIRED
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert "1 条" in rows[0].detail


# ==========================================================================
# 任务 2：凭证超时与关门收尾
# ==========================================================================
async def _grant_and_enter(*, user_id: int, lab_id: int, when: dt.datetime) -> tuple[int, str]:
    """签发一张凭证并让它入场，返回 (permit_id, 明文凭证)。"""
    from lagent.domain.access import issue_permit, verify_entry

    async with session_scope() as session:
        permit, plain = await issue_permit(
            session,
            user_id=user_id,
            lab_id=lab_id,
            date_=when.date(),
            valid_from=dt.time(8, 0),
            valid_to=dt.time(22, 0),
            source="admin_grant",
        )
        permit_id = permit.id
    async with session_scope() as session:
        decision = await verify_entry(
            session, lab_id=lab_id, now=when, credential=plain, identity_user_id=user_id
        )
    assert decision.ok, f"准备数据失败：{decision.reason_code} {decision.message}"
    return permit_id, plain


class TestStalePermits:
    async def test_forgetting_to_check_out_locks_a_person_out_until_swept(self, isolated_db):
        """★ 这是本任务存在的理由，所以断言落在**用户能感知的后果**上。

        场景：李娜进去了、忘了刷出场。`uq_permit_one_inside` 会让她再也进不了
        任何房间 —— 包括第二天换一个实验室。关门后清扫必须把她放出来，
        并且**她第二天真的能进去**。

        只断言 `status == 'used'` 是不够的：那只说明改了个字段，
        说明不了座位真的被释放了。
        """
        from lagent.domain.access import verify_entry
        from lagent.models import DENY_ALREADY_INSIDE

        day, next_day = dt.date(2026, 9, 24), dt.date(2026, 9, 25)
        permit_id, _ = await _grant_and_enter(user_id=LINA, lab_id=LAB_SPEC, when=at(10, 0, day))

        # 关门后（22:00 + 30 分钟宽限）跑清扫
        changed, detail = await sweep_stale_permits(now=at(22, 45, day))
        assert changed == 1, detail

        async with session_scope() as session:
            permit = await session.get(EntryPermit, permit_id)
            assert permit is not None
            assert permit.status == "used"
            assert permit.checked_out_at == at(22, 45, day)
            assert permit.gate_out == FORCED_GATE_OUT, "要能区分「系统替他收尾」和「他刷了卡」"

        # 座位真的放开了吗？第二天让他再进一次 —— 这条才是"假占用解开了"的证据
        _, fresh = await _grant_and_enter(user_id=LINA, lab_id=LAB_SPEC, when=at(10, 0, next_day))
        async with session_scope() as session:
            again = await verify_entry(
                session,
                lab_id=LAB_SPEC,
                now=at(10, 0, next_day),
                credential=fresh,
                identity_user_id=LINA,
            )
        assert not again.ok
        assert again.reason_code == DENY_ALREADY_INSIDE, (
            "刚用新凭证进的门，再刷一次应当报「已在馆」而不是别的 —— "
            "报别的说明座位/在馆状态没有被正确处理"
        )

    async def test_people_still_inside_opening_hours_are_not_touched(self, isolated_db):
        """★ 反面对照：实验室还开着的时候**不能**把人踢出在馆名单。

        用 valid_to 当收尾依据就会犯这个错 —— 而 valid_to 是*入场*窗口的上限，
        不是离场时间。一个人完全可以合法地在 valid_to 之前进去、之后还在做实验。

        用李娜而不是张伟：分析楼 301 里有「高效液相色谱仪」（受控设备），
        所以进这个房间要「色谱」资质 —— 张伟只有光谱资质，连准备数据这一步都过不去。
        """
        day = dt.date(2026, 9, 24)
        permit_id, _ = await _grant_and_enter(user_id=LINA, lab_id=LAB_SPEC, when=at(14, 0, day))

        # 19:00 —— 分析楼当天 08:00-22:00，还在开放时间内
        changed, detail = await sweep_stale_permits(now=at(19, 0, day))
        assert changed == 0, f"开放时间内不该收尾任何人：{detail}"
        async with session_scope() as session:
            permit = await session.get(EntryPermit, permit_id)
            assert permit is not None and permit.status == "checked_in"

    async def test_yesterdays_checked_in_permit_is_cleaned_up(self, isolated_db):
        """往日遗留：昨天进去、再没出来 —— 必须收尾，否则他永远进不了任何房间。"""
        yesterday = dt.date(2026, 9, 23)
        permit_id, _ = await _grant_and_enter(
            user_id=ADMIN, lab_id=LAB_SPEC, when=at(10, 0, yesterday)
        )
        changed, _ = await sweep_stale_permits(now=at(9, 0, dt.date(2026, 9, 24)))
        assert changed == 1
        async with session_scope() as session:
            permit = await session.get(EntryPermit, permit_id)
            assert permit is not None and permit.status == "used"

    async def test_issued_but_never_used_permit_from_a_past_day_is_expired(self, isolated_db):
        """★ 往日签发、从未使用的凭证也要标记过期。

        只判「今天到点」会留下一个隐蔽的残留：昨天的凭证永远停在 `issued`，
        于是「还有多少张没用的凭证」这个统计单调增长、永远对不上。
        """
        from lagent.domain.access import issue_permit

        yesterday = dt.date(2026, 9, 23)
        async with session_scope() as session:
            permit, _ = await issue_permit(
                session,
                user_id=LINA,
                lab_id=LAB_SPEC,
                date_=yesterday,
                valid_from=dt.time(9, 0),
                valid_to=dt.time(10, 0),
                source="admin_grant",
            )
            permit_id = permit.id

        await sweep_stale_permits(now=at(9, 0, dt.date(2026, 9, 24)))
        async with session_scope() as session:
            row = await session.get(EntryPermit, permit_id)
        assert row is not None and row.status == "expired"

    async def test_force_checkout_is_recorded_in_the_audit_trail(self, isolated_db):
        """★ 把人从在馆名单里请出去，必须逐条可查。

        这条与「预约过期只记汇总」形成对照：前者关系到一个具体的人，
        事后一定会有人问「他为什么被算作已离场」。
        """
        day = dt.date(2026, 9, 24)
        permit_id, _ = await _grant_and_enter(user_id=LINA, lab_id=LAB_SPEC, when=at(10, 0, day))
        await sweep_stale_permits(now=at(23, 0, day))

        async with session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(AuditLog).where(AuditLog.action == ACTION_SWEEP_FORCE_CHECKOUT)
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_name == "system"
        assert str(permit_id) == row.target_id
        assert f"user={LINA}" in row.detail

    async def test_forced_checkout_does_not_fake_an_access_event(self, isolated_db):
        """★ 强制收尾**不能**往门禁流水里塞一条假出场。

        `access_events` 的定义是「谁什么时候站在哪个门口」。为了好看而补一条
        没有对应刷卡的记录，会让这条证据链变得不可信 —— 而它的全部价值
        就在于可信。所以这里断言：清扫前后通行事件数不变。
        """
        from lagent.models import AccessEvent

        day = dt.date(2026, 9, 24)
        await _grant_and_enter(user_id=LINA, lab_id=LAB_SPEC, when=at(10, 0, day))
        async with session_scope() as session:
            before = (
                await session.execute(select(EntryPermit).where(EntryPermit.status == "checked_in"))
            ).all()
            events_before = len((await session.execute(select(AccessEvent))).scalars().all())
        assert before, "准备数据失败"

        await sweep_stale_permits(now=at(23, 0, day))

        async with session_scope() as session:
            events_after = len((await session.execute(select(AccessEvent))).scalars().all())
        assert events_after == events_before

    async def test_a_normal_exit_is_not_disturbed(self, isolated_db):
        """反面对照：正常刷过出场的人，清扫不该再碰他。"""
        from lagent.domain.access import verify_exit

        day = dt.date(2026, 9, 24)
        permit_id, _ = await _grant_and_enter(user_id=LINA, lab_id=LAB_SPEC, when=at(10, 0, day))
        async with session_scope() as session:
            outcome = await verify_exit(session, user_id=LINA, now=at(12, 0, day))
        assert outcome.ok

        await sweep_stale_permits(now=at(23, 0, day))
        async with session_scope() as session:
            permit = await session.get(EntryPermit, permit_id)
        assert permit is not None
        assert permit.status == "used"
        assert permit.gate_out != FORCED_GATE_OUT, "正常出场的人不该被标成系统收尾"
        assert permit.checked_out_at == at(12, 0, day)


# ==========================================================================
# 任务 3：归档
# ==========================================================================
class TestArchive:
    @pytest.fixture(autouse=True)
    def _archive_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LAB_ARCHIVE_DIR", str(tmp_path / "archive"))
        monkeypatch.setenv("LAB_AUDIT_RETENTION_DAYS", "7")
        monkeypatch.setenv("LAB_ACCESS_EVENT_RETENTION_DAYS", "7")
        from lagent.config import reset_settings_cache

        reset_settings_cache()
        yield tmp_path / "archive"
        reset_settings_cache()

    async def test_old_rows_are_exported_then_removed(self, isolated_db, _archive_dir):
        """★ 归档要同时满足两件事：库里删掉、文件里有完整内容。

        只断言"库里没了"会让一个只删不导的实现通过 —— 那就真的丢数据了。
        """
        day = dt.date(2026, 9, 24)
        async with session_scope() as session:
            session.add(
                AuditLog(
                    action="auth.login",
                    actor_name="张伟",
                    detail="很久以前的一次登录",
                    created_at=at(9, 0, day) - dt.timedelta(days=30),
                )
            )
            session.add(
                AuditLog(
                    action="auth.login",
                    actor_name="李娜",
                    detail="刚刚的一次登录",
                    created_at=at(9, 0, day),
                )
            )

        archived, detail = await sweep_archive_events(now=at(9, 0, day))
        assert archived == 1, detail

        files = sorted(_archive_dir.glob("audit-*.jsonl"))
        assert len(files) == 1
        payload = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
        assert len(payload) == 1
        assert payload[0]["actor_name"] == "张伟"
        assert payload[0]["action"] == "auth.login"

        async with session_scope() as session:
            remaining = (
                (
                    await session.execute(
                        select(AuditLog).where(AuditLog.action == "auth.login")
                    )
                )
                .scalars()
                .all()
            )
        # 只比 auth.login：清扫**自己**也会写一条归档汇总（actor 是空的），
        # 把它算进来会让断言看起来像"多了一行没被清掉"，实则完全正常。
        assert [r.actor_name for r in remaining] == ["李娜"], "没超期的记录不能被归档掉"

    async def test_second_run_same_day_does_not_overwrite_the_first_archive(
        self, isolated_db, _archive_dir
    ):
        """★ 同一天跑两批，第一批的文件不能被覆盖。

        用「一天一个文件」的命名时，第二批会在第一批已经删掉源数据之后
        把它覆盖掉 —— 数据就此消失，而且两批的输出都"看起来正常"。
        """
        day = dt.date(2026, 9, 24)
        for name in ("甲", "乙"):
            async with session_scope() as session:
                session.add(
                    AuditLog(
                        action="auth.login",
                        actor_name=name,
                        created_at=at(9, 0, day) - dt.timedelta(days=30),
                    )
                )
            await sweep_archive_events(now=at(9, 0, day))

        files = sorted(_archive_dir.glob("audit-*.jsonl"))
        assert len(files) == 2, f"两批应当留下两个文件，实得 {[f.name for f in files]}"
        # 用集合而不是有序列表：两个文件名的先后取决于「-1 后缀」和「.」谁排前面，
        # 那是文件名的偶然，不是被测行为。（中文按码位排序也和拼音不同。）
        names = [
            json.loads(line)["actor_name"]
            for path in files
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        assert sorted(names) == sorted(["甲", "乙"]), f"两批内容都必须在：{names}"

    async def test_archive_failure_deletes_nothing(self, isolated_db, monkeypatch, tmp_path):
        """★ 归档失败时**一行都不能删**。

        这是唯一真正会丢数据的组合：删了但没归档。实现上的保障是
        「先落盘、后删除」，而落盘本身也做了原子写。
        这里用一个必然写不进去的路径来触发失败。
        """
        # 用一个**文件**当作目录名 → mkdir 必然失败
        blocker = tmp_path / "blocker"
        blocker.write_text("我不是目录", encoding="utf-8")
        monkeypatch.setenv("LAB_ARCHIVE_DIR", str(blocker / "archive"))
        from lagent.config import reset_settings_cache

        reset_settings_cache()
        try:
            async with session_scope() as session:
                session.add(
                    AuditLog(
                        action="auth.login",
                        actor_name="甲",
                        created_at=at(9, 0) - dt.timedelta(days=30),
                    )
                )
            with pytest.raises(OSError):
                await sweep_archive_events(now=at(9, 0))
            async with session_scope() as session:
                remaining = (await session.execute(select(AuditLog))).scalars().all()
            assert len(remaining) == 1, "归档失败却删了源数据 —— 这是真的丢数据"
        finally:
            reset_settings_cache()

    async def test_empty_database_is_not_an_error(self, isolated_db, _archive_dir):
        """反面对照：没有可归档的东西时不该报错，也不该留下空文件。"""
        archived, _ = await sweep_archive_events(now=at(9, 0))
        assert archived == 0
        assert list(_archive_dir.glob("*.jsonl")) == []


# ==========================================================================
# 任务框架本身
# ==========================================================================
class TestRunner:
    async def test_one_failing_task_does_not_stop_the_others(self):
        """★ 失败隔离：这是整个框架存在的意义。

        没有它，「归档目录没权限」这种局部问题会把「凭证收尾」一起拖停 ——
        而后者才是真正要紧的那个，且它停摆不会报错，只会悄悄不做。
        """

        async def boom() -> tuple[int, str]:
            raise RuntimeError("磁盘满了")

        async def fine() -> tuple[int, str]:
            return 3, "正常"

        from lagent.sweep import run_once as run_tasks

        results = await run_tasks(
            tasks=(SweepTask("先失败", boom), SweepTask("后成功", fine))
        )
        assert [r.ok for r in results] == [False, True]
        assert "RuntimeError" in results[0].error and "磁盘满了" in results[0].error
        assert results[1].processed == 3

    async def test_a_non_swallowed_error_is_reported_not_raised(self):
        """异常要变成**数据**（返回值里的一条），而不是冒出去打断调用方。"""

        async def boom() -> tuple[int, str]:
            raise ValueError("坏的输入")

        from lagent.sweep import run_once as run_tasks

        results = await run_tasks(tasks=(SweepTask("坏任务", boom),))
        assert results[0] == SweepResult("坏任务", error="ValueError: 坏的输入")

    async def test_runner_accumulates_counters(self, isolated_db):
        """累计量是给 `/metrics` 用的，口径必须与逐轮结果一致。

        这里刻意**逐轮累加**再比较，而不是拿 ``last_results`` 去比 ——
        后者只在"每轮处理量相同"时才成立，是个假通过：
        第一次跑有 3 条、第二次 0 条时，它会报错，而实现其实是对的。
        """
        runner = SweepRunner(interval_seconds=1)
        first = await runner.run_once()
        second = await runner.run_once()
        assert runner.runs == 2
        assert runner.failures == 0
        expected = sum(r.processed for r in first) + sum(r.processed for r in second)
        assert runner.total_processed == expected

    async def test_start_then_stop_really_stops(self, isolated_db):
        """★ 循环必须能真的停下来。

        停不下来的后台任务在测试里表现为"进程挂住"，在线上表现为热重载后
        多个循环同时写库 —— 后者更难查，因为它不报错。
        """
        import asyncio

        runner = SweepRunner(interval_seconds=3600)
        runner.start()
        assert runner.running
        await asyncio.sleep(0)  # 让循环至少跑起来一轮
        await runner.stop(timeout=5.0)
        assert not runner.running
        assert runner._task is None

    async def test_stop_is_safe_when_never_started(self):
        runner = SweepRunner(interval_seconds=3600)
        await runner.stop()
        assert not runner.running

    async def test_stop_is_safe_to_call_twice(self, isolated_db):
        runner = SweepRunner(interval_seconds=3600)
        runner.start()
        await runner.stop()
        await runner.stop()
        assert not runner.running

    async def test_default_tasks_cover_all_three_concerns(self):
        """默认任务集要覆盖三件事，别在重构里悄悄少掉一项。"""
        from lagent.sweep import DEFAULT_TASKS

        names = [task.name for task in DEFAULT_TASKS]
        assert len(names) == 3
        joined = " ".join(names)
        assert "预约" in joined and "凭证" in joined and "归档" in joined


# ==========================================================================
# 只读诊断
# ==========================================================================
class TestCountPending:
    async def test_reports_backlog_without_changing_anything(self, isolated_db):
        """★ 诊断必须是只读的。

        一个自称"看一眼"的命令顺手改数据的话，出问题时就没法用它取证了 ——
        而"取证"恰恰是这类命令最主要的用途。
        """
        from lagent.clock import now_local

        day = now_local().date()
        async with session_scope() as session:
            session.add(
                Reservation(
                    user_id=LINA,
                    equipment_id=1,
                    date=day - dt.timedelta(days=3),
                    start_time=dt.time(9, 0),
                    end_time=dt.time(10, 0),
                    status="confirmed",
                )
            )
            session.add(
                AuditLog(
                    action="auth.login",
                    actor_name="甲",
                    # 相对"现在"取：断言里比的是保留期（90 天），
                    # 写死日期的话用例会随着日历翻页而悄悄失效。
                    created_at=now_local() - dt.timedelta(days=400),
                )
            )

        pending = await count_pending()
        assert pending["active_reservations"] >= 1
        assert pending["archivable_audit_rows"] == 1

        # 什么都没变
        async with session_scope() as session:
            still_active = (
                await session.execute(
                    select(Reservation.id).where(Reservation.status.in_(ACTIVE_STATUSES))
                )
            ).all()
            audit_count = len((await session.execute(select(AuditLog))).scalars().all())
        assert still_active and audit_count == 1

    async def test_zeroes_on_a_fresh_database(self, isolated_db):
        pending = await count_pending()
        assert pending["stale_permits"] == 0
        assert pending["inside_people"] == 0
        assert pending["archivable_audit_rows"] == 0


# ==========================================================================
# 与门禁的边界：清扫不是安全依赖
# ==========================================================================
class TestSweepIsNotASecurityDependency:
    async def test_entry_is_denied_even_if_the_sweeper_never_runs(self, isolated_db):
        """★ 正确性不依赖清扫。

        如果有一天有人把「凭证过期」改成只靠清扫来标记，门禁就会在任务挂掉时
        把过期凭证放进去 —— 那是最严重的一类回归。所以这里显式钉住：
        **一次清扫都不跑**，过期的凭证也必须被拒。
        """
        from lagent.domain.access import issue_permit, verify_entry
        from lagent.models import DENY_PERMIT_EXPIRED

        day = dt.date(2026, 9, 24)
        async with session_scope() as session:
            _, plain = await issue_permit(
                session,
                user_id=LINA,
                lab_id=LAB_SPEC,
                date_=day,
                valid_from=dt.time(9, 0),
                valid_to=dt.time(10, 0),
                source="admin_grant",
            )
        async with session_scope() as session:
            decision = await verify_entry(
                session, lab_id=LAB_SPEC, now=at(12, 0, day), credential=plain,
                identity_user_id=LINA,
            )
        assert not decision.ok
        assert decision.reason_code == DENY_PERMIT_EXPIRED
        # 而且此刻库里它仍然是 issued —— 说明拒绝来自核验本身，不是清扫
        async with session_scope() as session:
            permit = (await session.execute(select(EntryPermit))).scalars().one()
        assert permit.status == "issued"

    async def test_a_lab_that_is_closed_that_day_releases_everyone(self, isolated_db):
        """实验室当天不开放 → 里面不该有人在（关门收尾的另一半条件）。

        这条覆盖的是「凭证日期当天不在开放时间内」这种情形：
        种子里的材料楼 412 周末 09:00-18:00、工作日 09:00-18:00，
        这里直接把 open_hours 清空来构造「当天完全不开放」。
        """
        day = dt.date(2026, 9, 24)
        permit_id, _ = await _grant_and_enter(user_id=ADMIN, lab_id=LAB_SPEC, when=at(10, 0, day))
        async with session_scope() as session:
            await session.execute(
                update(Laboratory).where(Laboratory.id == LAB_SPEC).values(open_hours={})
            )
        changed, _ = await sweep_stale_permits(now=at(10, 30, day))
        assert changed == 1
        async with session_scope() as session:
            permit = await session.get(EntryPermit, permit_id)
        assert permit is not None and permit.status == "used"


# ==========================================================================
# 与服务的接线：循环到底有没有在跑
# ==========================================================================
class TestLifespanWiring:
    """★ 「任务写好了」和「服务真的在跑它」是两件事。

    这里断言的是后者。少了这一组，一份写完却忘了接进 lifespan 的实现
    会让前面所有用例继续全绿 —— 它们都是**直接调用**任务函数的。

    注意测试环境默认把清扫关掉了（见 conftest，理由与"每例一套库"相同：
    跨用例共享的后台写库动作会让断言依赖调度顺序）。所以这里显式打开。
    """

    @pytest.fixture(autouse=True)
    def _enable(self, isolated_db, monkeypatch):
        monkeypatch.setenv("LAB_SWEEP_ENABLED", "true")
        from lagent.config import reset_settings_cache

        reset_settings_cache()
        yield
        reset_settings_cache()

    async def test_sweeper_runs_while_the_app_is_up_and_stops_on_shutdown(self, isolated_db):
        from lagent.api import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            sweeper = app.state.sweeper
            assert isinstance(sweeper, SweepRunner)
            assert sweeper.running, "服务起来了，后台清扫就该在跑"
            interval = sweeper.interval_seconds

        assert not sweeper.running, "lifespan 退出后循环必须停下 —— 否则进程退不干净"
        assert interval > 0, "间隔取自配置，不能被写成 0（0 会变成忙等）"


class TestSweepCanBeTurnedOff:
    """开关必须真的管用：不能用它「假装关掉」、实际还在跑。"""

    @pytest.fixture(autouse=True)
    def _disable(self, isolated_db, monkeypatch):
        monkeypatch.setenv("LAB_SWEEP_ENABLED", "false")
        from lagent.config import reset_settings_cache

        reset_settings_cache()
        yield
        reset_settings_cache()

    async def test_nothing_is_started_when_the_feature_is_off(self, isolated_db):
        from lagent.api import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            # 对象仍然挂着（运维要能读到它的计数器），但循环不跑
            assert isinstance(app.state.sweeper, SweepRunner)
            assert not app.state.sweeper.running


# ==========================================================================
# 命令行入口
# ==========================================================================
class TestSweepCommand:
    async def test_cli_runs_one_round_and_reports_counts(self, isolated_db, capsys):
        from lagent.cli import _sweep

        async with session_scope() as session:
            session.add(
                Reservation(
                    user_id=LINA,
                    equipment_id=1,
                    date=dt.date(2026, 9, 20),
                    start_time=dt.time(9, 0),
                    end_time=dt.time(10, 0),
                    status="confirmed",
                )
            )
        capsys.readouterr()
        assert await _sweep() == 0
        out = capsys.readouterr().out
        assert "后台清扫" in out
        assert "清扫前待处理" in out and "清扫后待处理" in out
        assert "1" in out

    async def test_cli_returns_nonzero_when_a_task_fails(
        self, isolated_db, monkeypatch, tmp_path, capsys
    ):
        """★ 失败要体现在退出码上。

        运维脚本只看退出码。失败却返回 0，等于把「清扫没做」伪装成「一切正常」——
        而清扫没做的后果要到有人卡在门口才暴露。
        """
        from lagent.cli import _sweep
        from lagent.clock import now_local

        # 用一个**文件**当作归档目录名 → mkdir 必然失败，归档任务抛 OSError。
        # 注意必须同时让"有东西可归档"，否则任务会在查库那一步就空手返回、
        # 根本走不到写文件 —— 那样这个用例会假通过。
        bad = tmp_path / "not-a-dir"
        bad.write_text("x", encoding="utf-8")
        monkeypatch.setenv("LAB_ARCHIVE_DIR", str(bad / "archive"))
        from lagent.config import reset_settings_cache

        reset_settings_cache()
        try:
            async with session_scope() as session:
                session.add(
                    AuditLog(
                        action="auth.login",
                        actor_name="甲",
                        created_at=now_local() - dt.timedelta(days=400),
                    )
                )
            capsys.readouterr()
            assert await _sweep() == 1
            out = capsys.readouterr().out
            assert "失败" in out
            # 一个任务失败不能把其它任务一起拖停 —— 退出码要反映"有失败"，
            # 但成功的那两项仍应照常完成。
            assert "✓" in out
        finally:
            reset_settings_cache()
