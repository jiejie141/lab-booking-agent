"""协商：约束冲突时给出**取舍后的备选**，而不是一句「不可预约」。

教学版的做法是「查有没有冲突 → 有就说不行」，把换条件的活留给用户。
这里的做法是把诉求看作一组**可逐级放宽的约束**，按代价从低到高走一遍阶梯，
每上一个台阶就把「我放宽了哪一条」明确写进 ``Proposal.relaxations``：

    阶梯 A  shift             同设备、同时长，只挪时间
    阶梯 B  shorten           同设备，缩短时长以塞进更小的空隙
    阶梯 C  switch_equipment  换本实验室里的同类设备
    阶梯 D  switch_lab        换到别的实验室的同类设备

日期也是可放宽维度：同一天找不到就顺延 ±1、+2 天。日期偏移、时间偏移、
时长缩水、换设备、换实验室各自折算成扣分，最后按「与原始诉求的接近度」排序。

这样做的直接好处是：用户看到的是「3 小时约不到，但 2 小时可以；或者换到
隔壁那台可以满足 3 小时」这种可比较的权衡，而不是一个死胡同。

返回条数有上限，但截断方式很讲究：**每种放宽类别至少保留一条**（见 _select_diverse）。
只按分数截断的话，「挪时间」常常霸榜，用户会看到一屏同质选项，误以为没有别的路。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from ..clock import minutes_between, overlaps
from ..models import User
from ..schemas import (
    ConstraintCheck,
    NegotiationResult,
    Proposal,
    ProposalKind,
    Requirement,
)
from .availability import (
    EquipmentView,
    busy_intervals,
    evaluate,
    free_windows,
    load_active_reservations,
    load_equipment_index,
    matches_target,
    open_window,
    place_slots,
    summarize_failures,
)

# 阶梯尝试的日期偏移，按优先级排列；0 是同一天。
DATE_OFFSETS: tuple[int, ...] = (0, 1, -1, 2)


def _add_minutes(value: dt.time, minutes: int) -> dt.time:
    total = value.hour * 60 + value.minute + minutes
    return dt.time(hour=(total // 60) % 24, minute=total % 60)


def _closest_start(
    preferred: dt.time | None,
    window_start: dt.time,
    window_end: dt.time,
    hours: float,
) -> dt.time:
    """在 [window_start, window_end - hours] 这个可行区间里，取离 preferred 最近的起点。

    这就是协商质量的核心：同一个「可用」的判定，落点选得好不好，决定用户
    看到的是「能接受的替代」还是「等于没有」。
    """
    window_start_m = window_start.hour * 60 + window_start.minute
    latest_m = (window_end.hour * 60 + window_end.minute) - int(hours * 60)
    if latest_m < window_start_m:
        return window_start
    if preferred is None:
        return window_start
    preferred_m = preferred.hour * 60 + preferred.minute
    if preferred_m < window_start_m:
        return window_start
    if preferred_m > latest_m:
        return _add_minutes(window_start, latest_m - window_start_m)
    return preferred


def _shift_minutes(base: dt.time | None, actual: dt.time) -> int:
    if base is None:
        return 0
    return abs((actual.hour * 60 + actual.minute) - (base.hour * 60 + base.minute))


def _score(
    *,
    date_offset: int,
    shift: int,
    duration_ratio: float,
    equipment_changed: bool,
    lab_changed: bool,
) -> float:
    """与原始诉求的接近度，落在 (0, 1]。各项权重是主观定的，但**公开且可复算**。"""
    score = 1.0
    score -= 0.25 * min(abs(date_offset), 2) / 2.0
    score -= 0.20 * min(shift / 240.0, 1.0)
    score -= 0.30 * max(0.0, 1.0 - duration_ratio)
    if equipment_changed:
        score -= 0.15
    if lab_changed:
        score -= 0.10
    return round(max(score, 0.05), 2)


async def negotiate(
    session: AsyncSession,
    user: User,
    req: Requirement,
    *,
    now: dt.datetime | None = None,
    max_proposals: int = 5,
) -> NegotiationResult:
    """主入口：能全满足就给精确候选，不能就给放宽后的备选。"""
    cur = req.normalized()
    if cur.date is None:
        return NegotiationResult(
            satisfied=False,
            blockers=["还没有确定日期，无法计算任何候选时段"],
            blocker_kind="no_date",
        )

    views = await load_equipment_index(session)
    targeted = [ev for ev in views if matches_target(ev, cur)]
    if not targeted:
        return NegotiationResult(
            satisfied=False,
            blockers=[f"没有找到匹配「{cur.equipment_name or cur.category}」的设备"],
            blocker_kind="no_target",
        )

    # 需要看邻近日期，才能给出「顺延一天」这类备选
    dates = [cur.date + dt.timedelta(days=offset) for offset in DATE_OFFSETS]
    reservations = await load_active_reservations(session, dates)

    # ---- 先试「完整满足」 -------------------------------------------------
    exact: list[Proposal] = []
    for ev in targeted:
        for start, end in _slots_for(ev, user, cur, reservations, cur.date, now=now):
            hours = minutes_between(start, end) / 60
            exact.append(
                Proposal(
                    kind="exact",
                    equipment_id=ev.equipment.id,
                    equipment_name=ev.equipment.name,
                    lab_label=ev.lab.label,
                    date=cur.date,
                    start=start,
                    end=end,
                    hours=hours,
                    score=1.0,
                    reason="完全满足你的全部条件",
                )
            )
    if exact:
        exact.sort(key=lambda p: (p.start, p.equipment_id))
        return NegotiationResult(
            satisfied=True,
            checks=_checks_for_best(views, targeted, user, cur, reservations, now=now),
            proposals=exact[:max_proposals],
            blocker_kind="none",
        )

    # ---- 走放宽阶梯 -------------------------------------------------------
    # 想要类别的取值域：点名了类别就用类别；点了具体设备就用那台设备的类别；
    # 什么都没说（比如「随便哪台都行」）则不限制类别。
    if cur.category:
        wanted_categories = {cur.category}
    elif cur.equipment_name:
        wanted_categories = {ev.equipment.category for ev in targeted}
    else:
        wanted_categories = {ev.equipment.category for ev in views}

    hinted_labs = {ev.lab.id for ev in targeted}
    proposals: list[Proposal] = []

    for offset in DATE_OFFSETS:
        day = cur.date + dt.timedelta(days=offset)
        if offset != 0 and day < (now or dt.datetime.now()).date():
            # 只往前看未来，不回退到已经过去的日期
            continue
        day_req = cur.model_copy(update={"date": day})
        date_relax = (
            []
            if offset == 0
            else [f"日期由 {cur.date.isoformat()} 调整为 {day.isoformat()}"]
        )

        for tier in ("shift", "shorten", "switch_equipment", "switch_lab"):
            for ev in views:
                category_ok = ev.equipment.category in wanted_categories
                same_lab = ev.lab.id in hinted_labs
                if tier in ("shift", "shorten"):
                    if not matches_target(ev, cur):
                        continue
                elif tier == "switch_equipment":
                    if not category_ok or not same_lab or matches_target(ev, cur):
                        continue
                else:  # switch_lab
                    if not category_ok or same_lab:
                        continue

                for start, end, relax in _place_with_relaxation(
                    ev, user, day_req, reservations, tier
                ):
                    hours = minutes_between(start, end) / 60
                    if hours <= 0:
                        continue
                    # 放宽说明必须逐项写全：日期、设备、实验室、时间、时长。
                    # 只说「日期变了」而不提「换了一台设备」，用户会以为下的还是原来那台。
                    relaxations = list(date_relax)
                    if not matches_target(ev, cur):
                        relaxations.append(f"改用同类设备「{ev.equipment.name}」")
                    if ev.lab.id not in hinted_labs:
                        relaxations.append(f"改到 {ev.lab.label}（其他实验室）")
                    relaxations += relax
                    if not relaxations:
                        # 什么都没放宽却又不是精确解，说明这条不该出现在阶梯里
                        continue
                    equipment_changed = not matches_target(ev, cur)
                    proposals.append(
                        Proposal(
                            kind=_kind_of(tier, offset),
                            equipment_id=ev.equipment.id,
                            equipment_name=ev.equipment.name,
                            lab_label=ev.lab.label,
                            date=day,
                            start=start,
                            end=end,
                            hours=hours,
                            relaxations=relaxations,
                            score=_score(
                                date_offset=offset,
                                shift=_shift_minutes(cur.start, start),
                                duration_ratio=(
                                    hours / cur.duration_hours
                                    if cur.duration_hours
                                    else 1.0
                                ),
                                equipment_changed=equipment_changed,
                                lab_changed=ev.lab.id not in hinted_labs,
                            ),
                            reason=_reason_of(tier),
                        )
                    )

    # 同一时段可能被多个阶梯重复产生，去重后按接近度排序
    proposals = _select_diverse(_dedupe(proposals), max_proposals)
    checks = _checks_for_best(views, targeted, user, cur, reservations, now=now)
    # 走到这里就说明「原诉求被某条约束挡下了」，所以分类一律是 constraint，
    # 具体卡在哪一条由 checks 交代（回复层据此区分「缺资质」和「时段被占」）。
    return NegotiationResult(
        satisfied=False,
        checks=checks,
        proposals=proposals,
        blockers=_blockers_of(views, targeted, user, cur, reservations, now=now),
        blocker_kind="constraint",
    )


# --------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------
def _kind_of(tier: str, offset: int) -> ProposalKind:
    if tier == "shorten":
        return "shorten"
    if tier == "switch_equipment":
        return "switch_equipment"
    if tier == "switch_lab":
        return "switch_lab"
    # 同设备换时间：日期变了也仍然算 shift
    return "shift"


def _reason_of(tier: str) -> str:
    return {
        "shift": "同设备，把时间挪到你可用且空闲的时段",
        "shorten": "同设备，缩短时长以塞进现有空隙",
        "switch_equipment": "换用本实验室里的同类设备",
        "switch_lab": "换到其他实验室的同类设备",
    }[tier]


def _slots_for(
    ev: EquipmentView,
    user: User,
    req: Requirement,
    reservations: dict[tuple[int, dt.date], list],
    date_: dt.date,
    *,
    now: dt.datetime | None,
) -> list[tuple[dt.time, dt.time]]:
    """在「所有非时间约束都通过」的前提下，返回该设备当天可行的时段。"""
    todays = reservations.get((ev.equipment.id, date_), [])
    checks = evaluate(ev, user, req, todays, now=now)
    # 只让「时间之外」的约束决定能不能用这台设备；时间问题交给 place_slots
    blocking = [c for c in summarize_failures(checks) if c.name != "conflict"]
    if blocking:
        return []
    window = open_window(ev.lab, date_)
    free = free_windows(window, busy_intervals(todays))
    return place_slots(free, req, float(ev.equipment.max_hours))


def _place_with_relaxation(
    ev: EquipmentView,
    user: User,
    req: Requirement,
    reservations: dict[tuple[int, dt.date], list],
    tier: str,
) -> list[tuple[dt.time, dt.time, list[str]]]:
    """按阶梯级别放宽并落位，返回 (开始, 结束, 放宽说明)。"""
    todays = reservations.get((ev.equipment.id, req.date), [])
    checks = evaluate(ev, user, req, todays, now=None)
    blocking = [c for c in summarize_failures(checks) if c.name != "conflict"]
    if tier == "shorten":
        # 「缩短时长」这一档的存在意义就是化解 max_hours 冲突：
        # 用户要 6 小时而设备单次上限 2 小时，正确的处理不是直接拒绝，
        # 而是给出「6 小时不行，2 小时可以」这种可比较的替代。
        # 其余档位仍然被 max_hours 拦住 —— 它们不改时长，改了也没用。
        blocking = [c for c in blocking if c.name != "max_hours"]
    if blocking:
        return []

    window = open_window(ev.lab, req.date)
    free = free_windows(window, busy_intervals(todays))
    if not free:
        return []

    limit = float(ev.equipment.max_hours)
    want = req.duration_hours or limit

    results: list[tuple[dt.time, dt.time, list[str]]] = []

    if tier == "shift":
        # 诉求时长不变，只换位置。关键是**选离原诉求最近的落点**，不是窗口开头：
        # 用户说要 14:00，窗口 08:00-14:00 里最近的可行解是 12:00-14:00；
        # 直接取窗口开头会给出 08:00-10:00 —— 技术上「可用」，但用户根本不会接受。
        for window_start, window_end in free:
            if minutes_between(window_start, window_end) / 60 < want:
                continue
            start = _closest_start(req.start, window_start, window_end, want)
            end = _add_minutes(start, int(want * 60))
            if not _same_place(req, start, end):
                note = []
                if req.start and start != req.start:
                    note.append(
                        f"起始时间由 {req.start.strftime('%H:%M')} 调整为 {start.strftime('%H:%M')}"
                    )
                if req.end and end != req.end:
                    note.append(
                        f"结束时间由 {req.end.strftime('%H:%M')} 调整为 {end.strftime('%H:%M')}"
                    )
                results.append((start, end, note))
        return results

    if tier == "shorten":
        # 时长可缩：在每个空闲窗口里能塞多长就多长（不超过设备上限）。
        for window_start, window_end in free:
            available = minutes_between(window_start, window_end) / 60
            hours = min(available, limit)
            if hours <= 0:
                continue
            if req.duration_hours and hours >= req.duration_hours:
                # 能完整满足就不算「缩短」，交给 shift 阶梯
                continue
            start = _closest_start(req.start, window_start, window_end, hours)
            end = _add_minutes(start, int(hours * 60))
            note = []
            if req.duration_hours:
                note.append(f"时长由 {req.duration_hours:g} 小时缩短为 {hours:g} 小时")
            else:
                note.append(f"时长按可用空隙定为 {hours:g} 小时")
            results.append((start, end, note))
        return results

    # switch_equipment / switch_lab：时间照原诉求试，不行就退化为「挪到当天最早可用」
    original = [
        (start, end, [])
        for start, end in place_slots(free, req, limit)
    ]
    if original:
        return original
    for window_start, window_end in free:
        available = minutes_between(window_start, window_end) / 60
        hours = min(available, want, limit)
        if hours <= 0:
            continue
        start = window_start
        end = _add_minutes(start, int(hours * 60))
        note = [f"时段改为 {start.strftime('%H:%M')}-{end.strftime('%H:%M')}"]
        if req.duration_hours and hours < req.duration_hours:
            note.append(f"时长同时由 {req.duration_hours:g} 小时缩短为 {hours:g} 小时")
        return [(start, end, note)]
    return []


def _same_place(req: Requirement, start: dt.time, end: dt.time) -> bool:
    if req.start and req.end:
        return start == req.start and end == req.end
    return False


def _dedupe(proposals: Sequence[Proposal]) -> list[Proposal]:
    """同 (设备, 日期, 开始, 结束) 只留分最高的那条。"""
    best: dict[tuple, Proposal] = {}
    for p in proposals:
        key = (p.equipment_id, p.date, p.start, p.end)
        if key not in best or p.score > best[key].score:
            best[key] = p
    return list(best.values())


def _select_diverse(proposals: Sequence[Proposal], limit: int) -> list[Proposal]:
    """按分数取前 N 条，但**保证每种放宽类别至少露一次脸**。

    这一步不是锦上添花，而是协商能不能用的分水岭。只按分数截断会出这种事故：
    「挪时间」这一类的候选往往分数最高，于是前 5 条全是不同时间的同一种取舍，
    而真正对用户价值最大的「缩短时长就能用上这台」被挤出了榜单 ——
    用户看到的是一屏重复选项，却以为没有别的路可走。

    所以先按类别各取最优（shift → shorten → switch_equipment → switch_lab），
    再用剩余名额按分数补齐，最后统一按分数排序返回。
    """
    ordered = sorted(proposals, key=lambda p: (-p.score, p.date, p.start))
    if len(ordered) <= limit:
        return ordered

    picked: list[Proposal] = []
    seen_kinds: set[str] = set()
    for p in ordered:
        if len(picked) >= limit:
            break
        if p.kind not in seen_kinds:
            picked.append(p)
            seen_kinds.add(p.kind)

    chosen = {id(p) for p in picked}
    for p in ordered:
        if len(picked) >= limit:
            break
        if id(p) not in chosen:
            picked.append(p)
            chosen.add(id(p))

    picked.sort(key=lambda p: (-p.score, p.date, p.start))
    return picked


def _checks_for_best(
    views: Sequence[EquipmentView],
    targeted: Sequence[EquipmentView],
    user: User,
    req: Requirement,
    reservations: dict[tuple[int, dt.date], list],
    *,
    now: dt.datetime | None,
) -> list[ConstraintCheck]:
    """挑一台最接近诉求的设备，把它的约束判定摊开给用户看。"""
    probe = targeted[0]
    todays = reservations.get((probe.equipment.id, req.date), [])
    return evaluate(probe, user, req, todays, now=now)


def _blockers_of(
    views: Sequence[EquipmentView],
    targeted: Sequence[EquipmentView],
    user: User,
    req: Requirement,
    reservations: dict[tuple[int, dt.date], list],
    *,
    now: dt.datetime | None,
) -> list[str]:
    """汇总「为什么原方案不行」，去重后按出现顺序返回。"""
    seen: list[str] = []
    for ev in targeted:
        todays = reservations.get((ev.equipment.id, req.date), [])
        for check in summarize_failures(evaluate(ev, user, req, todays, now=now)):
            text = check.detail
            if text not in seen:
                seen.append(text)
    return seen
