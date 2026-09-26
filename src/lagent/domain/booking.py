"""下单与取消：并发安全 + 业务规则兜底。

**「区间不重叠」由数据库唯一索引保证，不靠应用层自觉。**

早先的实现只在 ``(equipment_id, date, start_time)`` 上建唯一索引，
并依赖「设备级锁 + 锁内复检」兜住区间相交。这个设计是错的，实测漏了：

  1. 唯一索引只能拦**开始时间相同**的重复写入；
     ``13:00-15:00`` 与 ``14:00-16:00`` 开始时间不同，索引看不见它们重叠。
  2. 所谓「设备级锁」在 SQLite 上压根不存在 —— SQLite 没有行级锁，
     ``acquire_equipment_lock`` 当时直接 return 什么都没做。
     于是「锁内复检」退化成典型的 check-then-act：两个事务各自读到
     「无冲突」（读快照），再分别写入，两条都落库。
     20 并发实测：两种时段双双成功 = 超卖。

现在改成把区间拆成 30 分钟的格（``models.ReservationSlot``），
唯一索引打在 ``(equipment_id, date, slot_index)`` 上。
任何形式的相交（相同 / 部分重叠 / 包含）都会撞索引，**且与数据库无关**。

三层防线仍然保留，但职责重新划分：

  1. **写前校验**：即便参数是模型给的，也重新过一遍全部非时间约束
     （设备状态、准入资质、开放时间、单次上限、粒度对齐）。
     模型输出不可信，业务规则绝不能交给它最终裁决。
  2. **占用格唯一索引**：★★ 这是**唯一**的正确性保证。撞上即冲突。
  3. **重试**：撞索引不是失败退出，而是重试 —— 抢到坑的很可能就是另一个
     并发请求，重试时会重新查一次冲突，把「被别人抢了」如实回报给用户。

另有一个 PostgreSQL 专属的 ``pg_advisory_xact_lock``，作用是**减少重试次数**
（提前把同设备请求串起来），它**不承担正确性职责**：
即使它完全不起作用，第 2 层也能拦住重复。这一点很关键 ——
上一版就是把它误当成了正确性保证，而它在 SQLite 上根本没生效。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
from collections.abc import Awaitable, Callable, Sequence
from typing import ParamSpec, cast

from sqlalchemy import CursorResult, delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..clock import minutes_between, now_local, overlaps
from ..config import get_settings
from ..db import session_scope
from ..metrics import (
    record_booking_outcome,
    record_booking_retry,
    record_cancel_outcome,
    record_review_outcome,
)
from ..models import (
    ACTIVE_STATUSES,
    DEFAULT_SLOT_GRANULARITY_MINUTES,
    EQUIPMENT_NORMAL,
    STATUS_CANCELLED,
    STATUS_CONFIRMED,
    STATUS_PENDING,
    Equipment,
    Reservation,
    ReservationSlot,
    User,
    is_aligned,
    slot_indexes_for,
)
from ..schemas import BookingOutcome, ReservationOut
from .violations import state_for

_P = ParamSpec("_P")


def _counted(
    recorder: Callable[[str], None],
) -> Callable[
    [Callable[_P, Awaitable[BookingOutcome]]],
    Callable[_P, Awaitable[BookingOutcome]],
]:
    """把「按结果计数」这件事集中到一个地方（P1-4）。

    为什么不在这十来个 ``return`` 旁边各写一次 ``record_...``：
    **漏掉一个返回点不会报任何错**，只会让那个结果的计数永远偏小 ——
    与 P1-3 在审计里遇到的完全是同一个坑（构造点有七八处，逐个传必然漏）。
    包一层的代价是"埋点在哪"不那么显眼，所以这里写了这段注释，
    并且用 ``functools.wraps`` 保住原函数的签名与文档。

    ``ParamSpec`` 而不是 ``**kwargs``：后者会让 mypy 和 IDE 都丢掉真实签名，
    而这两个函数是领域层的对外入口，签名本身就是文档。
    """

    def decorate(
        fn: Callable[_P, Awaitable[BookingOutcome]],
    ) -> Callable[_P, Awaitable[BookingOutcome]]:
        @functools.wraps(fn)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> BookingOutcome:
            outcome = await fn(*args, **kwargs)
            recorder(outcome.outcome_label)
            return outcome

        return wrapper

    return decorate


# --------------------------------------------------------------------------
# 只读辅助
# --------------------------------------------------------------------------
async def find_conflict(
    session: AsyncSession,
    equipment_id: int,
    date_: dt.date,
    start: dt.time,
    end: dt.time,
    *,
    exclude_id: int | None = None,
) -> Reservation | None:
    """锁内复检：该设备当天是否已有与之重叠的有效预约。"""
    stmt = select(Reservation).where(
        Reservation.equipment_id == equipment_id,
        Reservation.date == date_,
        Reservation.status.in_(ACTIVE_STATUSES),
    )
    if exclude_id is not None:
        stmt = stmt.where(Reservation.id != exclude_id)
    for row in (await session.execute(stmt)).scalars().all():
        if overlaps(start, end, row.start_time, row.end_time):
            return row
    return None


def granularity_minutes() -> int:
    """当前配置的最小预约粒度（分钟）。"""
    return get_settings().slot_granularity_minutes or DEFAULT_SLOT_GRANULARITY_MINUTES


def _ensure_aligned(start: dt.time, end: dt.time) -> list[str]:
    """校验时间点是否落在粒度边界上。

    这不是形式主义：占用格是用「格索引」算的，未对齐的时间点会被向下取整，
    于是区间**漏保护**（例如 09:00-10:20 只占住到 10:00，10:00 之后的
    重叠请求就绕过去了）。与其静默漏保护，不如显式拒绝并告诉用户原因。

    产品上也是合理的：实验室按整点/半点排机时是通行做法。
    """
    size = granularity_minutes()
    problems: list[str] = []
    if not is_aligned(start, size):
        problems.append(f"开始时间需为 {size} 分钟的整数倍（如 09:00、09:30）")
    if not is_aligned(end, size):
        problems.append(f"结束时间需为 {size} 分钟的整数倍（如 11:00、11:30）")
    return problems


async def attach_slots(
    session: AsyncSession,
    reservation: Reservation,
    *,
    granularity: int | None = None,
) -> None:
    """给一条预约登记它占用的所有格。

    调用方必须已经 ``flush()`` 过 reservation（需要它的自增 id）。
    这里的 flush 就是**不变式 2 的裁决点**：任一格已被占用会抛 IntegrityError。
    """
    size = granularity or granularity_minutes()
    for index in slot_indexes_for(reservation.start_time, reservation.end_time, size):
        session.add(
            ReservationSlot(
                reservation_id=reservation.id,
                equipment_id=reservation.equipment_id,
                date=reservation.date,
                slot_index=index,
            )
        )
    await session.flush()


async def release_slots(session: AsyncSession, reservation_id: int) -> int:
    """释放一条预约占用的全部格（取消 / 改期时调用）。返回释放的格数。"""
    result = cast(
        CursorResult,
        await session.execute(
            delete(ReservationSlot).where(ReservationSlot.reservation_id == reservation_id)
        ),
    )
    return result.rowcount or 0


async def acquire_equipment_lock(session: AsyncSession, equipment_id: int) -> str:
    """按设备取事务级排他锁（**仅 PostgreSQL**，且不承担正确性职责）。

    ⚠️ 它只是「减少重试次数」的优化：提前把同一设备的并发请求串起来，
    避免大家都走到唯一索引撞车再重试。**正确性由占用格唯一索引保证** ——
    即使本函数完全不起作用（SQLite 下就是如此），也不会出现超卖。

    上一版把这里当成第二道正确性防线，是本次修复的核心教训：
    **在 SQLite 上它是空实现，而注释却写着"写事务天然互斥"，于是
    "锁内复检"实际上是 check-then-act。**
    """
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        # hashtext 把设备号映射成 advisory lock 的 key；_xact_ 版本随事务结束自动释放
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"lagent:equipment:{equipment_id}"},
        )
        return "pg_advisory_xact_lock"
    if dialect == "sqlite":
        # SQLite 没有行级锁，这里确实什么都不做。返回策略名只为可观测性 ——
        # 千万别再把它当成"已经保护好了"。
        return "none(sqlite)"
    return "none"


def _to_out(
    res: Reservation,
    equipment_name: str = "",
    lab_label: str = "",
    user_name: str = "",
) -> ReservationOut:
    return ReservationOut(
        id=res.id,
        equipment_id=res.equipment_id,
        user_id=res.user_id,
        user_name=user_name,
        equipment_name=equipment_name,
        lab_label=lab_label,
        date=res.date,
        start_time=res.start_time,
        end_time=res.end_time,
        status=res.status,
        purpose=res.purpose,
        slot=res.slot_label,
        # 带上违约时间戳，管理员才能在界面上看出"哪条被判了未到场"并去豁免
        no_show_at=res.no_show_at,
        pardoned_at=res.pardoned_at,
    )


async def _validate(
    session: AsyncSession,
    user: User,
    equipment: Equipment,
    date_: dt.date,
    start: dt.time,
    end: dt.time,
) -> list[str]:
    """写前校验：不信任调用方（尤其是模型）给的参数。返回人话错误列表。"""
    problems: list[str] = []

    if equipment.status != EQUIPMENT_NORMAL:
        problems.append(f"设备「{equipment.name}」当前状态为 {equipment.status}，不可预约")

    if equipment.requires_training and equipment.category not in (user.certs or []):
        problems.append(
            f"你缺少「{equipment.category}」准入资质，该设备需通过培训后才能预约"
        )

    if end <= start:
        problems.append("结束时间必须晚于开始时间")
        return problems

    # 粒度对齐：不对齐会让占用格算漏，等于漏保护（见 _ensure_aligned）
    problems.extend(_ensure_aligned(start, end))

    hours = minutes_between(start, end) / 60
    if hours > equipment.max_hours:
        problems.append(
            f"单次最长 {equipment.max_hours} 小时，本次请求 {hours:g} 小时"
        )

    lab = equipment.lab
    if lab is not None:
        from ..clock import parse_time, weekday_key

        raw = (lab.open_hours or {}).get(weekday_key(date_))
        if not raw or len(raw) != 2:
            problems.append(f"{lab.label} 当天不开放")
        else:
            open_start, open_end = parse_time(str(raw[0])), parse_time(str(raw[1]))
            if start < open_start or end > open_end:
                problems.append(
                    f"超出 {lab.label} 开放时间 "
                    f"{open_start.strftime('%H:%M')}-{open_end.strftime('%H:%M')}"
                )

    return problems


async def _load_equipment(session: AsyncSession, equipment_id: int) -> Equipment | None:
    """带 lab 预加载地取设备。

    异步 session 里访问未预加载的关系会抛 MissingGreenlet —— 因为懒加载需要
    一个同步 IO 上下文，而 async 驱动里没有。凡是会读到 ``equipment.lab``
    的地方都必须像这样显式 eager load，不能指望它自己加载。
    """
    stmt = (
        select(Equipment)
        .options(selectinload(Equipment.lab))
        .where(Equipment.id == equipment_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# --------------------------------------------------------------------------
# 下单
# --------------------------------------------------------------------------
@_counted(record_booking_outcome)
async def create_reservation(
    *,
    user_id: int,
    equipment_id: int,
    date_: dt.date,
    start: dt.time,
    end: dt.time,
    purpose: str = "",
    now: dt.datetime | None = None,
) -> BookingOutcome:
    settings = get_settings()
    now = now or now_local()
    retries = 0

    for attempt in range(settings.booking_max_retry):
        retries = attempt
        try:
            async with session_scope() as session:
                await acquire_equipment_lock(session, equipment_id)

                user = await session.get(User, user_id)
                if user is None:
                    return BookingOutcome(
                        ok=False,
                        message=f"用户 {user_id} 不存在",
                        retries=attempt,
                        reason="not_found",
                    )

                # P1-8：违约限制。**放在这里而不是在 API 层**，是因为
                # 下单有三个入口（表单接口、Agent 工具、管理员代下单），
                # 在 API 层各判一次的话，迟早出现"对话能约、表单不能约"
                # 这种不一致 —— 而用户只会理解成系统在针对他。
                state = await state_for(session, user_id, now=now)
                if state.blocked:
                    return BookingOutcome(
                        ok=False,
                        message=state.message,
                        retries=attempt,
                        # forbidden 而不是 invalid：他填的时段没问题，
                        # 是他这个人此刻没有预约资格。前端据此给的是
                        # "去联系管理员"，而不是"换个时间试试"。
                        reason="forbidden",
                    )

                equipment = await _load_equipment(session, equipment_id)
                if equipment is None:
                    return BookingOutcome(
                        ok=False,
                        message=f"设备 {equipment_id} 不存在",
                        retries=attempt,
                        reason="not_found",
                    )

                problems = await _validate(session, user, equipment, date_, start, end)
                if problems:
                    return BookingOutcome(
                        ok=False,
                        message="；".join(problems),
                        retries=attempt,
                        reason="invalid",
                    )

                if dt.datetime.combine(date_, start) <= now:
                    return BookingOutcome(
                        ok=False,
                        message="该时间点已经过去，请选择之后的时间",
                        retries=attempt,
                        reason="invalid",
                    )

                # 锁内复检：这是「不被别人抢走」的实际判据
                conflict = await find_conflict(session, equipment_id, date_, start, end)
                if conflict is not None:
                    return BookingOutcome(
                        ok=False,
                        message=f"该时段刚被占用：{conflict.slot_label}",
                        retries=attempt,
                        conflict_with=_to_out(conflict, equipment.name, equipment.lab.label),
                        # conflict：复检就发现被占 —— 用户看到的是"这坑没了"，
                        # 是真实业务冲突，不是系统打架（后者是 contention）。
                        reason="conflict",
                    )

                # 需要审批的设备落到 pending（P1-5）。
                # pending 在 ACTIVE_STATUSES 里，所以**申请即占坑**：
                # 不占坑的话，"提交申请"到"审批通过"之间别人能再约同一时段，
                # 等批下来才发现冲突 —— 用户白等一场，而且更气。
                needs_review = equipment.requires_approval
                res = Reservation(
                    user_id=user_id,
                    equipment_id=equipment_id,
                    date=date_,
                    start_time=start,
                    end_time=end,
                    status=STATUS_PENDING if needs_review else STATUS_CONFIRMED,
                    purpose=purpose,
                    version=1,
                )
                session.add(res)
                # 第一次 flush 只为拿到自增 id（占用格要引用它）
                await session.flush()
                # ★ 第二次 flush —— 这里才是「区间不重叠」的裁决点：
                # 任一格已被占用就抛 IntegrityError，整个事务回滚。
                await attach_slots(session, res)
                out = _to_out(res, equipment.name, equipment.lab.label)
                return BookingOutcome(
                    ok=True,
                    message=(
                        f"已提交申请，等待管理员审批：{out.slot} "
                        f"{equipment.name}（{equipment.lab.label}）"
                        if needs_review
                        else f"预约成功：{out.slot} {equipment.name}（{equipment.lab.label}）"
                    ),
                    reservation=out,
                    retries=attempt,
                    reason="ok",
                )

        except IntegrityError:
            # 占用格唯一索引拦下了重叠（或同坑重复）—— 抢坑的是并发请求，重试即可。
            # 注意：重试前事务已回滚，不会留下半条预约或半个占用格。
            #
            # 这个计数与"复检发现被占"分开记：它衡量的是**并发争抢的强度**，
            # 也就是"该扩容了吗"。合成一个数字就把产品问题和容量问题混在一起了。
            record_booking_retry()
            await asyncio.sleep(0.005 * (attempt + 1))
            continue

    return BookingOutcome(
        ok=False,
        message=f"并发冲突，已重试 {settings.booking_max_retry} 次仍未成功，请稍后再试",
        retries=retries,
        reason="contention",
    )


# --------------------------------------------------------------------------
# 取消（乐观锁）
# --------------------------------------------------------------------------
@_counted(record_cancel_outcome)
async def cancel_reservation(
    *,
    reservation_id: int,
    user_id: int,
    reason: str = "",
    expected_version: int | None = None,
    max_retry: int | None = None,
) -> BookingOutcome:
    """取消预约。

    用「带版本条件的 UPDATE」而不是「读出来改再写」：后者在并发下会
    丢掉别人的修改（两个请求同时读到 version=1，各自写回 version=2）。
    条件更新让数据库来裁决版本是否仍然匹配，行数为 0 即说明被别人改过。
    """
    settings = get_settings()
    limit = max_retry if max_retry is not None else settings.booking_max_retry

    for attempt in range(limit):
        async with session_scope() as session:
            res = await session.get(Reservation, reservation_id)
            if res is None:
                return BookingOutcome(
                    ok=False,
                    message=f"预约 {reservation_id} 不存在",
                    retries=attempt,
                    reason="not_found",
                )
            if res.user_id != user_id:
                return BookingOutcome(
                    ok=False,
                    message="只能取消自己的预约",
                    retries=attempt,
                    reason="forbidden",
                )
            if res.status not in ACTIVE_STATUSES:
                return BookingOutcome(
                    ok=False,
                    message=f"该预约当前状态为 {res.status}，无需取消",
                    retries=attempt,
                    reason="state",
                )

            target_version = expected_version if expected_version is not None else res.version
            stmt = (
                update(Reservation)
                .where(
                    Reservation.id == reservation_id,
                    Reservation.version == target_version,
                    Reservation.status.in_(ACTIVE_STATUSES),
                )
                .values(
                    status="cancelled",
                    cancel_reason=reason,
                    version=target_version + 1,
                    updated_at=now_local(),
                )
            )
            result = cast(CursorResult, await session.execute(stmt))
            if result.rowcount == 0:
                # 版本不匹配 = 有并发修改；重新读一次版本再试
                continue

            # ★ 释放占用格：不释放的话这个时段就永远订不回来了。
            # 与状态更新在同一事务里，避免"取消了但格子还占着"的不一致。
            await release_slots(session, reservation_id)

            equipment = await _load_equipment(session, res.equipment_id)
            await session.refresh(res)
            return BookingOutcome(
                ok=True,
                message=f"已取消预约：{res.slot_label}",
                reservation=_to_out(
                    res,
                    equipment.name if equipment else "",
                    equipment.lab.label if equipment and equipment.lab else "",
                ),
                retries=attempt,
                reason="ok",
            )

    return BookingOutcome(
        ok=False,
        message=f"取消失败：已重试 {limit} 次仍有并发修改",
        retries=limit,
        # contention：「取消失败」以前是一句人话，谁也答不出它到底是
        # 权限问题还是并发打架。分类之后这两件事在指标上是分开的。
        reason="contention",
    )


# --------------------------------------------------------------------------
# 审批（P1-5）
# --------------------------------------------------------------------------
@_counted(record_review_outcome)
async def decide_reservation(
    *,
    reservation_id: int,
    approve: bool,
    reason: str = "",
) -> BookingOutcome:
    """管理员通过 / 驳回一条 ``pending`` 申请。

    与 :func:`cancel_reservation` 同一套写法（条件 UPDATE + 乐观锁）：
    两个管理员同时点"通过"和"驳回"时，由数据库裁决谁生效，
    而不是"各读各的、各写各的"。

    驳回与"用户自己取消"是**同一条路径**（``cancelled`` + 释放占用格），
    所以"驳回了但格子还占着"这种不一致不可能出现 —— 释放只有一处实现。
    """
    for attempt in range(get_settings().booking_max_retry):
        async with session_scope() as session:
            res = await session.get(Reservation, reservation_id)
            if res is None:
                return BookingOutcome(
                    ok=False,
                    message=f"预约 {reservation_id} 不存在",
                    retries=attempt,
                    reason="not_found",
                )
            if res.status != STATUS_PENDING:
                # 已经处理过了。这不算冲突（不是两个请求抢同一资源），
                # 而是"状态不对" —— 最常见的成因是管理员在另一个标签页点过了。
                return BookingOutcome(
                    ok=False,
                    message=f"该申请当前状态为 {res.status}，无需再处理",
                    retries=attempt,
                    reason="state",
                )

            target_status = STATUS_CONFIRMED if approve else STATUS_CANCELLED
            stmt = (
                update(Reservation)
                .where(
                    Reservation.id == reservation_id,
                    Reservation.version == res.version,
                    Reservation.status == STATUS_PENDING,
                )
                .values(
                    status=target_status,
                    cancel_reason=reason if not approve else "",
                    version=res.version + 1,
                    updated_at=now_local(),
                )
            )
            result = cast(CursorResult, await session.execute(stmt))
            if result.rowcount == 0:
                continue  # 版本被别人改过，重读再试

            if not approve:
                # ★ 驳回必须释放占用格，否则这个时段永远订不回来
                await release_slots(session, reservation_id)

            equipment = await _load_equipment(session, res.equipment_id)
            await session.refresh(res)
            out = _to_out(
                res,
                equipment.name if equipment else "",
                equipment.lab.label if equipment and equipment.lab else "",
            )
            return BookingOutcome(
                ok=True,
                message=(
                    f"已通过：{out.slot}"
                    if approve
                    else f"已驳回：{out.slot}" + (f"（{reason}）" if reason else "")
                ),
                reservation=out,
                retries=attempt,
                reason="ok",
            )

    return BookingOutcome(
        ok=False,
        message="审批失败：有并发修改，请重试",
        retries=get_settings().booking_max_retry,
        reason="contention",
    )


async def pending_reservations(session: AsyncSession) -> list[ReservationOut]:
    """待审批列表（管理员后台用）。

    ⚠️ 不能图省事复用 :func:`list_reservations` 再在内存里过滤：它返回的是
    **全量**，而"待审批"接口一旦返回全部预约，管理员在页面上就分不清
    哪些要处理 —— 这正是"省一个函数"的代价。
    """
    rows = (
        await session.execute(
            select(Reservation)
            .where(Reservation.status == STATUS_PENDING)
            .order_by(Reservation.date, Reservation.start_time)
        )
    ).scalars().all()
    return await _to_outs(session, rows)


async def list_reservations(
    session: AsyncSession,
    *,
    user_id: int | None = None,
    date_: dt.date | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[ReservationOut]:
    """查询预约。

    ``limit`` / ``offset`` 是 P2 补的：单院系的数据量下它不是瓶颈，
    但"接口一次把全表读进内存"是个**迟早会炸**的形状 ——
    而且炸的时候表现是"服务变慢、然后 OOM"，不会有人想到是分页没做。
    """
    stmt = select(Reservation).order_by(Reservation.date.desc(), Reservation.start_time)
    if user_id is not None:
        stmt = stmt.where(Reservation.user_id == user_id)
    if date_ is not None:
        stmt = stmt.where(Reservation.date == date_)
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    rows = (await session.execute(stmt)).scalars().all()
    return await _to_outs(session, rows)


async def count_reservations(
    session: AsyncSession, *, user_id: int | None = None
) -> int:
    """配合分页：总数不跟着页走，否则前端算不出"还有几页"。"""
    stmt = select(func.count()).select_from(Reservation)
    if user_id is not None:
        stmt = stmt.where(Reservation.user_id == user_id)
    return int(await session.scalar(stmt) or 0)


async def _to_outs(
    session: AsyncSession, rows: Sequence[Reservation]
) -> list[ReservationOut]:
    """把预约行批量转成对外结构（补上设备名与实验室名）。"""
    if not rows:
        return []

    # 一次把用到的设备、实验室与用户全捞出来，避免逐行 get 造成 N+1
    equipment_ids = {row.equipment_id for row in rows}
    equip_stmt = (
        select(Equipment)
        .options(selectinload(Equipment.lab))
        .where(Equipment.id.in_(equipment_ids))
    )
    equipment_map = {
        item.id: item for item in (await session.execute(equip_stmt)).scalars().all()
    }
    # 申请人名字：管理员的审批 / 违约列表要显示"谁在申请"，缺了它那个列表没法用
    # （试运行时对着一个只有设备和时段的待办队列才发现这件事）。
    # 只查列表里真正出现过的用户，不整表扫。
    user_ids = {row.user_id for row in rows}
    user_map = {
        item.id: item
        for item in (await session.execute(select(User).where(User.id.in_(user_ids))))
        .scalars()
        .all()
    }

    out: list[ReservationOut] = []
    for row in rows:
        equipment = equipment_map.get(row.equipment_id)
        out.append(
            _to_out(
                row,
                equipment.name if equipment else "",
                equipment.lab.label if equipment and equipment.lab else "",
                user_map[row.user_id].username if row.user_id in user_map else "",
            )
        )
    return out
