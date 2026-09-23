"""复现「重叠但开始时间不同」的重复预约（超卖）。

    python scripts/overlap_race.py

背景
----
`models.Reservation` 的部分唯一索引建在

    (equipment_id, date, start_time) WHERE status IN ('pending','confirmed')

上，只能拦住**开始时间完全相同**的重复写入。
14:00-16:00 与 15:00-17:00 这类「部分重叠、开始时间不同」的请求索引看不见，
只能靠 `acquire_equipment_lock` + 锁内复检 —— 而在 SQLite 上
`acquire_equipment_lock` 直接返回 "sqlite_write_lock" 不做任何事（没有行级锁），
于是锁内复检退化成了典型的 check-then-act：两个事务各自读到「无冲突」，
再用不同的 start_time 写入，唯一索引不拦，两条都落库。

为什么 `main.py loadtest` 没发现
-------------------------------
压测里 40 个并发请求用的是**同一个开始时间**，走的正是唯一索引能覆盖的路径。
所以「并发安全」这个结论此前只在同开始时间这一种情形下成立。

期望
----
三种情形都应「恰好 1 个成功」。当前后两种会各自成功 2 个。

对应文档：docs/ENTERPRISE-UPGRADE.md 的 P0-1。
修好之后，本脚本应当被改写成一条断言「恰好 1 成功」的测试。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import select  # noqa: E402

from lagent.agent.tools import tool_create_reservation  # noqa: E402
from lagent.clock import now_local  # noqa: E402
from lagent.db import init_db, isolated_database, session_scope  # noqa: E402
from lagent.models import ACTIVE_STATUSES, Reservation  # noqa: E402
from lagent.seed import seed  # noqa: E402

CONCURRENCY = 20


async def run_case(label: str, slots: list[tuple[dt.time, dt.time]], day_offset: int) -> bool:
    """每例用不同日期，避免上一例的遗留记录干扰判定。返回是否通过。"""
    day = now_local().date() + dt.timedelta(days=day_offset)
    attempts = [
        tool_create_reservation(
            user_id=2,
            equipment_id=1,
            date_=day,
            start=slots[i % len(slots)][0],
            end=slots[i % len(slots)][1],
            purpose=f"{label}-{i}",
        )
        for i in range(CONCURRENCY)
    ]
    results = await asyncio.gather(*attempts, return_exceptions=True)
    exceptions = [r for r in results if isinstance(r, BaseException)]
    succeeded = [r for r in results if not isinstance(r, BaseException) and r.ok]

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Reservation).where(
                    Reservation.equipment_id == 1,
                    Reservation.date == day,
                    Reservation.status.in_(ACTIVE_STATUSES),
                )
            )
        ).scalars().all()
        kept = sorted((r.start_time, r.end_time) for r in rows)

    requested = ", ".join(f"{a.strftime('%H:%M')}-{b.strftime('%H:%M')}" for a, b in slots)
    print(f"\n【{label}】{CONCURRENCY} 并发 · 请求 {requested}")
    print(f"  成功 {len(succeeded)} · 异常 {len(exceptions)}")
    if exceptions:
        kinds: dict[str, int] = {}
        for exc in exceptions:
            kinds[type(exc).__name__] = kinds.get(type(exc).__name__, 0) + 1
        print(f"  异常类型 {kinds}")
    print(f"  落库 {len(kept)} 条："
          + "、".join(f"{a.strftime('%H:%M')}-{b.strftime('%H:%M')}" for a, b in kept))

    passed = len(kept) == 1
    print("  ✓ 恰好 1 条，未超卖" if passed else "  ✗ 出现互相重叠的有效预约（超卖）")
    return passed


async def main() -> int:
    root = pathlib.Path(tempfile.mkdtemp(prefix="lagent-race-"))
    url = f"sqlite+aiosqlite:///{(root / 'race.db').as_posix()}"

    print("=" * 74)
    print("重叠预约并发实验：唯一索引只覆盖 (设备, 日期, 开始时间)")
    print("=" * 74)

    async with isolated_database(url):
        await init_db()
        await seed(force=True)
        results = [
            await run_case("同开始时间（对照组）", [(dt.time(9, 0), dt.time(11, 0))], 21),
            await run_case("部分重叠·不同开始", [(dt.time(13, 0), dt.time(15, 0)),
                                               (dt.time(14, 0), dt.time(16, 0))], 22),
            await run_case("完全包含关系", [(dt.time(15, 0), dt.time(17, 0)),
                                            (dt.time(15, 30), dt.time(16, 0))], 23),
        ]

    print("\n" + "=" * 74)
    print(f"结果：{sum(results)}/{len(results)} 项通过")
    if not all(results):
        print("结论：非重叠不变式没有下沉到数据库层，重叠写入可被绕过。")
    print("=" * 74)
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
