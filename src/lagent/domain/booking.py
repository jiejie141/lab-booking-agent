"""下单与取消：并发安全 + 业务规则兜底。

三层防线，缺一不可：

1. **写前校验**：即便参数是模型给的，也重新过一遍全部非时间约束
   （设备状态、准入资质、开放时间、单次上限）。
   模型输出不可信，业务规则绝不能交给它最终裁决。

2. **设备级串行化**：时间区间重叠只能在事务里判。READ COMMITTED 下
   两个事务可以各自查完「没冲突」再双双写入，这就是 check-then-act 漏判。
   PostgreSQL 上用 ``pg_advisory_xact_lock`` 按设备加事务级排他锁；
   SQLite 本身同一时刻只允许一个写事务，写入即排他，无需额外加锁。

3. **数据库唯一索引**：同设备 + 同日期 + 同开始时间的有效预约唯一
   （见 models.Reservation 的部分唯一索引）。这是最后一道闸 —— 前两层
   万一有缝隙，重复写入会被数据库直接拒绝，而不是悄悄写进去两行。

发生第 3 层拦截时不是失败退出，而是**重试**：因为此时抢到坑位的很可能是
另一个并发请求，重试前会重新查一次冲突，把「被别人抢了」如实回报给用户。
"""

from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..config import get_settings
from ..db import session_scope
from ..models import (
    ACTIVE_STATUSES,
    EQUIPMENT_NORMAL,
    Equipment,
    Reservation,
    User,
)
from ..schemas import BookingOutcome, ReservationOut
from ..clock import minutes_between, now_local, overlaps


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


async def acquire_equipment_lock(session: AsyncSession, equipment_id: int) -> str:
    """按设备取事务级排他锁（仅 PostgreSQL 需要）。返回所用策略，便于测试断言。"""
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        # hashtext 把设备号映射成 advisory lock 的 key；_xact_ 版本随事务结束自动释放
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"lagent:equipment:{equipment_id}"},
        )
        return "pg_advisory_xact_lock"
    if dialect == "sqlite":
        # SQLite 的写事务天然互斥：写锁生效期间其他写事务会被阻塞在 busy_timeout 上。
        # 这里不需要也不应该再叠一层应用层锁 —— 单进程内的 asyncio 锁在多进程部署下无效，
        # 反而会给人「已经保护好了」的错觉。
        return "sqlite_write_lock"
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
                # flush 才会真正打数据库，唯一索引冲突在这一刻抛出
                await session.flush()
                out = _to_out(res, equipment.name, equipment.lab.label)
                return BookingOutcome(
                    ok=True,
                    message=f"预约成功：{out.slot} {equipment.name}（{equipment.lab.label}）",
                    reservation=out,
                    retries=attempt,
                )

        except IntegrityError:
            # 第 3 层防线拦下了「同坑重复写入」—— 抢坑的是并发请求，重试即可
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
            result = await session.execute(stmt)
            if result.rowcount == 0:
                # 版本不匹配 = 有并发修改；重新读一次版本再试
                continue

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
