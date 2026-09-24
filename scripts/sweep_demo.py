"""把「后台清扫」解决的三件事在真实库上跑一遍。

    python scripts/sweep_demo.py

为什么要有这个脚本
------------------
``tests/test_sweep.py`` 已经覆盖了每个分支，但它跑在 pytest 里 ——
读代码的人看不到「清扫前 / 清扫后」这件事的**形状**。而 P1-2 要论证的恰恰是
一个时间维度上的说法：

    有些状态不是被谁改坏的，是**放久了自己变坏的**。

所以这里把三件事按「不管它 → 出什么问题 → 清扫后 → 问题消失」的顺序演一遍，
每一步都把可核对的数量打出来。**跑在临时库上**，不碰开发库
（理由与 ``overlap_race.py`` 相同：自己会写数据的验证脚本必须用专属的库，
否则第一次跑通、第二次结论就变了）。

三件事
------
1. 忘刷出场 —— 人被 ``uq_permit_one_inside`` 锁死，另一个房间也进不去；
2. 过期预约 —— 状态还挂在 ``confirmed``，占用格不放，同一时段约不回来；
3. 审计与流水 —— 只追加不清理，迟早是库里最大的两张表。

对应文档：docs/ENTERPRISE-UPGRADE.md 的 P1-2。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import func, select  # noqa: E402

from lagent.clock import now_local  # noqa: E402
from lagent.db import init_db, isolated_database, session_scope  # noqa: E402
from lagent.domain.access import (  # noqa: E402
    issue_permit,
    verify_entry,
)
from lagent.domain.booking import attach_slots  # noqa: E402
from lagent.models import (  # noqa: E402
    AuditLog,
    EntryPermit,
    LabOccupancy,
    Reservation,
    ReservationSlot,
)
from lagent.seed import seed  # noqa: E402
from lagent.sweep import (  # noqa: E402
    count_pending,
    sweep_archive_events,
    sweep_expired_reservations,
    sweep_stale_permits,
)

LINA = 2  # 李娜：资质齐全，能进所有房间
LAB_SPEC = 1  # 分析楼 301，工作日 08:00-22:00
LAB_CELL = 2  # 生物楼 205，工作日 08:00-20:00

_checks: list[tuple[str, bool]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    _checks.append((label, ok))
    mark = "✓" if ok else "✗"
    print(f"    {mark} {label}{('：' + detail) if detail else ''}")


def banner(title: str) -> None:
    print("\n" + "-" * 74)
    print(title)
    print("-" * 74)


async def _count(session, stmt) -> int:
    """数一下。``select(func.count())`` 的类型是 ``int | None``，在这里收口一次。

    不这么做就要在每个比较处写 ``int(x or 0)``，四处之后没人记得为什么 ——
    于是某一次比较会写错成 ``None > 0``，而那个表达式恰恰**不报错**、直接抛 TypeError。
    """
    return int(await session.scalar(stmt) or 0)


def _next_weekday(after: dt.date) -> dt.date:
    day = after + dt.timedelta(days=1)
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    return day


async def _enter(user_id: int, lab_id: int, when: dt.datetime) -> tuple[int, str, str]:
    """签发 + 入场。返回 (permit_id, 明文, 拒绝原因码)。入场成功时原因码为空。"""
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
    return permit_id, plain, decision.reason_code


async def scenario_forgot_checkout(day: dt.date) -> None:
    """① 忘刷出场：人被锁死，而且**不是**锁在一个房间里。"""
    banner("① 忘刷出场 —— 只发生一次，却会一直占着")
    permit_id, _, reason = await _enter(LINA, LAB_SPEC, dt.datetime.combine(day, dt.time(14, 0)))
    check("李娜 14:00 进入分析楼 301", not reason, reason or "放行")

    _, _, reason = await _enter(LINA, LAB_CELL, dt.datetime.combine(day, dt.time(16, 0)))
    check(
        "同一人去生物楼 205 被拒（她「还在」分析楼）",
        reason == "already_inside",
        reason,
    )

    async with session_scope() as session:
        inside = await _count(
            session,
            select(func.count()).select_from(EntryPermit).where(
                EntryPermit.status == "checked_in"
            )
        )
        seats = await _count(session, select(func.count()).select_from(LabOccupancy))
    check("在馆 1 人 —— 其实人早走了", inside == 1, f"{inside} 人")
    check("座位仍被占着", seats > 0, f"{seats} 格")

    # 关门（22:00）+ 宽限（30 分钟）之后跑一轮。
    # 返回的条数是「过期凭证 + 关门收尾」两类之和 —— 这里只关心后者，
    # 所以断言落在**这张凭证的状态**上，而不是那个混合计数。
    _, detail = await sweep_stale_permits(now=dt.datetime.combine(day, dt.time(22, 45)))
    async with session_scope() as session:
        swept = await session.get(EntryPermit, permit_id)
    check("关门后自动收尾", swept is not None and swept.status == "used", detail)
    check(
        "标成「系统收尾」而不是他刷了卡",
        swept is not None and swept.gate_out == "@auto",
        swept.gate_out if swept is not None else "",
    )

    async with session_scope() as session:
        inside = await _count(
            session,
            select(func.count()).select_from(EntryPermit).where(
                EntryPermit.status == "checked_in"
            )
        )
        seats = await _count(session, select(func.count()).select_from(LabOccupancy))
    check("在馆归零", inside == 0, f"{inside} 人")
    check("座位真的放开了", seats == 0, f"{seats} 格")

    # 第二天她必须真能进门 —— 这才是"解锁"的证据，前面几条只是字段变了
    next_day = _next_weekday(day)
    _, _, reason = await _enter(LINA, LAB_CELL, dt.datetime.combine(next_day, dt.time(10, 0)))
    check("第二天她能正常进生物楼 205", not reason, reason or "放行")


async def scenario_expired_reservation(day: dt.date) -> None:
    """② 过期预约：只改状态不删占用格，同一时段就永远约不回来。"""
    banner("② 过期预约 —— 「已过期」却还占着时段")
    past = day - dt.timedelta(days=3)
    async with session_scope() as session:
        res = Reservation(
            user_id=LINA,
            equipment_id=1,
            date=past,
            start_time=dt.time(9, 0),
            end_time=dt.time(11, 0),
            status="confirmed",
        )
        session.add(res)
        await session.flush()
        await attach_slots(session, res)
        res_id = res.id

    async with session_scope() as session:
        slots = await _count(
            session,
            select(func.count())
            .select_from(ReservationSlot)
            .where(ReservationSlot.reservation_id == res_id),
        )
    check("往日预约仍挂着 confirmed 并占格", slots > 0, f"{slots} 格")

    changed, detail = await sweep_expired_reservations(now=dt.datetime.combine(day, dt.time(12, 0)))
    check("清扫把它置为 expired", changed >= 1, detail)

    async with session_scope() as session:
        row = await session.get(Reservation, res_id)
        left = await _count(
            session,
            select(func.count())
            .select_from(ReservationSlot)
            .where(ReservationSlot.reservation_id == res_id),
        )
    check("状态变了", row is not None and row.status == "expired")
    check("占用格一并释放", left == 0, f"剩 {left} 格")


async def scenario_archive(day: dt.date, archive_dir: pathlib.Path) -> None:
    """③ 归档：先落盘、后删除。失败时一行都不能删。"""
    banner("③ 审计与流水 —— 只追加不清理")
    os.environ["LAB_ARCHIVE_DIR"] = str(archive_dir)
    os.environ["LAB_AUDIT_RETENTION_DAYS"] = "7"
    os.environ["LAB_ACCESS_EVENT_RETENTION_DAYS"] = "7"
    from lagent.config import reset_settings_cache

    reset_settings_cache()

    now = dt.datetime.combine(day, dt.time(23, 30))
    async with session_scope() as session:
        session.add(AuditLog(action="auth.login", actor_name="张伟", created_at=now - dt.timedelta(days=30)))
        session.add(AuditLog(action="auth.login", actor_name="李娜", created_at=now))
    check("库里有 1 条超期、1 条未超期", True, "30 天前 / 刚刚")

    archived, detail = await sweep_archive_events(now=now)
    files = sorted(archive_dir.glob("audit-*.jsonl"))
    check("导出了 1 条", archived == 1, detail)
    check("落成一个 JSONL 文件", len(files) == 1, files[0].name if files else "无")
    if files:
        lines = files[0].read_text(encoding="utf-8").splitlines()
        check("文件内容完整", len(lines) == 1 and "张伟" in lines[0])

    async with session_scope() as session:
        names = (
            (
                await session.execute(
                    select(AuditLog.actor_name).where(AuditLog.action == "auth.login")
                )
            )
            .scalars()
            .all()
        )
    check("库里只剩没超期的那条", list(names) == ["李娜"], f"{list(names)}")


async def main() -> int:
    root = pathlib.Path(tempfile.mkdtemp(prefix="lagent-sweep-"))
    url = f"sqlite+aiosqlite:///{(root / 'sweep.db').as_posix()}"
    day = _next_weekday(now_local().date())

    print("=" * 74)
    print("后台清扫（P1-2）：把「没人管就会一直占着」的状态收回来")
    print(f"库：{url}")
    print(f"演练日：{day}（工作日，分析楼 08:00-22:00）")
    print("=" * 74)

    async with isolated_database(url):
        await init_db()
        await seed(force=True)
        await scenario_forgot_checkout(day)
        await scenario_expired_reservation(day)
        await scenario_archive(day, root / "archive")

        # 只读诊断：跑完之后还剩多少积压。
        # 注意「在馆」这里不是 0 —— 那是最后一步为了证明"解锁了"而做的**真实入场**，
        # 不是清扫没做干净。
        pending = await count_pending()

    banner("跑完之后，只读诊断看到什么（count_pending）")
    for key, value in pending.items():
        print(f"    {key:<26} {value}")

    passed = sum(1 for _, ok in _checks if ok)
    print("\n" + "=" * 74)
    print(f"结果：{passed}/{len(_checks)} 项通过")
    if passed != len(_checks):
        failed = [label for label, ok in _checks if not ok]
        print("未通过：" + "；".join(failed))
    print("=" * 74)
    return 0 if passed == len(_checks) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
