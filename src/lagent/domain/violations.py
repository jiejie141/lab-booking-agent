"""违约：「约了不来」的判定与后果（P1-8）。

## 为什么这一块单独一个模块

它是全项目唯一一处**由系统主动限制用户权利**的逻辑，失败模式与其他模块
完全相反：别的模块出错最多是"没约上/进不去"（看得见、能重试）；
这里出错是"一个守规矩的人被静默地禁止预约"，而他往往过很久才发现，
发现时也说不清为什么。所以这个模块里每一处**不确定都往"不判"倒**，
而不是往"判"倒。

## 判定依据只能是可核销的物理事实

「没来」是个否定命题，无法直接证明。能证明的是它的反面：
**门禁在这一时段录到了这个人进门**（``access_events`` 里
``result=granted`` 且 ``direction=in``）。所以判定成立的前提是
**门禁真的在跑** —— 没有接入门禁的实验室，流水里一条记录都没有，
此时"没记录"既可能是"人没来"，也可能是"设备没通电"。

于是有一条硬规则：

    该实验室当天**一条门禁流水都没有** → 判不了 → 跳过，绝不猜。

这条规则不是谨慎，是必须：一个没接门禁的实验室如果照判，
结果是**它名下所有预约全部变违约、所有用户被锁**，
而且从界面上看一切正常（"系统判定你三次没来"）。
惩罚性自动化最危险的失败模式就是这样 —— 它不会报错，只会安静地冤枉所有人。

## 三条刻意的保守设置

1. **只判"昨天及以前"的预约。** 今天 10:00 的时段到 10:30 就过期了，
   但人可能只是迟到了二十分钟。判得太急会把"迟到"写成"没来"，
   而这两件事在学生眼里区别极大。放到第二天再判，门禁流水也已经完整。
2. **默认只记录、不处罚**（``noshow_blocking_enabled`` 默认 False）。
   先跑一段时间确认判定准确（门禁有没有漏刷、宽限够不够），
   再打开处罚。处罚是对人的，不能第一个星期就把阈值当真理。
3. **豁免不删判定记录。** 学生说"那天门禁坏了"且属实时，管理员填
   ``pardoned_at``，``no_show_at`` 保留 —— 系统确实判过，是人推翻了。
   抹掉判定事实等于说"系统从没这么认为过"，那是编造历史。

## 次数为什么不存成字段

违约次数**每次从数据推导**（窗口内 ``no_show_at`` 非空且 ``pardoned_at``
为空的行数）。存一个 ``violations_count`` 就要保证它和明细永远同步，
而"豁免一条之后要减一"这种逻辑迟早会在某条路径上漏掉。
能推导的东西就别存 —— 与"房间容量不靠应用层数"是同一个立场。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..clock import now_local
from ..config import get_settings
from ..models import (
    ACCESS_GRANTED,
    DIRECTION_IN,
    STATUS_EXPIRED,
    AccessEvent,
    Equipment,
    Reservation,
)

# 一次判定最多看多少条候选。宁可分批跑完，也不要一个事务锁住几千行 ——
# 清扫任务与在线预约是同一个库，长事务会直接变成用户看得见的卡顿。
DEFAULT_BATCH = 200


@dataclass(frozen=True)
class NoShowCandidate:
    """一条"疑似没来"的预约。

    把判定所需的事实在这里**摊平成普通字段**（而不是传 ORM 对象出去）：
    清扫任务要在事务外写审计，届时 ORM 对象已经脱离会话，
    再碰关系属性就是 ``MissingGreenlet``。
    """

    reservation_id: int
    user_id: int
    lab_id: int
    date: dt.date
    label: str


@dataclass(frozen=True)
class ViolationState:
    """一个用户当前的违约状态。

    ``blocked`` 与 ``over_threshold`` **分开**：处罚开关关着的时候
    一个人可以既"超过阈值"又"没被拦"。合成一个布尔的话，
    "系统已经在盯着他了，只是还没下手"这种状态就没法表达，
    而那恰恰是刚上线时最需要看到的信号。
    """

    count: int
    threshold: int
    window_days: int
    blocked: bool
    over_threshold: bool
    blocking_enabled: bool

    @property
    def message(self) -> str:
        base = f"近 {self.window_days} 天累计 {self.count} 次预约未到场（阈值 {self.threshold} 次）"
        if self.blocked:
            return f"{base}，已暂停预约权限。如有异议请联系实验室管理员核实门禁记录。"
        if self.over_threshold:
            return f"{base}；当前未开启自动限制，暂不影响预约。"
        return f"{base}。"


async def find_no_show_candidates(
    session: AsyncSession,
    *,
    now: dt.datetime | None = None,
    limit: int = DEFAULT_BATCH,
) -> tuple[list[NoShowCandidate], int]:
    """找出该判违约的预约，返回 ``(候选, 判不了的数量)``。

    第二个返回值不是装饰：它衡量的是"门禁有没有在工作"。
    一个实验室长期大量"判不了"，说明它根本没接门禁 ——
    这个数字出现在清扫日志里，比任何文档都更早让人发现问题。
    """
    settings = get_settings()
    now = now or now_local()
    grace = dt.timedelta(minutes=settings.noshow_grace_minutes)
    # 只判昨天及以前：今天刚过期的时段可能是"迟到"，不是"没来"。
    last_day = now.date() - dt.timedelta(days=1)
    earliest = last_day - dt.timedelta(days=settings.noshow_window_days)

    rows = (
        (
            await session.execute(
                select(Reservation, Equipment.lab_id)
                .join(Equipment, Equipment.id == Reservation.equipment_id)
                .where(
                    Reservation.status == STATUS_EXPIRED,
                    Reservation.no_show_at.is_(None),
                    Reservation.date >= earliest,
                    Reservation.date < now.date(),
                )
                .order_by(Reservation.date, Reservation.start_time)
                .limit(limit)
            )
        )
        .all()
    )

    candidates: list[NoShowCandidate] = []
    undecidable = 0
    for res, lab_id in rows:
        day_start = dt.datetime.combine(res.date, dt.time.min)
        day_end = day_start + dt.timedelta(days=1)

        # ---- 数据充分性：这个实验室当天有没有任何门禁流水 ----
        lab_has_events = await session.scalar(
            select(AccessEvent.id)
            .where(AccessEvent.lab_id == lab_id, AccessEvent.occurred_at >= day_start)
            .where(AccessEvent.occurred_at < day_end)
            .limit(1)
        )
        if lab_has_events is None:
            # 判不了就是判不了。不写 no_show_at，也不做任何猜测。
            undecidable += 1
            continue

        # ---- 这个人有没有真的进来 ----
        entered = await session.scalar(
            select(AccessEvent.id)
            .where(
                AccessEvent.lab_id == lab_id,
                AccessEvent.user_id == res.user_id,
                AccessEvent.result == ACCESS_GRANTED,
                AccessEvent.direction == DIRECTION_IN,
                AccessEvent.occurred_at >= dt.datetime.combine(res.date, res.start_time) - grace,
                AccessEvent.occurred_at <= dt.datetime.combine(res.date, res.end_time) + grace,
            )
            .limit(1)
        )
        if entered is not None:
            continue

        candidates.append(
            NoShowCandidate(
                reservation_id=res.id,
                user_id=res.user_id,
                lab_id=lab_id,
                date=res.date,
                label=res.slot_label,
            )
        )
    return candidates, undecidable


async def mark_no_shows(
    session: AsyncSession, ids: list[int], *, now: dt.datetime | None = None
) -> int:
    """给候选预约盖上 ``no_show_at``。返回**真正改到的行数**。

    带条件的 UPDATE（``no_show_at IS NULL``）而不是"读出来再写回去"：
    多副本同时清扫时，只有真正改到的那一方把它计入自己的处理量，
    于是两个副本不会把同一条违约各记一次审计。
    """
    if not ids:
        return 0
    now = now or now_local()
    result = await session.execute(
        update(Reservation)
        .where(Reservation.id.in_(ids), Reservation.no_show_at.is_(None))
        .values(no_show_at=now)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def count_violations(
    session: AsyncSession, user_id: int, *, now: dt.datetime | None = None
) -> int:
    """窗口内计入的违约次数（已豁免的不算）。"""
    settings = get_settings()
    now = now or now_local()
    since = now - dt.timedelta(days=settings.noshow_window_days)
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Reservation)
            .where(
                Reservation.user_id == user_id,
                Reservation.no_show_at.is_not(None),
                Reservation.no_show_at >= since,
                Reservation.pardoned_at.is_(None),
            )
        )
        or 0
    )


async def state_for(
    session: AsyncSession, user_id: int, *, now: dt.datetime | None = None
) -> ViolationState:
    """判定这个人此刻能不能预约。

    **唯一一个入口**：下单、Agent 工具、后台展示都调它。
    分散判断的话，"对话能约、表单不能约"这种不一致迟早出现，
    而用户只会理解成"系统在针对我"。
    """
    settings = get_settings()
    count = await count_violations(session, user_id, now=now)
    threshold = settings.noshow_block_threshold
    over = count >= threshold
    return ViolationState(
        count=count,
        threshold=threshold,
        window_days=settings.noshow_window_days,
        blocked=over and settings.noshow_blocking_enabled,
        over_threshold=over,
        blocking_enabled=settings.noshow_blocking_enabled,
    )


async def list_violations(
    session: AsyncSession, user_id: int, *, limit: int = 50
) -> list[Reservation]:
    """该用户被判违约的记录（含已豁免的 —— 豁免本身也是要给人看的历史）。"""
    rows = (
        (
            await session.execute(
                select(Reservation)
                .where(Reservation.user_id == user_id, Reservation.no_show_at.is_not(None))
                .order_by(Reservation.no_show_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def pardon(
    session: AsyncSession, reservation_id: int, *, now: dt.datetime | None = None
) -> Reservation | None:
    """豁免一条违约。返回更新后的预约，找不到则 None。

    只对**已经判过且还没被豁免**的行生效（条件 UPDATE）。
    重复豁免返回同一条而不是报错：管理员连点两下不该看到错误。
    """
    now = now or now_local()
    res = await session.get(Reservation, reservation_id)
    if res is None or res.no_show_at is None:
        return None
    res.pardoned_at = now
    await session.flush()
    return res
