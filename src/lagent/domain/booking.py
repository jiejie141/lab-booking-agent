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
from typing import cast

from sqlalchemy import CursorResult, delete, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..clock import minutes_between, now_local, overlaps
from ..config import get_settings
from ..db import session_scope
from ..models import (
    ACTIVE_STATUSES,
    DEFAULT_SLOT_GRANULARITY_MINUTES,
    EQUIPMENT_NORMAL,
    Equipment,
    Reservation,
    ReservationSlot,
    User,
    is_aligned,
    slot_indexes_for,
)
from ..schemas import BookingOutcome, ReservationOut


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


def _to_out(res: Reservation, equipment_name: str = "", lab_label: str = "") -> ReservationOut:
    return ReservationOut(
        id=res.id,
        equipment_id=res.equipment_id,
        equipment_name=equipment_name,
        lab_label=lab_label,
        date=res.date,
        start_time=res.start_time,
        end_time=res.end_time,
        status=res.status,
        purpose=res.purpose,
        slot=res.slot_label,
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
                    return BookingOutcome(ok=False, message=f"用户 {user_id} 不存在", retries=attempt)

                equipment = await _load_equipment(session, equipment_id)
                if equipment is None:
                    return BookingOutcome(
                        ok=False, message=f"设备 {equipment_id} 不存在", retries=attempt
                    )

                problems = await _validate(session, user, equipment, date_, start, end)
                if problems:
                    return BookingOutcome(
                        ok=False, message="；".join(problems), retries=attempt
                    )

                if dt.datetime.combine(date_, start) <= now:
                    return BookingOutcome(
                        ok=False, message="该时间点已经过去，请选择之后的时间", retries=attempt
                    )

                # 锁内复检：这是「不被别人抢走」的实际判据
                conflict = await find_conflict(session, equipment_id, date_, start, end)
                if conflict is not None:
                    return BookingOutcome(
                        ok=False,
                        message=f"该时段刚被占用：{conflict.slot_label}",
                        retries=attempt,
                        conflict_with=_to_out(conflict, equipment.name, equipment.lab.label),
                    )

                res = Reservation(
                    user_id=user_id,
                    equipment_id=equipment_id,
                    date=date_,
                    start_time=start,
                    end_time=end,
                    status="confirmed",
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
                    message=f"预约成功：{out.slot} {equipment.name}（{equipment.lab.label}）",
                    reservation=out,
                    retries=attempt,
                )

        except IntegrityError:
            # 占用格唯一索引拦下了重叠（或同坑重复）—— 抢坑的是并发请求，重试即可。
            # 注意：重试前事务已回滚，不会留下半条预约或半个占用格。
            await asyncio.sleep(0.005 * (attempt + 1))
            continue

    return BookingOutcome(
        ok=False,
        message=f"并发冲突，已重试 {settings.booking_max_retry} 次仍未成功，请稍后再试",
        retries=retries,
    )


# --------------------------------------------------------------------------
# 取消（乐观锁）
# --------------------------------------------------------------------------
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
                    ok=False, message=f"预约 {reservation_id} 不存在", retries=attempt
                )
            if res.user_id != user_id:
                return BookingOutcome(
                    ok=False, message="只能取消自己的预约", retries=attempt
                )
            if res.status not in ACTIVE_STATUSES:
                return BookingOutcome(
                    ok=False,
                    message=f"该预约当前状态为 {res.status}，无需取消",
                    retries=attempt,
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
            )

    return BookingOutcome(
        ok=False, message=f"取消失败：已重试 {limit} 次仍有并发修改", retries=limit
    )


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------
async def list_reservations(
    session: AsyncSession, *, user_id: int | None = None, date_: dt.date | None = None
) -> list[ReservationOut]:
    stmt = select(Reservation).order_by(Reservation.date.desc(), Reservation.start_time)
    if user_id is not None:
        stmt = stmt.where(Reservation.user_id == user_id)
    if date_ is not None:
        stmt = stmt.where(Reservation.date == date_)
    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        return []

    # 一次把用到的设备与实验室全捞出来，避免逐行 get 造成 N+1
    equipment_ids = {row.equipment_id for row in rows}
    equip_stmt = (
        select(Equipment)
        .options(selectinload(Equipment.lab))
        .where(Equipment.id.in_(equipment_ids))
    )
    equipment_map = {
        item.id: item for item in (await session.execute(equip_stmt)).scalars().all()
    }

    out: list[ReservationOut] = []
    for row in rows:
        equipment = equipment_map.get(row.equipment_id)
        out.append(
            _to_out(
                row,
                equipment.name if equipment else "",
                equipment.lab.label if equipment and equipment.lab else "",
            )
        )
    return out
