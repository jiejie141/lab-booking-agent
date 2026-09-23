"""多约束联合查询。

一条预约诉求要同时穿过 6 道约束，本模块负责把它们逐条判定并给出**可读的理由**，
而不是笼统地回一句「不可预约」：

    equipment_status  设备是否可用（非维护 / 非报废）
    training          用户是否具备该类别准入资质
    open_hours        是否落在实验室开放时间内
    max_hours         是否超出该设备单次最长时长
    capacity          房间容量是否够
    conflict          是否与已有有效预约重叠

判定与「找可用时段」分开：前者回答「为什么不行」，后者回答「那什么时候行」。
两者都需要的场景（协商）再去组合，见 negotiate.py。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..clock import (
    fmt_time,
    minutes_between,
    overlaps,
    parse_time,
    weekday_key,
)
from ..models import (
    ACTIVE_STATUSES,
    EQUIPMENT_NORMAL,
    Equipment,
    Laboratory,
    Reservation,
    User,
)
from ..schemas import AvailabilityCandidate, ConstraintCheck, Requirement


@dataclass(frozen=True)
class EquipmentView:
    """设备 + 它所在实验室的只读视图，避免在遍历里反复触发懒加载。"""

    equipment: Equipment
    lab: Laboratory

    @property
    def label(self) -> str:
        return f"{self.equipment.name}（{self.lab.label}）"


# --------------------------------------------------------------------------
# 数据装载
# --------------------------------------------------------------------------
async def load_equipment_index(session: AsyncSession) -> list[EquipmentView]:
    stmt = (
        select(Equipment)
        .options(selectinload(Equipment.lab))
        .order_by(Equipment.id)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [EquipmentView(equipment=e, lab=e.lab) for e in rows]


async def load_active_reservations(
    session: AsyncSession,
    dates: Iterable[dt.date],
    equipment_ids: Sequence[int] | None = None,
) -> dict[tuple[int, dt.date], list[Reservation]]:
    """按 (设备, 日期) 归拢有效预约。一次查完，避免 N+1。"""
    date_list = list(dict.fromkeys(dates))
    if not date_list:
        return {}
    stmt = select(Reservation).where(
        Reservation.date.in_(date_list),
        Reservation.status.in_(ACTIVE_STATUSES),
    )
    if equipment_ids:
        stmt = stmt.where(Reservation.equipment_id.in_(list(equipment_ids)))
    rows = (await session.execute(stmt)).scalars().all()

    grouped: dict[tuple[int, dt.date], list[Reservation]] = {}
    for row in rows:
        grouped.setdefault((row.equipment_id, row.date), []).append(row)
    return grouped


# --------------------------------------------------------------------------
# 开放时间与空闲窗口
# --------------------------------------------------------------------------
def open_window(lab: Laboratory, date_: dt.date) -> tuple[dt.time, dt.time] | None:
    raw = (lab.open_hours or {}).get(weekday_key(date_))
    if not raw or len(raw) != 2:
        return None
    start, end = parse_time(str(raw[0])), parse_time(str(raw[1]))
    return (start, end) if end > start else None


def busy_intervals(reservations: Sequence[Reservation]) -> list[tuple[dt.time, dt.time]]:
    """把有效预约压成互不重叠的占用区间，便于做区间减法。"""
    items = sorted(
        ((r.start_time, r.end_time) for r in reservations if r.is_active),
        key=lambda pair: pair[0],
    )
    merged: list[tuple[dt.time, dt.time]] = []
    for start, end in items:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def free_windows(
    window: tuple[dt.time, dt.time] | None,
    busy: Sequence[tuple[dt.time, dt.time]],
) -> list[tuple[dt.time, dt.time]]:
    """开放窗口减去占用区间 = 空闲窗口。"""
    if window is None:
        return []
    open_start, open_end = window
    free: list[tuple[dt.time, dt.time]] = []
    cursor = open_start
    for busy_start, busy_end in busy:
        if busy_start > cursor:
            free.append((cursor, min(busy_start, open_end)))
        cursor = max(cursor, busy_end)
    if cursor < open_end:
        free.append((cursor, open_end))
    return [(s, e) for s, e in free if minutes_between(s, e) > 0]


def place_slots(
    free: Sequence[tuple[dt.time, dt.time]],
    req: Requirement,
    max_hours: float,
) -> list[tuple[dt.time, dt.time]]:
    """把诉求放进空闲窗口，返回所有**完整满足原诉求**的 (开始, 结束)。

    三种情形：
      * 诉求已给死起止时间 —— 只检查是否被某个空闲窗口完整容纳；
      * 诉求给了起始时间与时长 —— 从起始时间往后量，必须落在同一窗口内；
      * 只给了日期与时长 —— 在每个空闲窗口内取该窗口的最早可行位置。

    注意本函数**只做严格匹配，不做任何削弱**：要 3 小时而窗口只剩 2 小时就是无解，
    返回空列表，把「缩短时长」「挪时间」这类取舍交给 negotiate.py ——
    否则会出现最难发现的那种体验问题：用户要 3 小时，系统默默给了 2 小时却不吭声。
    """
    cur = req.normalized()
    if not cur.start and not cur.end:
        duration = cur.duration_hours
        if not duration:
            return []
        if max_hours and duration > max_hours:
            # 原诉求本身就超出设备单次上限 —— 不是「没位置」，是「要得太多」。
            # 同样交给协商层去给「6 小时不行，但 2 小时可以」这类可比较的替代。
            return []
        placed: list[tuple[dt.time, dt.time]] = []
        for window_start, window_end in free:
            if minutes_between(window_start, window_end) >= duration * 60:
                placed.append((window_start, _add_minutes(window_start, int(duration * 60))))
        return placed

    if cur.start and cur.end:
        return [
            (cur.start, cur.end)
            for s, e in free
            if s <= cur.start and cur.end <= e
        ]

    # 有起始时间、缺结束时间：按剩余的空闲窗口长度决定能约多久（上限 max_hours）
    if cur.start:
        for s, e in free:
            if s <= cur.start < e:
                available = minutes_between(cur.start, e) / 60
                want = cur.duration_hours or min(max_hours, available)
                hours = min(want, max_hours, available)
                if hours <= 0:
                    return []
                return [(cur.start, _add_minutes(cur.start, int(hours * 60)))]
    return []


def _add_minutes(value: dt.time, minutes: int) -> dt.time:
    total = value.hour * 60 + value.minute + minutes
    return dt.time(hour=(total // 60) % 24, minute=total % 60)


# --------------------------------------------------------------------------
# 约束判定
# --------------------------------------------------------------------------
def matches_target(ev: EquipmentView, req: Requirement) -> bool:
    """设备是否落在用户点名范围内。名字做双向包含匹配，容忍「光谱」对上「光谱仪」。"""
    cur = req.normalized()
    if cur.equipment_name:
        want = cur.equipment_name.replace(" ", "")
        name = ev.equipment.name.replace(" ", "")
        if want not in name and name not in want:
            return False
    return not (cur.category and cur.category != ev.equipment.category)


def evaluate(
    ev: EquipmentView,
    user: User,
    req: Requirement,
    todays: Sequence[Reservation],
    *,
    now: dt.datetime | None = None,
) -> list[ConstraintCheck]:
    """逐条判定约束，返回全部结果（含通过的），便于前端完整展示。"""
    cur = req.normalized()
    checks: list[ConstraintCheck] = []

    # 1. 设备状态
    ok = ev.equipment.status == EQUIPMENT_NORMAL
    checks.append(
        ConstraintCheck(
            name="equipment_status",
            passed=ok,
            detail="设备正常" if ok else f"设备当前为 {ev.equipment.status}，不可预约",
        )
    )

    # 2. 准入资质 —— 代码层判定，绝不让模型决定用户有没有资格
    needs = ev.equipment.requires_training
    has = ev.equipment.category in (user.certs or [])
    ok = (not needs) or has
    if not needs:
        detail = "该设备无需培训资质"
    elif has:
        detail = f"已具备「{ev.equipment.category}」准入资质"
    else:
        detail = f"缺少「{ev.equipment.category}」准入资质，需先通过培训"
    checks.append(ConstraintCheck(name="training", passed=ok, detail=detail))

    # 3. 开放时间
    window = open_window(ev.lab, cur.date) if cur.date else None
    ok = window is not None
    checks.append(
        ConstraintCheck(
            name="open_hours",
            passed=ok,
            detail=(
                f"开放 {fmt_time(window[0])}-{fmt_time(window[1])}"
                if window
                else "该实验室当天不开放"
            ),
        )
    )

    # 4. 单次最长时长
    hours = cur.duration_hours
    limit = float(ev.equipment.max_hours)
    ok = hours is None or hours <= limit
    checks.append(
        ConstraintCheck(
            name="max_hours",
            passed=ok,
            detail=(
                f"单次最长 {limit:g} 小时"
                if ok
                else f"单次最长 {limit:g} 小时，你的 {hours:g} 小时超出上限"
            ),
        )
    )

    # 5. 容量
    if cur.capacity:
        ok = ev.lab.capacity >= cur.capacity
        checks.append(
            ConstraintCheck(
                name="capacity",
                passed=ok,
                detail=(
                    f"房间可容纳 {ev.lab.capacity} 人"
                    if ok
                    else f"房间仅容纳 {ev.lab.capacity} 人，少于所需 {cur.capacity} 人"
                ),
            )
        )

    # 6. 冲突
    conflicts = [
        r for r in todays
        if r.is_active and cur.start and cur.end and overlaps(cur.start, cur.end, r.start_time, r.end_time)
    ]
    checks.append(
        ConstraintCheck(
            name="conflict",
            passed=not conflicts,
            detail=(
                "该时段空闲"
                if not conflicts
                else "已被占用：" + "、".join(sorted({r.slot_label for r in conflicts}))
            ),
        )
    )

    # 7. 是否已过期（只提示，不阻断判定本身）
    if now and cur.date and cur.start and dt.datetime.combine(cur.date, cur.start) <= now:
        checks.append(
            ConstraintCheck(
                name="conflict",
                passed=False,
                detail="该时间点已经过去，请改约之后的时间",
            )
        )
    return checks


def summarize_failures(checks: Sequence[ConstraintCheck]) -> list[ConstraintCheck]:
    return [c for c in checks if not c.passed]


# --------------------------------------------------------------------------
# 组装候选
# --------------------------------------------------------------------------
def search_candidates(
    views: Sequence[EquipmentView],
    user: User,
    req: Requirement,
    reservations: dict[tuple[int, dt.date], list[Reservation]],
    *,
    limit: int = 8,
    now: dt.datetime | None = None,
) -> list[AvailabilityCandidate]:
    """返回**完整满足原诉求**的候选时段（不做任何放宽）。"""
    cur = req.normalized()
    if cur.date is None:
        return []

    out: list[AvailabilityCandidate] = []
    for ev in views:
        if not matches_target(ev, cur):
            continue
        todays = reservations.get((ev.equipment.id, cur.date), [])
        checks = evaluate(ev, user, cur, todays, now=now)
        if summarize_failures(checks):
            continue
        window = open_window(ev.lab, cur.date)
        free = free_windows(window, busy_intervals(todays))
        for start, end in place_slots(free, cur, float(ev.equipment.max_hours)):
            hours = minutes_between(start, end) / 60
            out.append(
                AvailabilityCandidate(
                    equipment_id=ev.equipment.id,
                    equipment_name=ev.equipment.name,
                    equipment_code=ev.equipment.code,
                    category=ev.equipment.category,
                    lab_label=ev.lab.label,
                    date=cur.date,
                    start=start,
                    end=end,
                    hours=hours,
                )
            )
    out.sort(key=lambda c: (c.start, c.equipment_id))
    return out[:limit]
