"""命令行入口：doctor / seed / chat / eval / loadtest。

其中两个子命令是本项目的「证据」而非「功能」：

  * ``loadtest`` —— 起 N 个并发请求抢同一个时段，断言**只有一个成功**。
    这是「并发安全」这个说法唯一的实证来源，不靠嘴说。
  * ``eval``     —— 跑固定用例集，量出意图识别与槽位抽取的准确率。
    没有它，「模型能听懂中文」也只是感觉。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import json
import pathlib
import shutil
import sys
import tempfile

from collections.abc import AsyncIterator
from contextlib import suppress

from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from .agent.graph import build_agent_from_settings, set_catalog
from .agent.state import SessionStore
from .agent.tools import TOOL_SPECS, tool_create_reservation
from .clock import now_local
from .config import get_settings
from .db import dispose_engine, init_db, isolated_database, session_scope
from .domain.availability import open_window
from .domain.booking import list_reservations
from .knowledge.retriever import build_retriever, fallback_reason, load_corpus
from .models import Equipment, Laboratory, Reservation, User
from .schemas import ChatRequest
from .seed import seed


# ==========================================================================
# doctor
# ==========================================================================
async def _doctor() -> int:
    settings = get_settings()
    print("=" * 66)
    print("lab-booking-agent · 环境自检")
    print("=" * 66)
    print(f"  app_mode          : {settings.app_mode}")
    print(f"  timezone          : {settings.timezone}  (now={now_local().isoformat(timespec='seconds')})")
    print(f"  database_url      : {settings.database_url}")
    print(f"  retrieval_backend : {settings.retrieval_backend}")

    await init_db()
    info = await seed()
    print(f"  seed              : {'已灌入' if info['seeded'] else '跳过'} {info.get('reason', '')}")

    async with session_scope() as session:
        labs = (await session.execute(select(func.count()).select_from(Laboratory))).scalar()
    print(f"  数据库连通        : 是（实验室 {labs} 个）")

    # 检索
    try:
        retriever = build_retriever(settings.retrieval_backend)
        stats = retriever.stats()
        print(f"  检索器            : {stats}")
        reason = fallback_reason()
        if reason:
            print(f"  ⚠ 检索已降级      : {reason}（hybrid 需要 chromadb，缺省回退 BM25）")
        corpus = load_corpus()
        probe = retriever.search("离心机 配平", 1)
        print(f"  检索自测          : {len(corpus)} 条语料，命中「{probe[0][0].heading if probe else '无'}」")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 检索不可用      : {exc}")
        return 1

    # 模型
    agent = build_agent_from_settings(SessionStore())
    if agent.client is None:
        print(f"  ✗ 模型             : 不可用（app_mode={settings.app_mode}）→ 对话将走引导式表单降级")
    else:
        print(f"  ✓ 模型             : {agent.client.name}")
        resp = await agent.ainvoke(ChatRequest(message="明天下午两点想用荧光光谱仪两小时", user_id=2, session_id="doctor"))
        print(f"  对话自测          : intent={resp.intent} stage={resp.stage} 方案 {len(resp.proposals)} 个")
        print(f"     trace           : {' → '.join(t.node for t in resp.trace)}")

    print(f"  已注册工具        : {', '.join(t['name'] for t in TOOL_SPECS)}")
    print("=" * 66)
    print("自检通过" if agent.client else "自检完成（模型不可用，属降级运行）")
    return 0


# ==========================================================================
# chat
# ==========================================================================
async def _chat(message: str, user_id: int, session_id: str) -> int:
    await init_db()
    await seed()
    async with session_scope() as session:
        rows = (await session.execute(select(Equipment))).scalars().all()
    set_catalog([(r.name, r.category) for r in rows])

    agent = build_agent_from_settings(SessionStore())
    resp = await agent.ainvoke(ChatRequest(message=message, user_id=user_id, session_id=session_id))
    print(f"[intent={resp.intent} stage={resp.stage} degraded={resp.degraded}]")
    print(resp.reply)
    for i, item in enumerate(resp.proposals, 1):
        print(f"  {i}. {item.label()} · {item.hours:g}h · score={item.score} · {item.relaxations}")
    if resp.booking:
        print(f"  下单：{'成功' if resp.booking.ok else '失败'} · 重试 {resp.booking.retries} 次 · {resp.booking.message}")
    print("  trace: " + " → ".join(f"{t.node}({t.elapsed_ms}ms)" for t in resp.trace))
    return 0 if resp.booking is None or resp.booking.ok else 1


# ==========================================================================
# 一次性沙箱库：eval / loadtest 专用
# ==========================================================================
@contextlib.asynccontextmanager
async def _scratch_database(prefix: str) -> AsyncIterator[None]:
    """开一个临时 Sqlite 库，用完连文件一起删掉。

    和开发库彻底隔离，命令之间也互不影响 —— 否则「跑第二遍结果就变了」。
    """
    root = pathlib.Path(tempfile.mkdtemp(prefix=prefix))
    url = f"sqlite+aiosqlite:///{(root / 'scratch.db').as_posix()}"
    try:
        async with isolated_database(url):
            await init_db()
            await seed(force=True)
            yield
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ==========================================================================
# loadtest —— 并发安全实证
# ==========================================================================
async def _loadtest(concurrency: int, rounds: int) -> int:
    """N 个并发请求抢同一时段，期望恰好 1 个成功。

    这条断言如果失败，说明「并发安全」是假的：
      * 成功 > 1 → 唯一索引没生效，出现了同坑重复写入；
      * 成功 = 0 → 全被误判为冲突，正常请求被吞掉。

    时段分配必须由脚本自己保证「轮与轮之间不重叠、且落在开放时间内」。
    早期版本直接拿轮次算小时数，第 2 轮排到了第 1 轮的尾巴上（全被判冲突是
    **正确**行为）、第 3 轮排到了闭馆之后，于是把「本来就该冲突」误报成并发失败。
    压测脚本自己写错比没有压测更糟 —— 它给出的是一个看起来很有说服力的错误结论。

    整个压测跑在一次性沙箱库里：它会写入几十条记录，留在开发库里既是垃圾，
    也会让下次压测/评测面对一个已经被人占过的时段。
    """
    async with _scratch_database("lagent-loadtest-"):
        return await _loadtest_rounds(concurrency, rounds)


async def _loadtest_rounds(concurrency: int, rounds: int) -> int:
    async with session_scope() as session:
        users = (await session.execute(select(User).order_by(User.id))).scalars().all()
        equipment = (
            await session.execute(
                select(Equipment).options(selectinload(Equipment.lab)).order_by(Equipment.id)
            )
        ).scalars().all()

    # 挑一台不需要资质的设备：否则请求会在资质那一步就被拦掉，测的就不是并发了
    target = next((e for e in equipment if not e.requires_training), equipment[0])
    lab = target.lab
    # 用两周后的日期，远离种子数据与评测留下的预约，让压测自足
    day = now_local().date() + dt.timedelta(days=14)
    window = open_window(lab, day)
    if window is None:
        print(f"✗ {lab.label} 在 {day} 不开放，无法压测")
        return 1
    open_start, open_end = window
    open_minutes = (
        open_end.hour * 60 + open_end.minute
    ) - (open_start.hour * 60 + open_start.minute)
    if open_minutes < rounds * 60:
        print(f"✗ {lab.label} 当天仅开放 {open_minutes} 分钟，不足以安排 {rounds} 轮不重叠时段")
        return 1

    # 清掉该设备当天的历史记录，让每轮从一个确定的状态开始
    async with session_scope() as session:
        await session.execute(
            delete(Reservation).where(
                Reservation.equipment_id == target.id, Reservation.date == day
            )
        )

    print("=" * 66)
    print(f"并发压测：{rounds} 轮 × {concurrency} 并发，抢同一时段")
    print(f"  目标：设备#{target.id} {target.name}（{lab.label}）· {day}")
    print(f"  开放时间：{open_start.strftime('%H:%M')}-{open_end.strftime('%H:%M')}")
    print("=" * 66)

    verdict = 0
    for index in range(1, rounds + 1):
        # 从开放时刻起，每轮占一个小时，轮与轮严格不重叠
        base = open_start.hour * 60 + open_start.minute + (index - 1) * 60
        slot_start = dt.time(hour=base // 60, minute=base % 60)
        slot_end = dt.time(hour=(base + 60) // 60, minute=(base + 60) % 60)
        attempts = [
            tool_create_reservation(
                user_id=users[i % len(users)].id,
                equipment_id=target.id,
                date_=day,
                start=slot_start,
                end=slot_end,
                purpose=f"并发压测第 {index} 轮",
            )
            for i in range(concurrency)
        ]
        results = await asyncio.gather(*attempts, return_exceptions=True)

        failures = [r for r in results if isinstance(r, BaseException)]
        outcomes = [r for r in results if not isinstance(r, BaseException)]
        ok = [r for r in outcomes if r.ok]
        conflict = [r for r in outcomes if not r.ok and r.conflict_with is not None]
        other = [r for r in outcomes if not r.ok and r.conflict_with is None]
        max_retry = max((r.retries for r in outcomes), default=0)

        flag = "✓" if len(ok) == 1 and not failures and not other else "✗"
        print(
            f"  轮 {index} [{slot_start.strftime('%H:%M')}-{slot_end.strftime('%H:%M')}] "
            f"成功 {len(ok)} / 明确冲突 {len(conflict)} / 其他 {len(other)} "
            f"/ 异常 {len(failures)} · 最大重试 {max_retry}  {flag}"
        )
        if flag == "✗":
            verdict = 1
            for row in other[:3]:
                print(f"      · 其他失败：{row.message}")
            for exc in failures[:3]:
                print(f"      · 异常：{type(exc).__name__}: {exc}")

    async with session_scope() as session:
        rows = await list_reservations(session, date_=day)
    kept = [r for r in rows if r.equipment_id == target.id]
    print(f"\n  压测后该设备在 {day} 共 {len(kept)} 条预约记录（期望恰好 {rounds} 条，每轮一条）")
    print("=" * 66)
    if verdict == 0 and len(kept) == rounds:
        print("结论：并发写入被数据库唯一索引正确拦下，未出现同坑重复")
        return 0
    print("结论：并发安全未达预期，见上方明细")
    return 1


# ==========================================================================
# eval
# ==========================================================================
async def _eval(cases_path: str | None) -> int:
    from .evaluation import run_eval

    async with _scratch_database("lagent-eval-"):
        report = await run_eval(cases_path)
    print(report.render())
    return 0 if report.passed else 1


# ==========================================================================
# 入口
# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lab-booking-agent", description="智能实验室预约 Agent")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor", help="环境自检：数据库、检索、模型、对话链路")
    sub.add_parser("tools", help="列出已注册的工具")

    seed_parser = sub.add_parser("seed", help="灌入种子数据")
    seed_parser.add_argument("--force", action="store_true", help="清空并重建")

    chat_parser = sub.add_parser("chat", help="用一句话和 Agent 对话")
    chat_parser.add_argument("message")
    chat_parser.add_argument("--user", type=int, default=2)
    chat_parser.add_argument("--session", default="cli")

    load_parser = sub.add_parser(
        "loadtest", help="并发抢坑压测（一次性沙箱库，验证唯一索引兜底）"
    )
    load_parser.add_argument("--concurrency", type=int, default=40)
    load_parser.add_argument("--rounds", type=int, default=3)

    eval_parser = sub.add_parser(
        "eval", help="跑评测集，量出意图与槽位准确率（一次性沙箱库，结果可复现）"
    )
    eval_parser.add_argument("--cases", default=None)

    sub.add_parser("serve", help="启动 FastAPI 服务（等同 python main.py）")
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.command == "doctor":
        return await _doctor()
    if args.command == "tools":
        for spec in TOOL_SPECS:
            print(f"{spec['name']}: {spec['description']}")
            for key, value in spec["params"].items():
                print(f"    {key}: {value}")
        return 0
    if args.command == "seed":
        await init_db()
        info = await seed(force=args.force)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    if args.command == "chat":
        return await _chat(args.message, args.user, args.session)
    if args.command == "loadtest":
        return await _loadtest(args.concurrency, args.rounds)
    if args.command == "eval":
        return await _eval(args.cases)
    build_parser().print_help()
    return 0


def main(argv: list[str] | None = None) -> int:
    # 直接 `python -m lagent.cli` 或从别处调用时也要加固（main.py 那条路径已加固过，
    # 重复调用无害）
    from .console import enable_utf8_output

    enable_utf8_output()

    args = build_parser().parse_args(argv)

    # serve 必须在 asyncio.run **之外**处理：uvicorn.run 内部自己要起事件循环，
    # 若把它塞进 _run（已经被 asyncio.run 包着）就会直接抛
    # 「asyncio.run() cannot be called from a running event loop」——
    # 「python main.py」能起服务而「python main.py serve」起不来，就是这么来的。
    if args.command in (None, "serve"):
        from .server import serve

        serve()
        return 0

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    finally:
        with suppress(Exception):
            asyncio.run(dispose_engine())


if __name__ == "__main__":
    sys.exit(main())
