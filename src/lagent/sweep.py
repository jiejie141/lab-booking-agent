"""后台清扫：把「没人管就会一直占着」的状态收回来（P1-2）。

## 为什么必须有它，而不是继续写在「已知限制」里

三件事会随时间自然变坏，而**没有任何用户操作会去修它们**：

1. **忘刷出场的人会一直「在馆」。** ``uq_permit_one_inside`` 让他再也进不了任何房间，
   ``lab_occupancy`` 里的座位也一直占着。这不是理论问题 —— 门禁最常见的现场事故
   就是「忘了刷出场」。管理员可以手工撤销凭证救回来，但不能指望有人 7×24 盯着。
2. **过期的预约一直挂在 ``confirmed``**：占用格不清，「今天还剩几个时段」这类
   统计会把已经过去的时段算进去。
3. **审计表与通行流水只追加、不清理**：它们迟早成为库里最大的两张表，
   而翻审计时 99% 的时间只关心最近几天。

## 三条正确性说明（写清楚，免得被当成「靠任务保证正确」）

* **门禁的判定不依赖本模块。** 凭证过期了，即使清扫没跑，``verify_entry``
  照样会拒 —— 所以这个循环挂掉不会让门禁失去保护。把清理任务写成"安全依赖"
  是很常见的错误：任务一挂，门就开了。
* **但「不依赖」不等于「可以静默挂掉」。** 每个任务单独隔离异常、记进返回值，
  由调用方（CLI / 启动日志 / 之后的 ``/metrics``）打出来。
* **归档的顺序是先落盘、后删除**，落盘用「写 .tmp + rename」保证不会留下
  半个文件被当成完整归档；归档失败时**一行都不删** —— 宁可表继续涨，
  也不要出现「删了但没归档」。

## 并发安全：把判定交给数据库

清扫是**批量改写**，天然会撞上「两个副本同时跑」。所以每处状态切换都用
**带条件的 UPDATE + rowcount**，而不是「先读出来、判断、再写回去」：

    只有真正把 ``checked_in`` 改成 ``used`` 的那一个事务，才去写审计条目。

这与 P0-1 学到的是同一条教训（check-then-act 在并发下必然出错）。
顺带一条取舍：**不是每个动作都值得逐条审计**。「预约到期」是可由数据推导的状态
（到期日一过，它就是过期），逐条记只会把审计表灌满噪声；所以那种只记**一条汇总**。
而「把一个人从在馆名单里请出去」直接关系到一个具体的人，必须逐条可查。

## 一个容易写错的地方：别在事务里调 audit

``audit.record`` 自己开一个事务（刻意的，见 audit.py）。如果在
``session_scope()`` 里调它，SQLite 上会出现「外层拿着写锁、内层再要写锁」——
轻则等满 ``busy_timeout``（30 秒）后失败，重则看起来像随机卡死。
所以本模块一律**先在事务里收集事实、出了事务再写审计**。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, or_, select, update

from .audit import record as audit_record
from .clock import now_local
from .config import get_settings
from .db import session_scope
from .domain.access import expire_stale_permits, release_seats
from .domain.availability import open_window
from .metrics import record_sweep_task
from .models import (
    ACTION_SWEEP_ARCHIVED,
    ACTION_SWEEP_FORCE_CHECKOUT,
    ACTION_SWEEP_RESERVATION_EXPIRED,
    ACTIVE_STATUSES,
    OUTCOME_OK,
    PERMIT_CHECKED_IN,
    PERMIT_ISSUED,
    PERMIT_USED,
    STATUS_EXPIRED,
    AccessEvent,
    AuditLog,
    EntryPermit,
    Laboratory,
    Reservation,
    ReservationSlot,
)
from .notify import sweep_notifications

# 强制收尾时写进 gate_out 的标记。用 ``@`` 前缀是为了和真实的门禁编号区分开 ——
# 事后统计"从哪个门出去的"时，这些不该被当成一台门禁设备。
FORCED_GATE_OUT = "@auto"


@dataclass(frozen=True)
class SweepResult:
    """一个任务跑完的结果。

    ``error`` 与 ``processed`` 分开而不是合成一个状态码：
    「跑了但出错」与「跑了、一条没处理」是两件完全不同的事，
    前者要告警、后者是稳态的常态。
    """

    name: str
    processed: int = 0
    detail: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass(frozen=True)
class SweepTask:
    name: str
    run: Callable[[], Awaitable[tuple[int, str]]]


# ===========================================================================
# 任务 1：过期预约
# ===========================================================================
async def sweep_expired_reservations(*, now: dt.datetime | None = None) -> tuple[int, str]:
    """把已经过去的「有效」预约改成 ``expired``，并删掉它们的占用格。

    两种都算过期，必须都判（这是本文件最容易漏的一处）：

      * ``date < 今天`` —— 往日遗留；
      * ``date == 今天 且 end_time <= 现在`` —— 今天已经用完的。

    只判前者的话，今天上午的预约会一直挂到明天才被清掉，
    而「今天还剩几个时段」这类统计恰恰最关心当天。

    刻意**不**碰 ``cancelled`` / ``completed``：它们已经不占资源，
    再改一次只会把用户自己操作留下的痕迹抹掉。
    """
    now = now or now_local()
    stale_condition = or_(
        Reservation.date < now.date(),
        (Reservation.date == now.date()) & (Reservation.end_time <= now.time()),
    )
    async with session_scope() as session:
        ids = list(
            (
                await session.execute(
                    select(Reservation.id).where(
                        Reservation.status.in_(ACTIVE_STATUSES), stale_condition
                    )
                )
            )
            .scalars()
            .all()
        )
        if not ids:
            return 0, ""
        # 带条件的 UPDATE：并发下只有真正改到行的那一方算「我处理的」，
        # 于是 CLI 报出来的数字不会因为多副本而翻倍。
        result = await session.execute(
            update(Reservation)
            .where(Reservation.id.in_(ids), Reservation.status.in_(ACTIVE_STATUSES))
            .values(status=STATUS_EXPIRED)
        )
        changed = int(result.rowcount or 0)  # type: ignore[attr-defined]
        # 占用格一并删掉。已过期 / 已取消的预约不该继续占着时段 ——
        # 即使某一行被别人抢先改成了 expired，它的格子照样该清。
        await session.execute(
            delete(ReservationSlot).where(ReservationSlot.reservation_id.in_(ids))
        )

    if changed:
        # 一条汇总，而不是逐条 —— 理由见模块 docstring 的取舍说明。
        await audit_record(
            action=ACTION_SWEEP_RESERVATION_EXPIRED,
            target_type="reservations",
            target_id=changed,
            detail=f"{changed} 条到期未使用的预约置为 {STATUS_EXPIRED}，占用格已释放",
        )
    return changed, f"占用格已释放（候选 {len(ids)} 条）"


# ===========================================================================
# 任务 2：凭证超时
# ===========================================================================
def _force_checkout_needed(lab: Laboratory, permit: EntryPermit, now: dt.datetime) -> bool:
    """关门了（含宽限）就认为人已经走了。纯函数，便于单测。

    **为什么用「实验室的关门时间」而不是凭证的 valid_to**：
    ``valid_to`` 是*入场*时间窗的上限，不是离场时间 —— 一个人可以合法地
    在 valid_to 之前进去、之后还在里面干活。用它当收尾依据会把正常做实验的人
    踢出「在馆」名单（而座位上确实还该占着）。「实验室关门了」则是一条谁都认的界线。
    """
    if permit.date < now.date():
        return True  # 往日遗留：无论如何都不该还算在馆
    window = open_window(lab, permit.date)
    if window is None:
        return True  # 当天不开放，里面不该有人
    _, close = window
    grace = dt.timedelta(minutes=get_settings().permit_checkout_grace_minutes)
    return now >= dt.datetime.combine(permit.date, close) + grace


async def sweep_stale_permits(*, now: dt.datetime | None = None) -> tuple[int, str]:
    """两件事：过期的 ``issued`` 凭证标记为 ``expired``；超时未出场的强制收尾。

    强制收尾做四件事，一件都不能省：

      1. 状态 → ``used``（凭证从此不可再用）；
      2. 写 ``checked_out_at``（在馆时长统计要它）；
      3. **释放座位**（这才是「假占用」被解开的实质）；
      4. 写审计 —— 否则「这个人是谁、什么时候被系统请出在馆名单的」查不到。

    第 4 条走的是**审计表**而不是 ``access_events``：``access_events`` 的定义是
    「谁什么时候站在哪个门口」，而这里根本没有门禁动作。往里塞一条假出场，
    会让「门禁流水」这条证据链的信噪比变差 —— 一条查不到对应刷卡的记录，
    比没有记录更误导人。
    """
    now = now or now_local()
    async with session_scope() as session:
        expired = await expire_stale_permits(session, now=now)

    forced: list[tuple[int, int, int]] = []  # (permit_id, user_id, lab_id)
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(EntryPermit, Laboratory)
                .join(Laboratory, Laboratory.id == EntryPermit.lab_id)
                .where(EntryPermit.status == PERMIT_CHECKED_IN)
            )
        ).all()
        for permit, lab in rows:
            if not _force_checkout_needed(lab, permit, now):
                continue
            result = await session.execute(
                update(EntryPermit)
                .where(
                    EntryPermit.id == permit.id,
                    # ★ 关键：把「还没被别人收尾」这个前提写进 WHERE。
                    # 两个副本同时跑时，只有真正改到行的那个事务会往下走，
                    # 于是审计条目不会重复 —— 与 P0-1 同一条教训。
                    EntryPermit.status == PERMIT_CHECKED_IN,
                )
                .values(
                    status=PERMIT_USED,
                    checked_out_at=now,
                    gate_out=permit.gate_out or FORCED_GATE_OUT,
                )
            )
            if int(result.rowcount or 0) == 0:  # type: ignore[attr-defined]
                continue
            await release_seats(session, permit.id)
            forced.append((permit.id, permit.user_id, permit.lab_id))

    # 出了事务再写审计（见模块 docstring 最后一段）
    for permit_id, user_id, lab_id in forced:
        await audit_record(
            action=ACTION_SWEEP_FORCE_CHECKOUT,
            actor_id=None,
            actor_name="system",
            target_type="permit",
            target_id=permit_id,
            detail=(
                f"user={user_id} lab={lab_id} 未刷出场，"
                f"关门后自动收尾（{now.isoformat(timespec='seconds')}）"
            ),
        )

    return expired + len(forced), f"标记过期 {expired} / 关门收尾 {len(forced)}"


# ===========================================================================
# 任务 3：审计与通行流水的归档
# ===========================================================================
def _row_to_dict(row: Any) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _write_jsonl(target: Path, rows: Sequence[dict[str, Any]]) -> None:
    """原子写：先写 ``.tmp``，fsync 后再 rename。

    直接往目标文件写的话，进程在中途被杀就会留下一个「看起来完整、其实缺一半」
    的归档 —— 而我们的下一步是**删掉源数据**。这个组合会真的丢数据。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_name(target.name + ".tmp")
    with scratch.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    scratch.replace(target)


def _free_archive_path(directory: Path, stem: str, now: dt.datetime) -> Path:
    """给本次导出一个不重名的路径。

    刻意**不用**「一天一个文件」的命名：同一天跑第二批时会把第一批覆盖掉，
    而那批源数据已经删了 —— 归档就真的丢了。所以文件名带运行时刻，
    再撞上就往后加序号。
    """
    stamp = now.strftime("%Y%m%dT%H%M%S")
    candidate = directory / f"{stem}-{stamp}.jsonl"
    index = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{stamp}-{index}.jsonl"
        index += 1
    return candidate


async def _archive(
    model: Any,
    time_column: Any,
    stem: str,
    retention_days: int,
    *,
    now: dt.datetime,
) -> tuple[int, str]:
    """导出并删除一批。返回 ``(条数, 说明)``。

    分批是为了内存：一次导出几百万行会把进程吃光，而这条路径在日常运维里
    恰恰是「第一次跑、表最大」的那次。
    """
    cutoff = now - dt.timedelta(days=max(retention_days, 0))
    directory = Path(get_settings().archive_dir)
    batch = max(int(get_settings().archive_batch_size), 1)
    total = 0
    while True:
        async with session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(model)
                        .where(time_column < cutoff)
                        .order_by(time_column)
                        .limit(batch)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                break
            payload = [_row_to_dict(row) for row in rows]
            # 先落盘。这一步抛异常就直接向上冒 —— 下面的 delete 不会执行。
            # 纯文件操作，不碰数据库，所以不会和外层事务抢锁。
            _write_jsonl(_free_archive_path(directory, stem, now), payload)
            await session.execute(delete(model).where(model.id.in_([row.id for row in rows])))
        total += len(payload)
        if len(payload) < batch:
            break
    return total, f"保留 {retention_days} 天 → {directory}"


async def sweep_archive_events(*, now: dt.datetime | None = None) -> tuple[int, str]:
    """把超过保留期的审计与通行流水导出成 JSONL，再从库里删除。"""
    now = now or now_local()
    settings = get_settings()
    audit_count, audit_detail = await _archive(
        AuditLog, AuditLog.created_at, "audit", settings.audit_retention_days, now=now
    )
    access_count, access_detail = await _archive(
        AccessEvent,
        AccessEvent.occurred_at,
        "access-events",
        settings.access_event_retention_days,
        now=now,
    )
    if audit_count:
        await audit_record(
            action=ACTION_SWEEP_ARCHIVED,
            outcome=OUTCOME_OK,
            target_type="audit_logs",
            target_id=audit_count,
            detail=f"{audit_detail}；access_events {access_count} 条（{access_detail}）",
        )
    return audit_count + access_count, f"审计 {audit_count} / 流水 {access_count}"


# ===========================================================================
# 任务组装与运行器
# ===========================================================================
DEFAULT_TASKS: tuple[SweepTask, ...] = (
    SweepTask("过期预约收尾", sweep_expired_reservations),
    SweepTask("凭证超时与关门收尾", sweep_stale_permits),
    SweepTask("审计与流水归档", sweep_archive_events),
    # 通知投递（P1-6）放在清扫里，而不是要求运维自己配 cron：
    # 配了 SMTP 就自动发，没配就如实报告"跳过 N 条"，两种状态都看得见。
    SweepTask("通知投递", sweep_notifications),
)


async def run_once(*, tasks: Sequence[SweepTask] = DEFAULT_TASKS) -> list[SweepResult]:
    """跑一轮全部任务。

    **逐个隔离异常**：一个任务失败不影响后面的。否则「归档目录没权限」这种
    局部问题会把「凭证收尾」也一起拖停 —— 而后者才是真正要紧的那个。

    每个任务的耗时与结果在这里**统一**记进指标（P1-4）：这是 CLI 与后台循环
    两条路径的**唯一**汇合点，记在这里就两条都覆盖，不必各自埋一遍。
    """
    results: list[SweepResult] = []
    for task in tasks:
        started = time.perf_counter()
        try:
            processed, detail = await task.run()
        except asyncio.CancelledError:
            # 被取消不是"失败"：进程正在退出，这一轮没跑完而已。
            # 记成 error 会在每次正常停机时留下一条假的失败记录。
            raise
        except Exception as exc:  # noqa: BLE001 - 任务是可插拔的，不能假定异常类型
            results.append(SweepResult(task.name, error=f"{type(exc).__name__}: {exc}"))
            record_sweep_task(
                task.name, ok=False, duration_seconds=time.perf_counter() - started
            )
        else:
            results.append(SweepResult(task.name, processed, detail))
            record_sweep_task(
                task.name,
                ok=True,
                duration_seconds=time.perf_counter() - started,
                processed=processed,
            )
    return results


@dataclass
class SweepRunner:
    """周期性跑清扫的后台循环。

    只在**服务进程内**跑（见 ``api.py`` 的 lifespan）。多副本部署时每个副本
    都会跑一遍：所有任务都写成幂等的、状态切换用条件 UPDATE，
    所以多跑不会算错，只多花一点 CPU。把它挪出进程是 P2 的事
    （见 ``docs/ENTERPRISE-UPGRADE.md``）。

    ``runs`` / ``total_processed`` / ``failures`` 是**本进程内**的累计量，
    供 CLI / doctor 显示。对外的指标在 ``metrics.py`` 里（`lagent_sweep_*`），
    P1-4 之后那边才是运维真正看的东西 —— 这里的字段只图本地看一眼方便。
    """

    interval_seconds: int = 300
    tasks: Sequence[SweepTask] = DEFAULT_TASKS
    runs: int = 0
    total_processed: int = 0
    failures: int = 0
    last_results: list[SweepResult] = field(default_factory=list)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    async def run_once(self) -> list[SweepResult]:
        self.last_results = await run_once(tasks=self.tasks)
        self.runs += 1
        self.total_processed += sum(r.processed for r in self.last_results)
        self.failures += sum(1 for r in self.last_results if not r.ok)
        return self.last_results

    def report(self) -> None:
        """只在「有事情发生」时说话。

        清扫大多轮次都是空跑（稳态），每 5 分钟打一行「本次处理 0 条」会训练人
        忽略这个日志 —— 那么它真正出错时也不会有人看见。
        """
        for result in self.last_results:
            if result.error:
                print(f"[sweep] ✗ {result.name}：{result.error}")
            elif result.processed:
                print(f"[sweep] {result.name}：{result.processed} 条（{result.detail}）")

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
                self.report()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 循环绝不能因为一轮失败就退出
                print(f"[sweep] ✗ 本轮清扫整体失败：{type(exc).__name__}: {exc}")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="lagent-sweep")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def stop(self, *, timeout: float = 5.0) -> None:
        """停掉循环。**必须能真的停下来** —— 否则测试与进程退出会挂住。"""
        self._stop.set()
        if self._task is None:
            return
        task, self._task = self._task, None
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def build_runner() -> SweepRunner:
    settings = get_settings()
    return SweepRunner(interval_seconds=settings.sweep_interval_seconds)


async def count_pending() -> dict[str, int]:
    """当前有多少「该被清扫」的东西。给 doctor / 运维看一眼。

    刻意做成**只读**：一个自称"诊断"的命令顺手改数据的话，出问题时就没法用它取证了。
    """
    now = now_local()
    settings = get_settings()
    async with session_scope() as session:
        active = await session.scalar(
            select(func.count()).select_from(Reservation).where(
                Reservation.status.in_(ACTIVE_STATUSES)
            )
        )
        inside = await session.scalar(
            select(func.count()).select_from(EntryPermit).where(
                EntryPermit.status == PERMIT_CHECKED_IN
            )
        )
        stale = await session.scalar(
            select(func.count()).select_from(EntryPermit).where(
                EntryPermit.status == PERMIT_ISSUED,
                or_(
                    EntryPermit.date < now.date(),
                    (EntryPermit.date == now.date()) & (EntryPermit.valid_to < now.time()),
                ),
            )
        )
        archivable_audit = await session.scalar(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.created_at < now - dt.timedelta(days=settings.audit_retention_days)
            )
        )
        archivable_access = await session.scalar(
            select(func.count()).select_from(AccessEvent).where(
                AccessEvent.occurred_at
                < now - dt.timedelta(days=settings.access_event_retention_days)
            )
        )
    return {
        "active_reservations": int(active or 0),
        "inside_people": int(inside or 0),
        "stale_permits": int(stale or 0),
        "archivable_audit_rows": int(archivable_audit or 0),
        "archivable_access_events": int(archivable_access or 0),
    }


__all__ = [
    "DEFAULT_TASKS",
    "FORCED_GATE_OUT",
    "SweepResult",
    "SweepRunner",
    "SweepTask",
    "build_runner",
    "count_pending",
    "run_once",
    "sweep_archive_events",
    "sweep_expired_reservations",
    "sweep_stale_permits",
]
