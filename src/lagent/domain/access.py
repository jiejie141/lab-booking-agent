"""人员准入：入室凭证的签发、核验、核销与在馆管理。

这个模块回答一个设备预约回答不了的问题：**「这个人此刻能不能进这个房间」**。

为什么要单独做，而不是复用设备预约：

1. **资源不同。** 设备预约约束的是「一台仪器一个时段只有一个人」；
   房间约束的是「同时最多 N 个人」。10 个人可以各自约到 10 台不同设备，
   但房间塞不下 10 个人 —— 设备的冲突判定对房间容量完全无感。
2. **时刻不同。** 设备预约是"未来某个时段归谁"；准入是"**现在**这一瞬间开不开门"。
   前者可以慢慢协商、给备选方案；后者必须在几百毫秒内给出是/否，
   而且答案要能显示在门禁屏上。
3. **身份语义不同。** 预约绑定「账号」；准入必须绑定「物理身份」。
   卡片、人脸、工号 —— 核验的必须是**站在门口的那个人**，
   否则截图转发就等于放行，等于没有门禁。

设计上的三条主线：

    ┌─ 签发：设备预约成功 ──▶ 派生入室凭证（用户只需要做一件事）
    │
    ├─ 核验：多道闸门、固定顺序、每次拒绝都给原因码
    │
    └─ 核销：单次通行 + 占座，两条不变式都由数据库兜底

**最后一点是本模块的工程核心**：容量不是靠应用层"数一下现在几个人"实现的
（那是 check-then-act，并发下必然失真），而是把容量离散成座位、
用唯一索引让数据库来裁决。详见 :class:`~lagent.models.LabOccupancy`。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..clock import now_local
from ..models import (
    ACCESS_DENIED,
    ACCESS_GRANTED,
    DEFAULT_SLOT_GRANULARITY_MINUTES,
    DENY_ALREADY_INSIDE,
    DENY_CERT_EXPIRED,
    DENY_CERT_MISSING,
    DENY_CERT_REVOKED,
    DENY_IDENTITY_MISMATCH,
    DENY_LAB_FULL,
    DENY_NO_PERMIT,
    DENY_NO_SUCH_USER,
    DENY_NOT_YET,
    DENY_PERMIT_EXPIRED,
    DENY_PERMIT_REVOKED,
    DENY_PERMIT_USED,
    DENY_WRONG_LAB,
    DIRECTION_IN,
    DIRECTION_OUT,
    LAB_BASIC_CERT,
    PERMIT_CHECKED_IN,
    PERMIT_EXPIRED,
    PERMIT_ISSUED,
    PERMIT_REVOKED,
    PERMIT_SOURCE_RESERVATION,
    PERMIT_USED,
    AccessEvent,
    CertGrant,
    EntryPermit,
    LabOccupancy,
    Laboratory,
)
from ..obs import current_request_id

# 允许比 valid_from 早多少入场。给迟到/早到留一点余量，
# 但**绝不**允许超时入场：valid_to 之后凭证即失效，不存在"晚几分钟没事"。
ENTRY_GRACE_MINUTES = 15


def _floor_slot(value: dt.time, step: int) -> int:
    """时间点所在的格（向下取整）。"""
    return (value.hour * 60 + value.minute) // step


def _ceil_slot(value: dt.time, step: int) -> int:
    """区间终点所在的格（向上取整；恰好对齐时不进位）。

    左闭右开区间 [start, end) 里，``end`` 自己那一格是**不含**的：
    14:00-16:00 与 16:00-18:00 首尾相接但不相交。所以 16:00 要回到第 32 格本身，
    而 16:10 必须算到第 33 格 —— 否则 16:10 之前那 10 分钟没人占位。
    """
    minutes = value.hour * 60 + value.minute
    exact = minutes % step == 0 and not value.second and not value.microsecond
    return minutes // step if exact else -(-minutes // step)

# 凭证串长度（字节）。24 字节 ≈ 192 bit 熵，无法被枚举。
_CREDENTIAL_BYTES = 24


# ===========================================================================
# 判定结果：门禁要的不是布尔值，是「为什么」
# ===========================================================================
@dataclass(frozen=True)
class EntryDecision:
    """一次核验的结论。

    ``ok`` 只回答开不开门；``reason_code`` 回答**为什么** —— 门禁屏要显示它，
    值班人员要照着它办事（"该复训"和"该找管理员"是两种完全不同的处置），
    事后统计也要靠它回答"哪种拒绝最多"（是没约，还是培训过期）。

    只回一句"验证失败"的门禁，等于让值班人员在门口猜原因。
    """

    ok: bool
    reason_code: str = ""
    message: str = ""
    permit_id: int | None = None
    user_id: int | None = None
    user_name: str = ""
    lab_id: int | None = None
    # 给门禁屏/前端补的上下文（例如"你的资质 2026-05-01 已过期"）
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def denied(self) -> bool:
        return not self.ok


@dataclass(frozen=True)
class CertState:
    """一个资质类别在**今天**的状态。

    带上 ``expires_at`` 是因为"你的离心机资质过期了"必须能说出**什么时候**过期的 ——
    不然用户不知道自己是三年前考的证，还是上周刚到期，处置动作完全一样但
    体验差了很远（前者要重新培训，后者可能只是没换证）。
    """

    state: str  # ok / expired / revoked / missing
    expires_at: dt.date | None = None


@dataclass(frozen=True)
class EntryContext:
    """核验所需的全部事实。**刻意做成纯数据**，不持有 session。

    这样 :func:`evaluate_entry` 是纯函数：可单测、可离线复算、
    可以在不碰数据库的情况下回答"为什么这次判定是这样"。
    真实系统里"事后复盘为什么那次没开门"是常规需求，
    如果判定逻辑和数据库查询缠在一起，就只能靠翻日志猜。
    """

    now: dt.datetime
    lab_id: int
    # 读卡 / 人脸识别到的身份。None = 物理身份还没识别出来
    identity_user_id: int | None
    permit: EntryPermit | None = None
    # 所需资质在今天的有效情况：{类别: CertState}
    cert_states: dict[str, CertState] = field(default_factory=dict)
    # 该人当前是否已在馆（在馆凭证 id）
    inside_permit_id: int | None = None
    grace_minutes: int = ENTRY_GRACE_MINUTES


# 资质失效的三种原因要分开：处置方式不同（去考证 / 找管理员 / 去复训）
_CERT_MESSAGES: dict[str, str] = {
    "expired": "你的「{cert}」资质已于 {expires_at} 到期，请先完成复训。",
    "revoked": "你的「{cert}」资质已被撤销，请联系实验室管理员。",
    "missing": "你没有「{cert}」资质，不能进入该实验室。",
}

# 资质状态 → 拒绝原因码。撤销与过期是两回事：前者是"你被停权了"，
# 后者是"到点了"，给用户的处置动作完全不同。
_DENY_BY_CERT_STATE: dict[str, str] = {
    "expired": DENY_CERT_EXPIRED,
    "revoked": DENY_CERT_REVOKED,
    "missing": DENY_CERT_MISSING,
}


def evaluate_entry(ctx: EntryContext) -> EntryDecision:
    """多闸门判定：**按固定顺序短路**，任一不过即拒。

    顺序不是随便排的，遵循两条原则：

    1. **先问「你是谁、你凭什么」，再问「装不装得下」。**
       容量判定（占座）会写库、会产生副作用；如果先占座再去查资质，
       一道本可以在内存里拒掉的请求会白白占用并需要回滚一个座位。
       更糟的是漏了回滚 —— 座位泄漏比拒绝更麻烦（房间显示满了但没人）。
    2. **不可救的问题排在前面。** "凭证已核销"是死路，没有必要再去查资质；
       而资质过期是"可救"的（复训即可），放后面。
    """
    permit = ctx.permit

    # ---- 闸门 1：有没有凭证（未预约者就卡在这一道）----
    if permit is None:
        return EntryDecision(
            ok=False,
            reason_code=DENY_NO_PERMIT,
            message="你没有该实验室的有效入室凭证。请先预约，预约成功后凭证会自动发放。",
            lab_id=ctx.lab_id,
        )

    # ---- 闸门 2：凭证本身还能不能用 ----
    if permit.status == PERMIT_REVOKED:
        return EntryDecision(
            ok=False,
            reason_code=DENY_PERMIT_REVOKED,
            message="该凭证已被撤销。",
            permit_id=permit.id,
            user_id=permit.user_id,
            lab_id=permit.lab_id,
        )
    if permit.status == PERMIT_USED or permit.checked_out_at is not None:
        # 单次通行：用过就不再可用。这是防"一张截图进十个人"的关键一道。
        return EntryDecision(
            ok=False,
            reason_code=DENY_PERMIT_USED,
            message="该凭证已经使用过了（凭证只允许通行一次）。",
            permit_id=permit.id,
            user_id=permit.user_id,
            lab_id=permit.lab_id,
        )
    if permit.status == PERMIT_EXPIRED:
        return EntryDecision(
            ok=False,
            reason_code=DENY_PERMIT_EXPIRED,
            message="该凭证已过期，请重新预约。",
            permit_id=permit.id,
            user_id=permit.user_id,
            lab_id=permit.lab_id,
        )

    # ---- 闸门 3：是不是这个房间的凭证 ----
    if permit.lab_id != ctx.lab_id:
        return EntryDecision(
            ok=False,
            reason_code=DENY_WRONG_LAB,
            message="该凭证不属于这个实验室。",
            permit_id=permit.id,
            user_id=permit.user_id,
            lab_id=ctx.lab_id,
        )

    # ---- 闸门 4：时间窗 ----
    today = ctx.now.date()
    if permit.date != today:
        # 隔夜的凭证一律无效。不做"跨天宽限"：夜班场景应当单独签发一张，
        # 而不是让昨天的凭证在今天凌晨仍然管用 —— 那会让"当天有效"这个
        # 说法变得不可解释。
        return EntryDecision(
            ok=False,
            reason_code=DENY_PERMIT_EXPIRED,
            message=f"该凭证的有效期是 {permit.date}，不是今天。",
            permit_id=permit.id,
            user_id=permit.user_id,
            lab_id=permit.lab_id,
        )

    now_t = ctx.now.time()
    earliest = _time_plus(permit.valid_from, -ctx.grace_minutes)
    if now_t < earliest:
        return EntryDecision(
            ok=False,
            reason_code=DENY_NOT_YET,
            message=(
                f"还没到入场时间。你的凭证从 {permit.valid_from:%H:%M} 开始生效"
                f"（可提前 {ctx.grace_minutes} 分钟入场）。"
            ),
            permit_id=permit.id,
            user_id=permit.user_id,
            lab_id=permit.lab_id,
        )
    if now_t > permit.valid_to:
        return EntryDecision(
            ok=False,
            reason_code=DENY_PERMIT_EXPIRED,
            message=f"凭证已于 {permit.valid_to:%H:%M} 失效，请重新预约。",
            permit_id=permit.id,
            user_id=permit.user_id,
            lab_id=permit.lab_id,
        )

    # ---- 闸门 5：人卡一致（防转让的核心一道）----
    # 凭证绑定的是账号，而站在门口的是人。两者不一致就是"替人刷卡"。
    if ctx.identity_user_id is None:
        return EntryDecision(
            ok=False,
            reason_code=DENY_IDENTITY_MISMATCH,
            message="没有识别到你的身份（请刷卡或进行人脸核验）。",
            permit_id=permit.id,
            user_id=permit.user_id,
            lab_id=permit.lab_id,
        )
    if ctx.identity_user_id != permit.user_id:
        return EntryDecision(
            ok=False,
            reason_code=DENY_IDENTITY_MISMATCH,
            message="这张凭证不属于当前识别到的身份，不能代刷。",
            permit_id=permit.id,
            user_id=ctx.identity_user_id,
            lab_id=permit.lab_id,
            extra={"permit_owner": permit.user_id},
        )
    if permit.user_id == 0 or ctx.identity_user_id == 0:
        # 占位防线：历史上出现过"默认 user_id=1"这类越权入口（见 P0-2），
        # 这里显式排除 0 号（系统占位身份），避免它被当成"任何人"。
        return EntryDecision(
            ok=False,
            reason_code=DENY_NO_SUCH_USER,
            message="身份无效。",
            permit_id=permit.id,
            lab_id=permit.lab_id,
        )

    # ---- 闸门 6：资质在**今天**是否仍然有效 ----
    # 注意这把关的是"有效期"，不是"有没有授权过"。三年前的证不算数。
    for cert in permit.required_certs or []:
        cs = ctx.cert_states.get(cert) or CertState("missing")
        if cs.state != "ok":
            return EntryDecision(
                ok=False,
                reason_code=_DENY_BY_CERT_STATE.get(cs.state, DENY_CERT_MISSING),
                message=_CERT_MESSAGES.get(cs.state, "资质不满足要求。").format(
                    cert=cert, expires_at=cs.expires_at or ""
                ),
                permit_id=permit.id,
                user_id=permit.user_id,
                lab_id=permit.lab_id,
                extra={"cert": cert, "cert_state": cs.state},
            )

    # ---- 闸门 7：一个人不能在两个房间同时"在馆" ----
    # 这既是统计准确性问题（人数被算两遍），也是安全问题：
    # 说不出一个人此刻到底在哪，出事时就找不到人。
    if ctx.inside_permit_id is not None:
        return EntryDecision(
            ok=False,
            reason_code=DENY_ALREADY_INSIDE,
            message="你已有一条在馆记录。请先刷卡出场，再进入其它实验室。",
            permit_id=permit.id,
            user_id=permit.user_id,
            lab_id=permit.lab_id,
            extra={"inside_permit_id": ctx.inside_permit_id},
        )

    return EntryDecision(
        ok=True,
        permit_id=permit.id,
        user_id=permit.user_id,
        lab_id=permit.lab_id,
    )


# ===========================================================================
# 签发
# ===========================================================================
def new_credential() -> tuple[str, str]:
    """生成一张凭证：返回 (明文, 哈希)。

    明文**只在签发这一次返回**，之后系统只留哈希 —— 与口令同样的处理。
    明文用于渲染二维码 / 写进 NFC 卡，数据库里查不到它，
    所以库被拖走时拿不到可直接使用的通行证。
    """
    plain = secrets.token_urlsafe(_CREDENTIAL_BYTES)
    return plain, hash_credential(plain)


def hash_credential(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def credential_fingerprint(plain_or_hash: str) -> str:
    """取短指纹用于事件流水：排查"是不是同一张截图被反复刷"够用了，
    同时不构成可用的凭证（只有前 8 字节，且不可逆推明文）。"""
    return plain_or_hash[:16]


async def required_certs_for_lab(session: AsyncSession, lab_id: int) -> list[str]:
    """进这个房间需要哪些资质。

    目前规则很朴素：实验室安全是基础，房间内只要有**受控设备**
    （``requires_training``）就再叠加它的类别 —— 因为要操作它就必须先走到它面前。
    规则随业务长起来时，这里换成一张"房间准入规则表"即可，
    调用方（``issue_permit``）不需要改。
    """
    from ..models import Equipment  # 局部导入：避免模块级循环依赖

    certs = [LAB_BASIC_CERT]
    rows = await session.execute(
        select(Equipment.category).where(
            Equipment.lab_id == lab_id, Equipment.requires_training.is_(True)
        )
    )
    for (category,) in rows.all():
        if category and category not in certs:
            certs.append(category)
    return certs


async def issue_permit(
    session: AsyncSession,
    *,
    user_id: int,
    lab_id: int,
    date_: dt.date,
    valid_from: dt.time,
    valid_to: dt.time,
    source: str = PERMIT_SOURCE_RESERVATION,
    reservation_id: int | None = None,
    required_certs: list[str] | None = None,
) -> tuple[EntryPermit, str]:
    """签发一张入室凭证。返回 (凭证, 明文凭证串)。

    调用方必须在同一事务里把 ``credential_hash`` 的唯一性交给数据库裁决 ——
    这里不预检是否重复：预检是 check-then-act，而哈希撞车的概率虽然极低，
    "极低概率 + 并发"正是最容易被忽略的组合。
    """
    if required_certs is None:
        required_certs = await required_certs_for_lab(session, lab_id)
    plain, digest = new_credential()
    permit = EntryPermit(
        user_id=user_id,
        lab_id=lab_id,
        date=date_,
        valid_from=valid_from,
        valid_to=valid_to,
        status=PERMIT_ISSUED,
        source=source,
        reservation_id=reservation_id,
        credential_hash=digest,
        required_certs=list(required_certs),
    )
    session.add(permit)
    await session.flush()
    return permit, plain


# ===========================================================================
# 容量：占座（★ 不变式在这里）
# ===========================================================================
class LabFullError(Exception):
    """房间在该时段已无空位。由唯一索引判定，不是应用层数的。"""


async def claim_seats(
    session: AsyncSession,
    *,
    lab_id: int,
    date_: dt.date,
    start: dt.time,
    end: dt.time,
    permit_id: int,
    capacity: int,
    granularity: int | None = None,
) -> int:
    """给凭证占住 [start, end) 每个时间格的一个座位。返回占用的格数。

    **为什么逐格占座而不是改一个计数**：计数列要「读 → 加一 → 写回」，
    两个并发请求会读到同一个旧值，最终人数少算 —— 这与本项目在设备预约上
    踩过的超卖是同一个坑。占座把"超容量"变成"同一座位被占两次"，
    由 ``uq_lab_slot_seat`` 判定，应用层改不掉。

    座位只是把容量离散化的槽口（0..capacity-1），不对应物理座位号。
    撞了座位就换一个；把 capacity 个座位都撞完，才是真的满了。

    **窗口必须向外吸附到网格边界**，不能直接 ``slot_indexes_for(start, end)``：
    ``slot_index_of()`` 对未对齐的时间点是**向下取整**的，于是 22:10-23:10 的凭证
    只会占掉 [22:00, 23:00) 两格 —— 而"23:05 刷卡进门"落在第 46 格，
    恰恰不在占位集合里。结果是凭证尾巴那一小段完全不受容量保护，房间可以超容。
    门禁是安全设施，宁可**多占一格**也不能少占，所以起点向下、终点向上吸附。
    （这个漏保护是被 ``main.py access-demo`` 自己暴露出来的：它打印的"当前格在馆人数"
    明明是 1 个人刚进去，却显示 0。）

    失败时抛 :class:`LabFullError`，调用方负责让整个事务回滚 ——
    绝不能"占了一半留着"，那会让房间显示满了却没人。
    """
    if capacity <= 0:
        raise LabFullError(f"实验室 {lab_id} 容量为 0，未开放")
    step = granularity or DEFAULT_SLOT_GRANULARITY_MINUTES
    slots = list(range(_floor_slot(start, step), _ceil_slot(end, step)))
    if not slots:
        # 空窗口说明判定层与这里的前提不一致（正常情况下 gate 4 已经拦掉），
        # fail-closed：宁可拒，也不能"占了 0 格"就把人放进去。
        raise LabFullError(f"实验室 {lab_id} 的凭证时间窗无效（{start}-{end}）")
    for slot in slots:
        placed = False
        for seat in range(capacity):
            try:
                # SAVEPOINT：撞车只回滚这一条 INSERT，不毒化外层事务。
                # 没有它，一次 IntegrityError 会让后续所有语句都失败。
                async with session.begin_nested():
                    session.add(
                        LabOccupancy(
                            lab_id=lab_id,
                            date=date_,
                            slot_index=slot,
                            seat=seat,
                            permit_id=permit_id,
                        )
                    )
                    await session.flush()
                placed = True
                break
            except IntegrityError:
                continue
        if not placed:
            raise LabFullError(f"实验室 {lab_id} 在 {slot} 时段已满（容量 {capacity}）")
    return len(slots)


async def release_seats(session: AsyncSession, permit_id: int) -> int:
    """释放凭证占的全部座位（出门 / 撤销凭证时调用）。"""
    from sqlalchemy import delete

    result = cast(
        CursorResult,
        await session.execute(
            delete(LabOccupancy).where(LabOccupancy.permit_id == permit_id)
        ),
    )
    return result.rowcount or 0


async def inside_count(session: AsyncSession, lab_id: int, date_: dt.date, slot_index: int) -> int:
    """某格当前在馆人数（= 已占座位数）。用于看板与告警。"""
    total = await session.scalar(
        select(func.count())
        .select_from(LabOccupancy)
        .where(
            LabOccupancy.lab_id == lab_id,
            LabOccupancy.date == date_,
            LabOccupancy.slot_index == slot_index,
        )
    )
    return int(total or 0)


# ===========================================================================
# 核验与核销
# ===========================================================================
async def find_permit_by_credential(
    session: AsyncSession, plain: str, *, today: dt.date
) -> EntryPermit | None:
    """按凭证串找凭证。按哈希查，不存明文。"""
    return await session.scalar(
        select(EntryPermit).where(
            EntryPermit.credential_hash == hash_credential(plain),
            EntryPermit.date == today,
        )
    )


async def find_active_permit(
    session: AsyncSession, *, user_id: int, lab_id: int, today: dt.date
) -> EntryPermit | None:
    """刷卡场景：没带二维码时，按人 + 房间找当刻还能用的凭证。

    只找 ``issued``（未核销）的那些；已核销/已撤销/已过期的都不算。
    """
    return await session.scalar(
        select(EntryPermit)
        .where(
            EntryPermit.user_id == user_id,
            EntryPermit.lab_id == lab_id,
            EntryPermit.date == today,
            EntryPermit.status == PERMIT_ISSUED,
        )
        .order_by(EntryPermit.valid_from)
    )


async def find_inside_permit(
    session: AsyncSession, *, user_id: int
) -> EntryPermit | None:
    """该人当前是否已在馆。"""
    return await session.scalar(
        select(EntryPermit).where(
            EntryPermit.user_id == user_id,
            EntryPermit.status == PERMIT_CHECKED_IN,
        )
    )


async def cert_states_for(
    session: AsyncSession, *, user_id: int, categories: list[str], today: dt.date
) -> dict[str, CertState]:
    """查询若干资质类别在 ``today`` 的状态。

    判定顺序是「先看有没有，再看撤没撤，最后看到期没到」 —— 三条路径给出的
    处置完全不同（去考证 / 找管理员 / 去复训），合并成一句"资质不合格"
    会让用户不知道该干什么。

    同时看 ``User.certs`` 这个历史字段：它是没有有效期的老数据，
    直接忽略会让所有存量用户在升级当天全部进不了门。
    迁移策略是"老数据按永久有效、新授权走 cert_grants"，
    供后续逐步补齐有效期。**这是有意的过渡妥协，不是设计缺陷** ——
    但它必须被标注出来，否则"所有老用户永久有效"会被当成有意策略而永远不迁。
    """
    from ..models import User  # 局部导入：避免模块级循环依赖

    states: dict[str, CertState] = {}
    if not categories:
        return states

    rows = await session.execute(
        select(CertGrant).where(
            CertGrant.user_id == user_id, CertGrant.category.in_(categories)
        )
    )
    grants = list(rows.scalars().all())

    legacy: set[str] = set()
    granted_categories = {g.category for g in grants}
    if any(c not in granted_categories for c in categories):
        user = await session.get(User, user_id)
        legacy = set((user.certs or []) if user is not None else [])

    for category in categories:
        same = [g for g in grants if g.category == category]
        if not same:
            states[category] = CertState("ok" if category in legacy else "missing")
            continue
        # 有授权记录：以**最新授予**的那条为准（复训换证 = 新的 granted_at）
        latest = max(same, key=lambda g: g.granted_at)
        if latest.revoked_at is not None:
            states[category] = CertState("revoked", latest.revoked_at.date())
        elif latest.expires_at < today:
            states[category] = CertState("expired", latest.expires_at)
        else:
            states[category] = CertState("ok", latest.expires_at)
    return states


async def verify_entry(
    session: AsyncSession,
    *,
    lab_id: int,
    now: dt.datetime | None = None,
    credential: str | None = None,
    identity_user_id: int | None = None,
    gate_id: str = "",
    check_in: bool = True,
) -> EntryDecision:
    """核验一次入场请求；``check_in=True`` 时顺带核销并占座。

    这是门禁侧唯一需要的入口 —— 它把"判定"和"落库"两件事收在一处，
    调用方（门禁服务 / HTTP 接口）不需要知道内部有几道闸门。

    ``check_in=False`` 用来做**预检**：门禁屏上想先显示"你的凭证有效，
    随时可以进"，而不产生任何副作用。判定逻辑完全共用，避免
    "预检说可以、真刷说不可以"这种最伤信任的分歧。
    """
    now = now or now_local()
    today = now.date()
    lab = await session.get(Laboratory, lab_id)
    if lab is None:
        return EntryDecision(ok=False, reason_code=DENY_WRONG_LAB, message="实验室不存在。")

    # ---- 先把「凭证」和「身份」这两条线索凑齐 ----
    permit: EntryPermit | None = None
    fingerprint = ""
    if credential:
        fingerprint = credential_fingerprint(hash_credential(credential))
        permit = await find_permit_by_credential(session, credential, today=today)
        if permit is not None and identity_user_id is None:
            # 扫二维码的场景：凭证本身就是身份线索，用它补全身份
            identity_user_id = permit.user_id
    elif identity_user_id is not None:
        # 刷卡场景：按人找当刻可用的凭证
        permit = await find_active_permit(
            session, user_id=identity_user_id, lab_id=lab_id, today=today
        )

    cert_states: dict[str, CertState] = {}
    inside: EntryPermit | None = None
    if permit is not None:
        cert_states = await cert_states_for(
            session,
            user_id=permit.user_id,
            categories=list(permit.required_certs or []),
            today=today,
        )
        inside = await find_inside_permit(session, user_id=permit.user_id)

    ctx = EntryContext(
        now=now,
        lab_id=lab_id,
        identity_user_id=identity_user_id,
        permit=permit,
        cert_states=cert_states,
        inside_permit_id=(inside.id if inside is not None else None),
    )
    decision = evaluate_entry(ctx)

    if decision.denied:
        await _record(
            session,
            event=AccessEvent(
                user_id=identity_user_id,
                lab_id=lab_id,
                gate_id=gate_id,
                direction=DIRECTION_IN,
                result=ACCESS_DENIED,
                reason_code=decision.reason_code,
                permit_id=(permit.id if permit is not None else None),
                credential_fingerprint=fingerprint,
                detail=decision.message,
            ),
        )
        return decision

    if not check_in:
        # 预检：判定通过但不产生任何副作用
        return decision

    if permit is None:
        # 理论上到不了这里（``evaluate_entry`` 在凭证为空时必然拒），
        # 但类型上必须收窄，而且它同时是一条 fail-closed 兜底：
        # 走到这个分支说明判定与后续逻辑的假设不一致，
        # 此时**绝不能放行**，宁可拒。
        return EntryDecision(
            ok=False,
            reason_code=DENY_NO_PERMIT,
            message="核验状态异常，已拒绝本次入场。",
            lab_id=lab_id,
        )

    # ---- 占座 → 核销。顺序不能反：座位占不到就不该把凭证标记成已入场 ----
    # 整块包在 SAVEPOINT 里：占座是逐格的，可能占了 3 格、第 4 格满了 ——
    # 那次部分占用必须整体撤销。绝不允许"占了一半留着"，那会让房间
    # 显示满了却没人，而且没有任何记录能解释这些座位去哪了。
    #
    # 用 SAVEPOINT 而不是往外抛异常：外层事务还要写下这条"因满员被拒"的事件，
    # 那是"未预约/超容量试图进入"的证据，不能跟着一起回滚掉。
    try:
        async with session.begin_nested():
            await claim_seats(
                session,
                lab_id=lab_id,
                date_=today,
                start=permit.valid_from,
                end=permit.valid_to,
                permit_id=permit.id,
                capacity=lab.capacity,
            )
    except LabFullError as exc:
        decision = EntryDecision(
            ok=False,
            reason_code=DENY_LAB_FULL,
            message="实验室该时段人数已满，请联系管理员或改约其它时间。",
            permit_id=permit.id,
            user_id=permit.user_id,
            lab_id=lab_id,
        )
        await _record(
            session,
            event=AccessEvent(
                user_id=permit.user_id,
                lab_id=lab_id,
                gate_id=gate_id,
                direction=DIRECTION_IN,
                result=ACCESS_DENIED,
                reason_code=DENY_LAB_FULL,
                permit_id=permit.id,
                credential_fingerprint=fingerprint,
                detail=str(exc),
            ),
        )
        return decision

    permit.status = PERMIT_CHECKED_IN
    permit.checked_in_at = now
    permit.gate_in = gate_id
    await session.flush()
    await _record(
        session,
        event=AccessEvent(
            user_id=permit.user_id,
            lab_id=lab_id,
            gate_id=gate_id,
            direction=DIRECTION_IN,
            result=ACCESS_GRANTED,
            reason_code="",
            permit_id=permit.id,
            credential_fingerprint=fingerprint,
            detail="",
        ),
    )
    return EntryDecision(
        ok=True,
        permit_id=permit.id,
        user_id=permit.user_id,
        lab_id=lab_id,
        message="已入场。",
    )


async def verify_exit(
    session: AsyncSession,
    *,
    user_id: int,
    now: dt.datetime | None = None,
    gate_id: str = "",
) -> EntryDecision:
    """出场：销账并释放座位。

    出门也要刷卡，不是多此一举 —— 没有出场记录，"当前在馆人数"就退化成
    "今天进过的人数"，容量约束与安全搜救都会失真。
    尾随进门最常见的手法正是"只刷进不刷出"。
    """
    now = now or now_local()
    permit = await find_inside_permit(session, user_id=user_id)
    if permit is None:
        return EntryDecision(
            ok=False,
            reason_code=DENY_NO_PERMIT,
            message="你没有在馆记录。",
            user_id=user_id,
        )
    permit.status = PERMIT_USED
    permit.checked_out_at = now
    permit.gate_out = gate_id
    released = await release_seats(session, permit.id)
    await session.flush()
    await _record(
        session,
        event=AccessEvent(
            user_id=user_id,
            lab_id=permit.lab_id,
            gate_id=gate_id,
            direction=DIRECTION_OUT,
            result=ACCESS_GRANTED,
            permit_id=permit.id,
            detail=f"释放 {released} 个座位",
        ),
    )
    return EntryDecision(ok=True, permit_id=permit.id, user_id=user_id, lab_id=permit.lab_id)


async def revoke_permit(
    session: AsyncSession, *, permit_id: int, reason: str = ""
) -> EntryPermit | None:
    """撤销凭证。已入场的会被标记为已使用并释放座位（人可能还在里面，
    但凭证不能再用于**再次**入场；清场由值班人员按在馆名单执行）。"""
    permit = await session.get(EntryPermit, permit_id)
    if permit is None:
        return None
    permit.status = PERMIT_REVOKED
    await release_seats(session, permit.id)
    await session.flush()
    return permit


async def expire_stale_permits(
    session: AsyncSession, *, now: dt.datetime | None = None
) -> int:
    """把已过 valid_to 仍未入场的凭证标记为过期。返回处理条数。

    **必须同时覆盖「今天到点」和「往日遗留」两种。** 只判今天会出现一个很隐蔽的
    残留：昨天签发但没用掉的凭证会永远停在 ``issued``（没人会去碰它），
    于是"还有多少张没用的凭证"这个统计会一直虚高，而且是**单调增长**的。

    这只是一个"清理展示状态"的批处理：即使不跑，核验时的时间窗判定
    也一样会拒 —— 正确性不依赖这个任务。它挂掉不能让门禁失去保护。
    所以它**没有**顺手去释放座位之类有副作用的动作：那些属于
    :mod:`lagent.sweep`，需要能单独审计。
    """
    now = now or now_local()
    from sqlalchemy import and_, or_, update

    result = cast(
        CursorResult,
        await session.execute(
            update(EntryPermit)
            .where(
                EntryPermit.status == PERMIT_ISSUED,
                or_(
                    EntryPermit.date < now.date(),
                    and_(
                        EntryPermit.date == now.date(),
                        EntryPermit.valid_to < now.time(),
                    ),
                ),
            )
            .values(status=PERMIT_EXPIRED)
        ),
    )
    await session.flush()
    return result.rowcount or 0


# ---------------------------------------------------------------------------
async def _record(session: AsyncSession, *, event: AccessEvent) -> None:
    """写一条通行事件。与业务同事务 —— 事件和被记录的状态必须一起成立。

    刻意不做成"永远不抛异常"（对比 ``audit.record``）：审计丢一条不该影响业务，
    但**通行事件丢一条意味着门开了却没有记录**。这种事必须在事务里一起成败，
    宁可整次核验失败重来，也不要出现"门开了、查不到是谁开的"。

    ``request_id`` 在这里统一补上（P1-3）：``verify_entry`` / ``verify_exit``
    里有七八处构造 ``AccessEvent`` 的地方，逐处传参必然漏。
    集中在这一处，门禁流水和审计、和 HTTP 访问日志就共用同一个关联 id，
    一条 SQL 能把「谁刷的卡」和「哪个请求触发的」对上。
    """
    if not event.request_id:
        event.request_id = current_request_id()[:36]
    session.add(event)


def _time_plus(value: dt.time, minutes: int) -> dt.time:
    """给时间点加减分钟（只关心当天循环，不跨天）。"""
    base = value.hour * 60 + value.minute + minutes
    base = max(0, min(base, 24 * 60 - 1))
    return dt.time(base // 60, base % 60)
