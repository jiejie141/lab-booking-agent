"""人员准入：预约资格校验、单次通行、容量不变式、未预约者拦截。

这一组测试要证明的不是"门禁接口能返回 200"，而是四件具体的事：

1. **没预约就进不去**，而且被拒这件事**有记录**（"限制未预约者进入"的证据）；
2. **一张凭证只能进一次**，且**人卡必须一致**（否则等于没有门禁）；
3. **容量在并发下不会被突破** —— 靠数据库唯一索引，不靠应用层数人数；
4. **资质看的是有效期**，不是"有没有授权过"。

第 3 点与设备预约的区间不变式是同一类问题，也是本组里最该被钉死的一条：
它有个"看起来也对"的错误实现，就是先查当前人数再决定放不放行。
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy import func, select

from lagent.clock import now_local
from lagent.db import session_scope
from lagent.domain.access import (
    ENTRY_GRACE_MINUTES,
    LAB_BASIC_CERT,
    LabFullError,
    cert_states_for,
    claim_seats,
    evaluate_entry,
    expire_stale_permits,
    find_inside_permit,
    inside_count,
    issue_permit,
    required_certs_for_lab,
    revoke_permit,
    verify_entry,
    verify_exit,
)
from lagent.models import (
    ACCESS_DENIED,
    ACCESS_GRANTED,
    DENY_ALREADY_INSIDE,
    DENY_CERT_EXPIRED,
    DENY_CERT_MISSING,
    DENY_CERT_REVOKED,
    DENY_IDENTITY_MISMATCH,
    DENY_LAB_FULL,
    DENY_NO_PERMIT,
    DENY_NOT_YET,
    DENY_PERMIT_USED,
    DENY_WRONG_LAB,
    PERMIT_CHECKED_IN,
    PERMIT_ISSUED,
    PERMIT_REVOKED,
    PERMIT_USED,
    AccessEvent,
    CertGrant,
    EntryPermit,
    LabOccupancy,
    User,
    slot_index_of,
)

# 种子数据：分析楼301(id=1,容量6,含光谱/色谱设备)、生物楼205(id=2,容量4)、
# 材料楼412(id=3,容量2,含高速离心机 -> 需要「离心」资质)
LAB_ANALYSIS = 1
LAB_BIO = 2
LAB_CENTRIFUGE = 3

ZHANGWEI, LINA, ADMIN = 1, 2, 3

WINDOW_START = dt.time(14, 0)
WINDOW_END = dt.time(16, 0)


def today() -> dt.date:
    return now_local().date()


def at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(today(), dt.time(hour, minute))


def live_window() -> tuple[dt.time, dt.time]:
    """给「走真实时钟」的接口用例用的有效时间窗。

    走 HTTP 的用例没有地方注入 ``now`` —— 接口层直接取 ``now_local()``。
    如果这类用例把窗口写死成 14:00-16:00，那它就**只在下午两点到四点之间是绿的**：
    其余时段跑必然 ``permit_expired``。这种"看时段才过"的测试比没有测试更糟 ——
    真回归来的时候，你会以为又是环境噪音而忽略掉。所以窗口从当前时刻推出来。
    """
    now = now_local()
    start = (now - dt.timedelta(hours=1)).time()
    end = (now + dt.timedelta(hours=1)).time()
    if start > end:  # 跨过午夜：退化成"今天整天有效"
        start, end = dt.time(0, 0), dt.time(23, 59, 59)
    return start, end


def live_window_json() -> dict[str, str]:
    start, end = live_window()
    return {
        "valid_from": start.strftime("%H:%M:%S"),
        "valid_to": end.strftime("%H:%M:%S"),
    }


# ---------------------------------------------------------------------------
# 帮助函数：大多数用例只需要"给某人发一张凭证"
# ---------------------------------------------------------------------------
async def grant(
    *,
    user_id: int,
    lab_id: int = LAB_ANALYSIS,
    date_: dt.date | None = None,
    start: dt.time = WINDOW_START,
    end: dt.time = WINDOW_END,
    required: list[str] | None = None,
) -> tuple[int, str]:
    """签发凭证，返回 (permit_id, 明文凭证)。"""
    async with session_scope() as session:
        permit, plain = await issue_permit(
            session,
            user_id=user_id,
            lab_id=lab_id,
            date_=date_ or today(),
            valid_from=start,
            valid_to=end,
            required_certs=required,
        )
        return permit.id, plain


async def enter(
    *,
    lab_id: int = LAB_ANALYSIS,
    credential: str | None = None,
    user_id: int | None = None,
    when: dt.datetime | None = None,
    gate: str = "gate-01",
):
    async with session_scope() as session:
        return await verify_entry(
            session,
            lab_id=lab_id,
            now=when or at(14, 0),
            credential=credential,
            identity_user_id=user_id,
            gate_id=gate,
        )


async def make_user(*, certs: list[str], name: str) -> int:
    """造一个只有裸 ``User.certs`` 的用户，返回 id。

    刻意用裸列表而不是 ``CertGrant``：容量类用例需要多个不同的人，
    顺带把"存量老数据仍可通行"这条兼容路径一起覆盖掉。
    """
    async with session_scope() as session:
        session.add(User(
            username=name, email=f"{name}@example.com",
            role="user", certs=certs, password_hash="",
        ))
        await session.flush()
        user = await session.scalar(select(User).where(User.username == name))
        assert user is not None
        return user.id


async def set_capacity(lab_id: int, capacity: int) -> None:
    """改房间容量。门禁正确性必须与容量数值无关，所以要能随手改小来压测。"""
    from sqlalchemy import text as sql_text

    async with session_scope() as session:
        await session.execute(
            sql_text("UPDATE laboratories SET capacity = :c WHERE id = :i"),
            {"c": capacity, "i": lab_id},
        )


async def occupancy_rows(permit_id: int) -> int:
    async with session_scope() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(LabOccupancy)
                .where(LabOccupancy.permit_id == permit_id)
            )
            or 0
        )


async def permit_status(permit_id: int) -> str:
    async with session_scope() as session:
        permit = await session.get(EntryPermit, permit_id)
        assert permit is not None
        return permit.status


async def events(**filters) -> list[AccessEvent]:
    async with session_scope() as session:
        stmt = select(AccessEvent).order_by(AccessEvent.id)
        for column, value in filters.items():
            stmt = stmt.where(getattr(AccessEvent, column) == value)
        return list((await session.execute(stmt)).scalars().all())


# ===========================================================================
# 一、资格要求是怎么算出来的
# ===========================================================================
class TestRequirement:
    async def test_basic_safety_cert_is_always_required(self, isolated_db):
        """进任何实验室都要基础安全资质，设备类别资质按房间内受控设备叠加。"""
        async with session_scope() as session:
            assert await required_certs_for_lab(session, LAB_BIO) == [
                LAB_BASIC_CERT,
                "细胞培养",
            ]

    async def test_category_certs_follow_the_equipment_in_the_room(self, isolated_db):
        """房间的类别要求来自房内的受控设备，不是拍脑袋写死的。"""
        async with session_scope() as session:
            certs = await required_certs_for_lab(session, LAB_ANALYSIS)
        assert certs[0] == LAB_BASIC_CERT
        # 分析楼里有需要培训的光谱/色谱设备，所以会叠加
        assert set(certs) == {LAB_BASIC_CERT, "光谱", "色谱"}

    async def test_requirement_is_frozen_on_the_permit(self, isolated_db):
        """签发后要求被冻结：之后房间规则变了，不该追溯改变已发出凭证的条件。"""
        pid, plain = await grant(user_id=LINA, lab_id=LAB_BIO)
        async with session_scope() as session:
            permit = await session.get(EntryPermit, pid)
            assert permit is not None
            assert permit.required_certs == [LAB_BASIC_CERT, "细胞培养"]
            # 即使是"没人要的类别"，也按签发时的要求判定
            assert await find_inside_permit(session, user_id=LINA) is None
        assert plain  # 明文只在签发时拿到


# ===========================================================================
# 二、未预约者：进不去，而且有记录
# ===========================================================================
class TestUnbooked:
    async def test_user_without_permit_is_denied(self, isolated_db):
        """★ 没有凭证就是进不去。这是「未预约者不得进入」的核心一道。"""
        decision = await enter(user_id=ZHANGWEI)
        assert decision.denied
        assert decision.reason_code == DENY_NO_PERMIT
        assert "预约" in decision.message  # 提示用户下一步该做什么

    async def test_denial_is_recorded_as_evidence(self, isolated_db):
        """★ 被拒必须留痕，否则"限制未预约者进入"拿不出任何证据。

        这一条很容易漏：拒绝路径 if 一 return 就走了，事件忘了写。
        事后来看就是"门禁好像拦了，但查不到谁试过"。
        """
        await enter(user_id=ZHANGWEI, gate="gate-09")
        denied = await events(result=ACCESS_DENIED)
        assert len(denied) == 1
        assert denied[0].reason_code == DENY_NO_PERMIT
        assert denied[0].user_id == ZHANGWEI
        assert denied[0].gate_id == "gate-09"
        assert denied[0].direction == "in"

    async def test_denied_attempt_occupies_nothing(self, isolated_db):
        """被拒不能占座 —— 否则"进不去的人"会把房间座位占满，容量凭空泄漏。"""
        await enter(user_id=ZHANGWEI)
        async with session_scope() as session:
            total = await session.scalar(
                select(func.count()).select_from(LabOccupancy)
            )
        assert total == 0

    async def test_unknown_credential_is_denied(self, isolated_db):
        """乱编一个凭证串：查不到 → 没有凭证。不能因为"格式像"就放行。"""
        decision = await enter(credential="not-a-real-credential", user_id=ZHANGWEI)
        assert decision.denied
        assert decision.reason_code == DENY_NO_PERMIT


# ===========================================================================
# 三、凭证语义：单次通行、绑身份、认房间
# ===========================================================================
class TestPermitSemantics:
    async def test_valid_permit_opens_the_door(self, isolated_db):
        pid, plain = await grant(user_id=LINA)
        decision = await enter(credential=plain, user_id=LINA)
        assert decision.ok, decision.message
        assert decision.permit_id == pid
        assert await permit_status(pid) == PERMIT_CHECKED_IN

    async def test_permit_is_single_use(self, isolated_db):
        """★ 一次通行。凭证不是身份标识 —— 否则截图转发就等于放行所有人。

        这里专门验"换个人拿着同一张凭证来刷"：凭证已核销，必须被拒。
        """
        pid, plain = await grant(user_id=LINA)
        assert (await enter(credential=plain, user_id=LINA)).ok

        # 李娜自己再刷一次（同一房间）—— 已核销
        self_again = await enter(credential=plain, user_id=LINA)
        assert self_again.denied

        # 张伟拿着这张（李娜的、已核销的）凭证来刷
        other = await enter(credential=plain, user_id=ZHANGWEI)
        assert other.denied
        assert other.reason_code in (DENY_PERMIT_USED, DENY_IDENTITY_MISMATCH)
        assert pid

    async def test_reused_permit_after_exit_is_still_denied(self, isolated_db):
        """出门之后凭证不许再用。"用完就撕"比"出门后还能再进"安全得多。"""
        pid, plain = await grant(user_id=LINA)
        assert (await enter(credential=plain, user_id=LINA)).ok
        async with session_scope() as session:
            assert (await verify_exit(session, user_id=LINA)).ok
        assert await permit_status(pid) == PERMIT_USED

        again = await enter(credential=plain, user_id=LINA)
        assert again.denied
        assert again.reason_code == DENY_PERMIT_USED

    async def test_identity_mismatch_is_denied(self, isolated_db):
        """★ 人卡不一致 = 替人刷卡。凭证绑账号，门口站的是人，两者必须对得上。"""
        _, plain = await grant(user_id=LINA)
        decision = await enter(credential=plain, user_id=ZHANGWEI)
        assert decision.denied
        assert decision.reason_code == DENY_IDENTITY_MISMATCH
        assert decision.extra.get("permit_owner") == LINA

    async def test_permit_for_another_lab_is_denied(self, isolated_db):
        """房间要对得上。跨房间刷同一张凭证是典型试探。"""
        _, plain = await grant(user_id=LINA, lab_id=LAB_ANALYSIS)
        decision = await enter(lab_id=LAB_CENTRIFUGE, credential=plain, user_id=LINA)
        assert decision.denied
        assert decision.reason_code == DENY_WRONG_LAB

    async def test_early_arrival_within_grace_is_allowed(self, isolated_db):
        """提前一点到场是常态，留宽限；但宽限不能变成"提前一小时也行"。"""
        _, plain = await grant(user_id=LINA, start=dt.time(14, 0))
        early = at(14, 0) - dt.timedelta(minutes=ENTRY_GRACE_MINUTES)
        early_ok = await enter(credential=plain, user_id=LINA, when=early)
        assert early_ok.ok, early_ok.message
        # 再早一分钟就越过宽限了
        too_early = early - dt.timedelta(minutes=1)
        assert (await enter(credential=plain, user_id=LINA, when=too_early)).denied

    async def test_too_early_is_denied(self, isolated_db):
        _, plain = await grant(user_id=LINA, start=dt.time(14, 0))
        decision = await enter(credential=plain, user_id=LINA, when=at(12, 0))
        assert decision.denied
        assert decision.reason_code == DENY_NOT_YET

    async def test_late_arrival_after_valid_to_is_denied(self, isolated_db):
        """过期不宽限。允许"晚几分钟没事"，有效期这个概念就不成立了。"""
        _, plain = await grant(user_id=LINA, end=dt.time(16, 0))
        decision = await enter(credential=plain, user_id=LINA, when=at(16, 30))
        assert decision.denied
        assert decision.reason_code != ""

    async def test_permit_from_another_day_is_denied(self, isolated_db):
        """隔夜凭证一律无效：不做跨天宽限，否则"当天有效"不可解释。"""
        _, plain = await grant(
            user_id=LINA, date_=today() + dt.timedelta(days=1)
        )
        decision = await enter(credential=plain, user_id=LINA, when=at(14, 0))
        assert decision.denied

    async def test_precheck_has_no_side_effects(self, isolated_db):
        """预检（check_in=False）不能核销、不能占座 ——

        否则门禁屏上"你随时可以进"的这一次显示就把凭证用掉了。
        """
        pid, plain = await grant(user_id=LINA)
        async with session_scope() as session:
            decision = await verify_entry(
                session, lab_id=LAB_ANALYSIS, now=at(14, 0),
                credential=plain, identity_user_id=LINA, check_in=False,
            )
        assert decision.ok
        assert await permit_status(pid) == PERMIT_ISSUED
        assert await occupancy_rows(pid) == 0

    async def test_card_swipe_finds_permit_without_credential(self, isolated_db):
        """刷卡场景：没带二维码，按人 + 房间找当刻可用凭证。"""
        pid, _ = await grant(user_id=LINA)
        decision = await enter(user_id=LINA)
        assert decision.ok
        assert decision.permit_id == pid


# ===========================================================================
# 四、资质：看的是"今天还有效吗"
# ===========================================================================
class TestCertValidity:
    async def test_expired_cert_blocks_entry(self, isolated_db):
        """★ 三年前考过的证不能今天还管用。

        种子给的是远期有效授权，这里追加一条**更晚授予但已过期**的记录：
        "以最新授予的那条为准"这条规则会选到它，于是判定为过期。
        """
        _, plain = await grant(user_id=ZHANGWEI, lab_id=LAB_CENTRIFUGE)
        async with session_scope() as session:
            session.add(CertGrant(
                user_id=ZHANGWEI, category="离心",
                granted_at=today() - dt.timedelta(days=800),
                expires_at=today() - dt.timedelta(days=30),
                evidence="test:expired",
            ))
        decision = await enter(lab_id=LAB_CENTRIFUGE, credential=plain, user_id=ZHANGWEI)
        assert decision.denied
        assert decision.reason_code == DENY_CERT_EXPIRED
        assert "复训" in decision.message

    async def test_revoked_cert_blocks_entry_with_its_own_reason(self, isolated_db):
        """撤销与过期给不同原因码：处置动作不同（找管理员 vs 去复训）。"""
        await grant(user_id=ZHANGWEI, lab_id=LAB_CENTRIFUGE)
        async with session_scope() as session:
            session.add(CertGrant(
                user_id=ZHANGWEI, category="离心",
                granted_at=today(),
                expires_at=today() + dt.timedelta(days=365),
                revoked_at=now_local(),
                evidence="test:revoked",
            ))
        pid2, plain2 = await grant(user_id=ZHANGWEI, lab_id=LAB_CENTRIFUGE)
        decision = await enter(lab_id=LAB_CENTRIFUGE, credential=plain2, user_id=ZHANGWEI)
        assert decision.denied
        assert decision.reason_code == DENY_CERT_REVOKED
        assert "管理员" in decision.message
        assert pid2

    async def test_missing_cert_blocks_entry(self, isolated_db):
        """张伟只有「光谱」：进离心机间应当被资质拦下（不是被容量或凭据拦下）。"""
        _, plain = await grant(user_id=ZHANGWEI, lab_id=LAB_CENTRIFUGE)
        decision = await enter(lab_id=LAB_CENTRIFUGE, credential=plain, user_id=ZHANGWEI)
        assert decision.denied
        assert decision.reason_code == DENY_CERT_MISSING

    async def test_expiry_is_evaluated_against_today(self, isolated_db):
        """资质有效性必须按"今天"算，而不是按签发日算 —— 否则签发时就一次性判完了。"""
        async with session_scope() as session:
            session.add(CertGrant(
                user_id=ZHANGWEI, category="离心",
                granted_at=today() - dt.timedelta(days=10),
                expires_at=today() - dt.timedelta(days=1),
            ))
            states = await cert_states_for(
                session, user_id=ZHANGWEI, categories=["离心"], today=today()
            )
            future = await cert_states_for(
                session,
                user_id=ZHANGWEI,
                categories=["离心"],
                today=today() + dt.timedelta(days=365),
            )
        assert states["离心"].state == "expired"
        assert states["离心"].expires_at == today() - dt.timedelta(days=1)
        assert future["离心"].state == "expired"

    async def test_legacy_cert_list_still_grants_access(self, isolated_db):
        """存量兼容：只有 ``User.certs`` 裸列表、没有授权记录的用户也能进。

        直接只认新表，会让所有老用户在升级当天一起进不了门 ——
        功能是对的，但"上线即全站不可用"。所以老数据按"永久有效"过渡，
        并由这条测试把该行为**显式钉住**（而不是默默生效）。
        """
        async with session_scope() as session:
            session.add(User(
                username="老用户", email="legacy@example.com",
                role="user", certs=["实验室安全", "离心"], password_hash="",
            ))
            await session.flush()
            user = await session.scalar(
                select(User).where(User.username == "老用户")
            )
            assert user is not None
            legacy_id = user.id
            states = await cert_states_for(
                session, user_id=legacy_id, categories=["离心", "光谱"], today=today()
            )
        assert states["离心"].state == "ok"          # 裸列表里有
        assert states["光谱"].state == "missing"      # 裸列表里没有

        _, plain = await grant(user_id=legacy_id, lab_id=LAB_CENTRIFUGE)
        decision = await enter(lab_id=LAB_CENTRIFUGE, credential=plain, user_id=legacy_id)
        assert decision.ok, decision.message


# ===========================================================================
# 五、容量：★ 这组是本文件的核心
# ===========================================================================
class TestCapacityInvariant:
    async def test_seats_are_claimed_for_every_slot_in_the_window(self, isolated_db):
        """14:00-16:00 按 30 分钟粒度展开成 4 格，占满才算占住整个时段。"""
        pid, _ = await grant(user_id=LINA, start=dt.time(14, 0), end=dt.time(16, 0))
        async with session_scope() as session:
            placed = await claim_seats(
                session, lab_id=LAB_ANALYSIS, date_=today(),
                start=dt.time(14, 0), end=dt.time(16, 0),
                permit_id=pid, capacity=6,
            )
        assert placed == 4
        assert await occupancy_rows(pid) == 4

    async def test_partial_claim_rolls_back_completely(self, isolated_db):
        """★ 占用必须"整段成功或整段失败"，绝不能占了一半留着。

        构造：容量 1 的房间，先让 A 占住 14:00-16:00；再让 B 申请
        13:30-15:00 —— 它的第一格是空的、后面几格已满。
        如果实现是"逐格插，失败就返回"，B 会永久占着 13:30 那一格，
        而它其实没进得去：房间从此显示"该格已满"却没人。
        """
        # 材料楼412 种子里容量是 2，这里必须先压到 1 ——
        # 否则 B 会被安排到第二个座位，验不到"整段回滚"。
        await set_capacity(LAB_CENTRIFUGE, 1)
        pid_a, plain_a = await grant(
            user_id=LINA, lab_id=LAB_CENTRIFUGE,
            start=dt.time(14, 0), end=dt.time(16, 0),
        )
        assert (await enter(lab_id=LAB_CENTRIFUGE, credential=plain_a, user_id=LINA)).ok

        pid_b, plain_b = await grant(
            user_id=ADMIN, lab_id=LAB_CENTRIFUGE,
            start=dt.time(13, 30), end=dt.time(15, 0),
        )
        decision = await enter(
            lab_id=LAB_CENTRIFUGE, credential=plain_b, user_id=ADMIN,
            when=at(13, 35),
        )
        assert decision.denied
        assert decision.reason_code == DENY_LAB_FULL

        # B 一格都不许留下，凭证也不能被标记成已入场
        assert await occupancy_rows(pid_b) == 0
        assert await permit_status(pid_b) == PERMIT_ISSUED
        async with session_scope() as session:
            assert await inside_count(
                session, LAB_CENTRIFUGE, today(), slot_index_of(dt.time(13, 30))
            ) == 0
        assert pid_a

    async def test_capacity_is_respected_sequentially(self, isolated_db):
        """容量 2 的房间：第 3 个人进不来。

        注意不能拿张伟来凑人数 —— 他只有「光谱」资质，会在**资质那道闸门**
        就被拦下，于是测试变成"验资质"而不是"验容量"，
        哪怕容量实现是坏的也照样绿。这类"用错前提把测试测歪"最容易被放过。
        """
        extra = await make_user(certs=[LAB_BASIC_CERT, "离心"], name="容量测试员")
        plaintexts = []
        for user_id in (LINA, ADMIN, extra):
            _, plain = await grant(user_id=user_id, lab_id=LAB_CENTRIFUGE)
            plaintexts.append((user_id, plain))

        results = []
        for user_id, plain in plaintexts:
            results.append(await enter(lab_id=LAB_CENTRIFUGE, credential=plain, user_id=user_id))

        assert [r.ok for r in results] == [True, True, False]
        assert results[2].reason_code == DENY_LAB_FULL

    async def test_concurrent_entry_never_exceeds_capacity(self, isolated_db):
        """★★ 核心断言：N 个并发抢一个容量 1 的房间，恰好 1 个进得去。

        这条与设备预约的"30 并发抢同一时段恰好 1 成功"是同一类验证。
        如果容量是用"先查当前人数、再决定放行"实现的，这里必然放进去多个 ——
        两个请求会读到同一个旧人数。

        注意并发的是**不同的人、不同的凭证**：这才是真实门禁的高峰形态
        （下课铃响，一群人同时刷卡）。
        """
        await set_capacity(LAB_CENTRIFUGE, 1)

        # 造 12 个人（重复用 3 个账号会被"不可同时在馆"提前拦掉，
        # 那是另一条闸门，会把本测试要验的容量问题掩盖掉）
        user_ids = []
        async with session_scope() as session:
            for i in range(12):
                session.add(User(
                    username=f"并发用户{i}", email=f"race{i}@example.com",
                    role="user", certs=[LAB_BASIC_CERT, "离心"], password_hash="",
                ))
            await session.flush()
            rows = await session.execute(
                select(User.id).where(User.username.like("并发用户%"))
            )
            user_ids = [r[0] for r in rows.all()]

        permits = []
        for uid in user_ids:
            _, plain = await grant(user_id=uid, lab_id=LAB_CENTRIFUGE)
            permits.append((uid, plain))

        async def attempt(uid: int, plain: str):
            async with session_scope() as session:
                return await verify_entry(
                    session, lab_id=LAB_CENTRIFUGE, now=at(14, 0),
                    credential=plain, identity_user_id=uid, gate_id="gate-race",
                )

        results = await asyncio.gather(
            *(attempt(uid, plain) for uid, plain in permits),
            return_exceptions=True,
        )
        failures = [r for r in results if isinstance(r, BaseException)]
        granted = [r for r in results if not isinstance(r, BaseException) and r.ok]

        assert not failures, f"并发核验抛异常（事务/锁问题）：{failures[:2]}"
        assert len(granted) == 1, f"容量 1 却有 {len(granted)} 个人进去了"
        assert all(
            r.reason_code in (DENY_LAB_FULL, DENY_ALREADY_INSIDE)
            for r in results
            if not isinstance(r, BaseException) and not r.ok
        )

        async with session_scope() as session:
            total = await session.scalar(
                select(func.count()).select_from(LabOccupancy)
            )
        assert total == 4, f"容量 1 × 4 格应为 4 条占位，实得 {total}"

    async def test_exit_releases_capacity(self, isolated_db):
        """出门销账 → 座位释放 → 后面的人能进。

        这是"刷卡出门"这件事的价值所在：不做出场，容量就永不回收，
        房间会在一天之内被"进过的人"占满。
        """
        # 材料楼412 容量 2
        extra = await make_user(certs=[LAB_BASIC_CERT, "离心"], name="出场测试员")
        pid_a, plain_a = await grant(user_id=extra, lab_id=LAB_CENTRIFUGE)
        pid_b, plain_b = await grant(user_id=LINA, lab_id=LAB_CENTRIFUGE)
        pid_c, plain_c = await grant(user_id=ADMIN, lab_id=LAB_CENTRIFUGE)

        assert (await enter(lab_id=LAB_CENTRIFUGE, credential=plain_a, user_id=extra)).ok
        assert (await enter(lab_id=LAB_CENTRIFUGE, credential=plain_b, user_id=LINA)).ok

        third = await enter(lab_id=LAB_CENTRIFUGE, credential=plain_c, user_id=ADMIN)
        assert third.denied and third.reason_code == DENY_LAB_FULL

        # 第一个人出门 → 座位释放
        async with session_scope() as session:
            assert (await verify_exit(session, user_id=extra)).ok
        assert await occupancy_rows(pid_a) == 0

        fourth = await enter(lab_id=LAB_CENTRIFUGE, credential=plain_c, user_id=ADMIN)
        assert fourth.ok, fourth.message
        assert pid_b and pid_c

    async def test_zero_capacity_lab_rejects_everyone(self, isolated_db):
        """容量为 0 的房间必须谁都进不去，而不是 IndexError 或静默放行。"""
        await set_capacity(LAB_ANALYSIS, 0)
        _, plain = await grant(user_id=LINA)
        decision = await enter(credential=plain, user_id=LINA)
        assert decision.denied
        assert decision.reason_code == DENY_LAB_FULL

    async def test_claim_seats_raises_when_full(self, isolated_db):
        """底层函数自己也要守住容量，不能只靠上层调用顺序正确。"""
        pid_a, _ = await grant(user_id=LINA, lab_id=LAB_CENTRIFUGE)
        pid_b, _ = await grant(user_id=ADMIN, lab_id=LAB_CENTRIFUGE)
        async with session_scope() as session:
            await claim_seats(
                session, lab_id=LAB_CENTRIFUGE, date_=today(),
                start=WINDOW_START, end=WINDOW_END, permit_id=pid_a, capacity=1,
            )
            with pytest.raises(LabFullError):
                await claim_seats(
                    session, lab_id=LAB_CENTRIFUGE, date_=today(),
                    start=WINDOW_START, end=WINDOW_END, permit_id=pid_b, capacity=1,
                )
        assert pid_b

    async def test_unaligned_window_still_protects_its_tail(self, isolated_db):
        """★ 未对齐的凭证窗口，尾巴那一小段也必须受容量保护。

        这条来自一个**被自己的演示暴露出来**的漏洞：``slot_index_of()`` 对未对齐的
        时间点是向下取整的，所以 14:10-15:10 的凭证只会占掉 [14:00,15:00) 两格，
        而 15:05 刷卡落在第 30 格 —— 不在占位集合里。容量 1 的房间于是在
        "A 的凭证还剩 5 分钟"的窗口里放进了第二个人，超容且无人察觉。

        构造：A 的窗口 14:10-15:10（未对齐），B 的窗口 15:00-16:00（对齐）。
        A 在 14:20 进场后，B 在 15:05 进场必须是 lab_full —— 那一刻 A 名义上还在里面。
        修复（窗口向外吸附到网格）之前，B 会被放行。
        """
        await set_capacity(LAB_ANALYSIS, 1)
        _, plain_a = await grant(user_id=LINA, start=dt.time(14, 10), end=dt.time(15, 10))
        _, plain_b = await grant(user_id=ADMIN, start=dt.time(15, 0), end=dt.time(16, 0))

        first = await enter(credential=plain_a, user_id=LINA, when=at(14, 20))
        assert first.ok, first.message

        second = await enter(credential=plain_b, user_id=ADMIN, when=at(15, 5))
        assert second.denied, "A 还在馆内，容量 1 的房间不该放第二个人进来"
        assert second.reason_code == DENY_LAB_FULL

    async def test_claim_seats_covers_unaligned_tail_grid(self, isolated_db):
        """底层直证：未对齐窗口向外吸附后，窗口内的每一格都被占住。

        14:10-15:10 应当覆盖第 28/29/30 三格（14:00-15:30），而不是 2 格。
        """
        pid, _ = await grant(user_id=LINA)
        async with session_scope() as session:
            count = await claim_seats(
                session,
                lab_id=LAB_ANALYSIS,
                date_=today(),
                start=dt.time(14, 10),
                end=dt.time(15, 10),
                permit_id=pid,
                capacity=5,
            )
        assert count == 3
        assert await occupancy_rows(pid) == 3

    async def test_aligned_window_is_not_widened(self, isolated_db):
        """吸附只补未对齐的尾巴，**不能**把对齐窗口顺手撑大。

        14:00-16:00 必须仍然是 4 格。否则"多占一格"就从安全余量变成了系统性浪费：
        每张凭证都白占半格，容量会被无谓地压低。
        """
        pid, _ = await grant(user_id=LINA)
        async with session_scope() as session:
            count = await claim_seats(
                session,
                lab_id=LAB_ANALYSIS,
                date_=today(),
                start=dt.time(14, 0),
                end=dt.time(16, 0),
                permit_id=pid,
                capacity=5,
            )
        assert count == 4


# ===========================================================================
# 六、在馆状态与撤销
# ===========================================================================
class TestPresence:
    async def test_cannot_be_inside_two_labs_at_once(self, isolated_db):
        """★ 一个人不能同时"在馆"于两个房间。

        既是统计问题（人数被算两遍、容量失真），也是安全问题：
        说不出一个人此刻在哪，出事时找不到人。
        """
        pid1, plain1 = await grant(user_id=LINA, lab_id=LAB_ANALYSIS)
        pid2, plain2 = await grant(user_id=LINA, lab_id=LAB_BIO)
        assert (await enter(lab_id=LAB_ANALYSIS, credential=plain1, user_id=LINA)).ok
        second = await enter(lab_id=LAB_BIO, credential=plain2, user_id=LINA)
        assert second.denied
        assert second.reason_code == DENY_ALREADY_INSIDE
        assert second.extra.get("inside_permit_id") == pid1
        assert pid2

    async def test_exit_then_enter_another_lab(self, isolated_db):
        """出场之后可以去别的房间 —— 这条闸门管的是"同时"，不是"当天只能进一个"。"""
        _, plain1 = await grant(user_id=LINA, lab_id=LAB_ANALYSIS)
        _, plain2 = await grant(user_id=LINA, lab_id=LAB_BIO)
        assert (await enter(lab_id=LAB_ANALYSIS, credential=plain1, user_id=LINA)).ok
        async with session_scope() as session:
            assert (await verify_exit(session, user_id=LINA)).ok
        assert (await enter(lab_id=LAB_BIO, credential=plain2, user_id=LINA,
                            when=at(15, 0))).ok

    async def test_exit_without_entry_is_denied(self, isolated_db):
        """没进过就不能出 —— 否则会伪造出一条不存在的在馆记录。"""
        async with session_scope() as session:
            decision = await verify_exit(session, user_id=ZHANGWEI)
        assert decision.denied

    async def test_revoke_blocks_further_entry_and_frees_seats(self, isolated_db):
        """撤销凭证：座位立刻释放（容量不被僵尸凭证占着），凭证不能再入场。"""
        pid, plain = await grant(user_id=LINA)
        assert (await enter(credential=plain, user_id=LINA)).ok
        async with session_scope() as session:
            revoked = await revoke_permit(session, permit_id=pid, reason="测试撤销")
            assert revoked is not None
        assert await permit_status(pid) == PERMIT_REVOKED
        assert await occupancy_rows(pid) == 0

        again = await enter(credential=plain, user_id=LINA)
        assert again.denied

    async def test_granted_event_is_recorded_on_success(self, isolated_db):
        """放行也要留痕：门开了查不到是谁开的，等于没有记录。"""
        _, plain = await grant(user_id=LINA)
        await enter(credential=plain, user_id=LINA)
        granted = await events(result=ACCESS_GRANTED)
        assert len(granted) == 1
        assert granted[0].user_id == LINA
        assert granted[0].permit_id is not None

    async def test_expire_stale_permits_marks_unused_ones(self, isolated_db):
        """到点没用掉的凭证标记为过期。这只是展示状态的清理，不承担正确性。"""
        pid, plain = await grant(
            user_id=LINA, start=dt.time(8, 0), end=dt.time(9, 0)
        )
        async with session_scope() as session:
            changed = await expire_stale_permits(session, now=at(12, 0))
        assert changed >= 1
        assert await permit_status(pid) == "expired"
        # 即使清理任务没跑，核验也会拒 —— 正确性不依赖这个批处理
        decision = await enter(credential=plain, user_id=LINA, when=at(12, 0))
        assert decision.denied


# ===========================================================================
# 七、纯函数判定：可单测、可离线复算
# ===========================================================================
class TestPureDecision:
    def test_evaluate_entry_is_a_pure_function(self):
        """判定不碰数据库：给同一份事实，永远得到同一结论。"""
        from lagent.domain.access import CertState, EntryContext

        permit = EntryPermit(
            user_id=7, lab_id=1, date=today(),
            valid_from=dt.time(14, 0), valid_to=dt.time(16, 0),
            status=PERMIT_ISSUED, credential_hash="x",
            required_certs=[LAB_BASIC_CERT],
        )
        ctx = EntryContext(
            now=at(14, 30), lab_id=1, identity_user_id=7, permit=permit,
            cert_states={LAB_BASIC_CERT: CertState("ok")},
        )
        first = evaluate_entry(ctx)
        second = evaluate_entry(ctx)
        assert first == second
        assert first.ok

    def test_evaluate_entry_denies_without_permit(self):
        from lagent.domain.access import EntryContext

        ctx = EntryContext(now=at(14, 0), lab_id=1, identity_user_id=7)
        decision = evaluate_entry(ctx)
        assert decision.reason_code == DENY_NO_PERMIT


# ===========================================================================
# 八、门禁接口：谁能调、拒绝能不能被门禁屏读到
# ===========================================================================
class TestGateApi:
    async def test_anonymous_cannot_call_the_gate(self, http):
        """★ 这个接口能决定"开不开门"，绝不能匿名可用。"""
        resp = await http.post("/api/access/verify", json={"lab_id": LAB_ANALYSIS})
        assert resp.status_code == 401

    async def test_ordinary_user_cannot_impersonate_the_gate(self, http, as_user):
        """普通用户令牌也不行 —— 否则任何一个学生都能自己给自己开门。

        门禁机是**设备身份**（走设备密钥），不是"某个用户"。
        """
        headers = await as_user("李娜")
        resp = await http.post(
            "/api/access/verify", json={"lab_id": LAB_ANALYSIS}, headers=headers
        )
        assert resp.status_code == 401

    async def test_admin_can_verify_and_gets_a_reason_code(self, http, as_user):
        """管理员可代门禁核验（排障用），且拒绝原因可读。"""
        headers = await as_user("管理员")
        resp = await http.post(
            "/api/access/verify",
            json={"lab_id": LAB_ANALYSIS, "user_id": ZHANGWEI, "gate_id": "gate-t1"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["granted"] is False
        # 张伟没有凭证 —— 但原因码与提示语必须具体，门禁屏要能照着显示
        assert body["reason_code"] == DENY_NO_PERMIT
        assert body["message"]

    async def test_admin_can_issue_then_gate_grants_entry(self, http, as_user):
        """★ 端到端：管理员发凭证 → 门禁核验放行。这是真实主链路。"""
        admin = await as_user("管理员")
        issued = await http.post(
            "/api/access/issue",
            json={
                "user_id": LINA,
                "lab_id": LAB_BIO,
                "date": today().isoformat(),
                **live_window_json(),
                "reason": "端到端测试",
            },
            headers=admin,
        )
        assert issued.status_code == 200, issued.text
        payload = issued.json()
        assert payload["credential"], "明文凭证必须返回，否则用户拿不到它"
        assert payload["permit_id"] > 0

        verify = await http.post(
            "/api/access/verify",
            json={
                "lab_id": LAB_BIO,
                "credential": payload["credential"],
                "user_id": LINA,
                "gate_id": "gate-e2e",
            },
            headers=admin,
        )
        assert verify.status_code == 200, verify.text
        assert verify.json()["granted"] is True, verify.text
        assert verify.json()["lab_label"]

    async def test_precheck_does_not_consume_the_credential(self, http, as_user):
        """预检不能把凭证用掉 —— 否则"屏幕上显示能不能进"就等于进了一次。"""
        admin = await as_user("管理员")
        issued = await http.post(
            "/api/access/issue",
            json={
                "user_id": LINA, "lab_id": LAB_BIO,
                "date": today().isoformat(),
                **live_window_json(),
                "reason": "预检测试",
            },
            headers=admin,
        )
        credential = issued.json()["credential"]

        pre = await http.post(
            "/api/access/verify",
            json={"lab_id": LAB_BIO, "credential": credential, "user_id": LINA,
                  "precheck": True},
            headers=admin,
        )
        assert pre.status_code == 200 and pre.json()["granted"] is True

        real = await http.post(
            "/api/access/verify",
            json={"lab_id": LAB_BIO, "credential": credential, "user_id": LINA},
            headers=admin,
        )
        assert real.json()["granted"] is True, "预检之后凭证仍应可用"

    async def test_inside_roster_requires_admin(self, http, as_user):
        """在馆名单是敏感信息：回答"楼里现在都有谁"，只给管理员。"""
        admin = await as_user("管理员")
        lina = await as_user("李娜")

        issued = await http.post(
            "/api/access/issue",
            json={
                "user_id": LINA, "lab_id": LAB_BIO,
                "date": today().isoformat(),
                **live_window_json(),
                "reason": "在馆名单测试",
            },
            headers=admin,
        )
        await http.post(
            "/api/access/verify",
            json={"lab_id": LAB_BIO, "credential": issued.json()["credential"],
                  "user_id": LINA},
            headers=admin,
        )

        forbidden = await http.get("/api/access/inside", headers=lina)
        assert forbidden.status_code == 403

        roster = await http.get("/api/access/inside", headers=admin)
        assert roster.status_code == 200, roster.text
        body = roster.json()
        assert body["count"] >= 1
        names = [row["username"] for row in body["inside"]]
        assert "李娜" in names

    async def test_exit_through_the_api_frees_the_roster(self, http, as_user):
        """走接口出场后，在馆名单里就查不到他了。"""
        admin = await as_user("管理员")
        issued = await http.post(
            "/api/access/issue",
            json={
                "user_id": LINA, "lab_id": LAB_BIO,
                "date": today().isoformat(),
                **live_window_json(),
                "reason": "出场测试",
            },
            headers=admin,
        )
        credential = issued.json()["credential"]
        await http.post(
            "/api/access/verify",
            json={"lab_id": LAB_BIO, "credential": credential, "user_id": LINA},
            headers=admin,
        )
        out = await http.post(
            "/api/access/verify",
            json={"lab_id": LAB_BIO, "user_id": LINA, "direction": "out"},
            headers=admin,
        )
        assert out.status_code == 200 and out.json()["granted"] is True
        roster = await http.get("/api/access/inside", headers=admin)
        assert all(row["username"] != "李娜" for row in roster.json()["inside"])

    async def test_unknown_field_is_rejected(self, http, as_user):
        """严格模式：门禁集成字段名写错要**明确报错**，不能被静默忽略。"""
        admin = await as_user("管理员")
        resp = await http.post(
            "/api/access/verify",
            json={"lab_id": LAB_ANALYSIS, "labId": LAB_ANALYSIS},
            headers=admin,
        )
        assert resp.status_code == 422
