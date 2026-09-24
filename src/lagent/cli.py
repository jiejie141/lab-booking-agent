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
from .clock import now_local, window_covering
from .config import get_settings
from .db import (
    SchemaDriftError,
    dispose_engine,
    downgrade,
    init_db,
    isolated_database,
    migrate,
    revision_status,
    schema_drift,
    session_scope,
)
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

    # 结构校验放在 seed 之前。库里缺列时 seed 自己也会炸，但它的报错是
    # 「no such column ...」加一屏堆栈，看不出「该重建库」。自检命令的职责
    # 恰恰是把这种情况翻译成人能照着做的话 —— 而不是自己也崩掉
    # （它真崩过一次，所以这里是一条回归点）。
    current, head = await revision_status()
    drift = await schema_drift()
    if drift:
        print(f"  ✗ 数据库结构      : 与代码不一致（库 revision={current or '无'} / head={head}）")
        for item in drift:
            print(f"      · {item}")
        print("      修法 A：python main.py seed --force     重建演示库（会清空现有数据）")
        print("      修法 B：把 LAB_DATABASE_URL 指向新的 sqlite 文件")
        print("=" * 66)
        print("自检未通过：库结构过期。修好后请重新跑一次 python main.py doctor")
        return 1
    print(f"  迁移 revision     : {current} (head={head})")

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

    # 工具路由要说清是**谁在选工具**。之前这行只写「已注册工具：a, b, c」，
    # 而「注册了」与「由模型调用」是两件事 —— 那种措辞很容易被读成
    # 「实现了 function calling」。现在把执行模式一并打出来，不含糊。
    routing = (
        "条件边（确定性路由，模型不参与）"
        if settings.execution_mode == "deterministic"
        else "模型 function calling（harness/react）"
    )
    print(f"  执行模式          : {settings.execution_mode}")
    print(f"  工具路由          : {routing}")
    print(f"  已注册工具        : {', '.join(t['name'] for t in TOOL_SPECS)}")

    # 后台清扫是「不用人管的那部分」：这里只**看**当前积压，不做任何修改 ——
    # 一个自称诊断的命令顺手改数据的话，出问题时就没法用它取证了。
    from .sweep import count_pending

    pending = await count_pending()
    state = "已启用" if settings.sweep_enabled else "已关闭（LAB_SWEEP_ENABLED=false）"
    print(f"  后台清扫          : {state}，间隔 {settings.sweep_interval_seconds}s")
    print(
        "  待清扫积压        : "
        f"在馆 {pending['inside_people']} 人 · "
        f"过期凭证 {pending['stale_permits']} 张 · "
        f"可归档 {pending['archivable_audit_rows'] + pending['archivable_access_events']} 行"
    )
    print("=" * 66)
    print("自检通过" if agent.client else "自检完成（模型不可用，属降级运行）")
    return 0


# ==========================================================================
# chat
# ==========================================================================
async def _chat(message: str, user_id: int, session_id: str) -> int:
    # seed() 内部第一步就是建表 + 校验结构（ensure_schema）。
    # 库过期时会抛 SchemaDriftError，由 main() 统一翻译成可照做的提示，
    # 而不是等跑到「查 users」那一步才炸出一段 SQLAlchemy 堆栈。
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
            # seed(force=True) 内部会先重建表结构，这里不必再单独 init_db。
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
# access-demo —— 人员准入实证
# ==========================================================================
async def _access_demo_steps() -> int:
    from .domain.access import (
        EntryDecision,
        inside_count,
        issue_permit,
        verify_entry,
        verify_exit,
    )
    from .models import AccessEvent, Laboratory, slot_index_of

    now = now_local()
    day = now.date()
    win_start, win_end = window_covering(now)
    lab_cell, lab_spec = 2, 1  # 生物楼205(细胞培养) / 分析楼301(光谱+色谱)

    async with session_scope() as session:
        by_name = {
            u.username: u.id
            for u in (await session.execute(select(User))).scalars().all()
        }
        labs = {
            lab.id: f"{lab.building}{lab.floor}楼{lab.room}"
            for lab in (await session.execute(select(Laboratory))).scalars().all()
        }
    zhangwei, lina = by_name["张伟"], by_name["李娜"]
    admin = by_name["管理员"]

    print("=" * 70)
    print("lab-booking-agent · 人员准入实证")
    print("=" * 70)
    print(f"  当前时间      : {now.isoformat(timespec='seconds')}")
    print(f"  入场时间窗    : {win_start:%H:%M}-{win_end:%H:%M}（今天）")
    print(f"  目标房间      : {labs[lab_cell]} / {labs[lab_spec]}")
    print()

    async def grant(user_id: int, lab_id: int) -> str:
        async with session_scope() as session:
            _, plain = await issue_permit(
                session,
                user_id=user_id,
                lab_id=lab_id,
                date_=day,
                valid_from=win_start,
                valid_to=win_end,
                source="admin_grant",
            )
            return plain

    async def enter(
        lab_id: int, *, credential: str | None = None, user_id: int | None = None
    ) -> EntryDecision:
        async with session_scope() as session:
            return await verify_entry(
                session,
                lab_id=lab_id,
                now=now,
                credential=credential,
                identity_user_id=user_id,
                gate_id="gate-demo",
            )

    failures: list[str] = []

    def report(index: int, title: str, decision: EntryDecision, want_ok: bool) -> None:
        ok = decision.ok is want_ok
        mark = "✓" if ok else "✗"
        verdict = "放行" if decision.ok else f"拒绝 · {decision.reason_code}"
        print(f"  场景 {index}  {title}")
        print(f"          → {mark} {verdict}")
        if decision.message:
            print(f"            {decision.message}")
        if not ok:
            failures.append(f"场景 {index}：期望 {'放行' if want_ok else '拒绝'}，实际 {verdict}")

    # ---- 场景 1：没预约就进不去，而且这件事有记录 ----
    d1 = await enter(lab_cell, user_id=zhangwei)
    report(1, "张伟没有预约，直接到门口刷卡", d1, want_ok=False)

    # ---- 场景 2：签发凭证后正常入场，并占住座位 ----
    cell_cred = await grant(lina, lab_cell)
    d2 = await enter(lab_cell, credential=cell_cred, user_id=lina)
    report(2, "给李娜签发凭证后刷卡入场", d2, want_ok=True)
    if d2.ok:
        async with session_scope() as session:
            seats = await inside_count(
                session, lab_cell, day, slot_index_of(now.time())
            )
        print(f"            {labs[lab_cell]} 当前格在馆 {seats} 人")

    # ---- 场景 3：已入场者重复刷卡（防重复占位）----
    # 注意这里**不是** permit_used：人还在馆内，先命中的是"不可同时在馆"那道闸门。
    # permit_used 要等出场之后才谈得上（见场景 7）。
    d3 = await enter(lab_cell, credential=cell_cred, user_id=lina)
    report(3, "李娜已经在馆内，再刷一次同一张凭证", d3, want_ok=False)

    # ---- 场景 4：人卡一致（防代刷）----
    # 必须**另发一张**：上面那张已经 checked_in，拿它测会先被"已在馆"拦掉，
    # 测到的就不是"人卡一致"这道闸门了 —— 用错前提会把验证测歪。
    fresh = await grant(lina, lab_cell)
    d4 = await enter(lab_cell, credential=fresh, user_id=zhangwei)
    report(4, "张伟拿着李娜的凭证来刷", d4, want_ok=False)

    # ---- 场景 5：预约资格（资质按今天算）----
    # 凭证能发出来，但张伟缺「色谱」资质，会在资质那道闸门被拦。
    # 这正是"能不能约"与"能不能进"必须分开判的原因。
    spec_cred_zw = await grant(zhangwei, lab_spec)
    d5 = await enter(lab_spec, credential=spec_cred_zw, user_id=zhangwei)
    report(5, "张伟持凭证进光谱色谱间（缺「色谱」资质）", d5, want_ok=False)

    # ---- 场景 6：出场销账，座位立刻释放 ----
    async with session_scope() as session:
        d6 = await verify_exit(session, user_id=lina, now=now, gate_id="gate-demo")
    report(6, "李娜刷卡出场", d6, want_ok=True)

    # ---- 场景 7：★ 单次核销 —— 已用过的凭证再刷就无效 ----
    # 这才是"截图转发"的真实形态：凭证本身还在李娜手机里，图片发给别人也没用。
    d7 = await enter(lab_cell, credential=cell_cred, user_id=lina)
    report(7, "用李娜**出场前那张**凭证再刷一次（模拟截图转发）", d7, want_ok=False)

    # ---- 场景 8： ★ 容量不变式 —— N 人并发抢容量 1 的房间 ----
    async with session_scope() as session:
        lab = await session.get(Laboratory, lab_spec)
        assert lab is not None
        original_capacity = lab.capacity
        lab.capacity = 1
    print(f"  场景 8  把 {labs[lab_spec]} 容量临时调成 1，"
          f"李娜与管理员**同时**刷卡（原容量 {original_capacity}）")

    creds = [(lina, await grant(lina, lab_spec)), (admin, await grant(admin, lab_spec))]

    async def attempt(user_id: int, credential: str) -> EntryDecision:
        async with session_scope() as session:
            return await verify_entry(
                session,
                lab_id=lab_spec,
                now=now,
                credential=credential,
                identity_user_id=user_id,
                gate_id="gate-demo-race",
            )

    outcomes = await asyncio.gather(
        *(attempt(uid, cred) for uid, cred in creds), return_exceptions=True
    )
    boom = [o for o in outcomes if isinstance(o, BaseException)]
    granted = [o for o in outcomes if isinstance(o, EntryDecision) and o.ok]
    denied = [o for o in outcomes if isinstance(o, EntryDecision) and not o.ok]
    if boom:
        failures.append(f"场景 8：并发核验抛异常 {boom[:1]}")
        print(f"          → ✗ 并发核验抛异常：{boom[:1]}")
    else:
        codes = ", ".join(o.reason_code for o in denied) or "无"
        mark = "✓" if len(granted) == 1 else "✗"
        print(f"          → {mark} 放行 {len(granted)} 人 / 拒绝 {len(denied)} 人（原因码：{codes}）")
        if len(granted) != 1:
            failures.append(f"场景 8：容量 1 却有 {len(granted)} 人进去")

    # ---- 收尾：未预约者被拦下这件事，必须有据可查 ----
    async with session_scope() as session:
        rows = (await session.execute(select(AccessEvent))).scalars().all()
    n_grant = sum(1 for r in rows if r.result == "granted")
    n_deny = sum(1 for r in rows if r.result == "denied")
    print()
    print(f"  access_events 流水：{len(rows)} 条（放行 {n_grant} / 拒绝 {n_deny}）")
    print("  ↑ 「限制未预约者进入」若没有这张表，就只是口头声称。")

    print("=" * 70)
    if failures:
        print("✗ 准入实证未通过：")
        for item in failures:
            print(f"    · {item}")
        return 1
    print("✓ 8 个场景全部符合预期")
    return 0


async def _access_demo() -> int:
    """跑在一次性沙箱库里，连跑两遍结论一样。"""
    async with _scratch_database("lagent-access-demo-"):
        return await _access_demo_steps()


# ==========================================================================
# migrate
# ==========================================================================
async def _migrate(revision: str, *, down: bool = False) -> int:
    """把库迁移到指定 revision，并打印前后版本。

    为什么值得单独一个子命令，而不是让人直接敲 `alembic upgrade head`：
    alembic 自己那条路径要额外带对 `-c alembic.ini`、环境变量也得先设好；
    而 `python main.py migrate` 与 `doctor` / `serve` 用的是**同一套配置加载**，
    不会出现「迁移升的库和服务连的库不是同一个」。
    """
    before, head = await revision_status()
    print(f"  库当前 revision : {before or '（未纳入迁移管理 / 空库）'}")
    print(f"  代码 head       : {head}")

    if down:
        after = await downgrade(revision)
    else:
        # `alembic upgrade base` 是**静默无效**的：base 不是前向目的地。
        # 「命令跑成功了、库一点没变」是最难查的一类问题，所以在这里直接拦下来，
        # 而不是让它返回 0 然后什么都不做。
        if revision == "base":
            print("  ✗ --revision base 是「回退到空库」，属于回退方向。")
            print("    要回退请显式加 --down：python main.py migrate --revision base --down")
            return 2
        after = await migrate(revision)

    if after == before:
        print(f"  结果            : 已是最新，无需迁移（{after}）")
    else:
        print(f"  结果            : {before or '空库'} → {after}")

    drift = await schema_drift()
    if drift:
        print("  ⚠ 迁移已执行，但结构仍与代码不一致：")
        for item in drift:
            print(f"      · {item}")
        raise SchemaDriftError(drift)
    print("  结构校验        : 与代码一致")
    return 0


# ==========================================================================
# sweep —— 后台清扫的「手动跑一轮」入口
# ==========================================================================
async def _sweep() -> int:
    """跑一轮清扫并打印每项处理了多少。

    为什么运维命令和后台循环要共用同一份实现：如果 CLI 是另一套代码，
    它就会慢慢长成「演示时好使、线上跑的是另一个东西」。
    这里的每个任务与 ``SweepRunner`` 里跑的完全是同一个函数。

    退出码非 0 表示**至少有一项失败**。刻意不给"部分成功"单独的码：
    运维只需要一个可判断的信号 —— 有没有东西出错。
    """
    from .sweep import count_pending, run_once

    before = await count_pending()
    results = await run_once()

    print("=" * 70)
    print("lab-booking-agent · 后台清扫（单轮）")
    print("=" * 70)
    print("  清扫前待处理：")
    for key, value in before.items():
        print(f"    {key:<26} {value}")
    print()
    for result in results:
        if result.error:
            print(f"  ✗ {result.name:<20} 失败：{result.error}")
        else:
            print(f"  ✓ {result.name:<20} {result.processed:>4} 条   {result.detail}")

    after = await count_pending()
    print()
    print("  清扫后待处理：")
    for key, value in after.items():
        mark = "" if before[key] == value else f"  （{before[key]} → {value}）"
        print(f"    {key:<26} {value}{mark}")
    print("=" * 70)

    failed = [r for r in results if not r.ok]
    if failed:
        print(f"✗ {len(failed)} 项失败，见上")
        return 1
    print(f"✓ 全部 {len(results)} 项完成（处理 {sum(r.processed for r in results)} 条）")
    return 0


# ==========================================================================
# 入口
# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lab-booking-agent", description="智能实验室预约 Agent")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor", help="环境自检：数据库、检索、模型、对话链路")
    sub.add_parser("tools", help="列出已注册的工具")

    migrate_parser = sub.add_parser("migrate", help="把数据库迁移到指定 revision（默认 head）")
    migrate_parser.add_argument(
        "--revision", default="head", help="目标 revision（默认 head；回退时常用 base）"
    )
    migrate_parser.add_argument(
        "--down",
        action="store_true",
        help="回退方向（downgrade）。不加 = 前向升级；--revision base --down 会倒空所有表",
    )

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

    sub.add_parser(
        "access-demo",
        help="人员准入实证：未预约拦截 / 单次核销 / 人卡一致 / 容量不变式（沙箱库）",
    )

    sub.add_parser(
        "sweep",
        help="后台清扫单轮：过期预约 / 凭证超时与关门收尾 / 审计归档（会改数据）",
    )

    sub.add_parser("serve", help="启动 FastAPI 服务（等同 python main.py）")
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.command == "doctor":
        return await _doctor()
    if args.command == "migrate":
        return await _migrate(args.revision, down=args.down)
    if args.command == "tools":
        for spec in TOOL_SPECS:
            mark = "（写操作 · 默认不对模型暴露）" if spec["side_effect"] else ""
            print(f"{spec['name']}{mark}: {spec['description']}")
            for name, desc in spec["params"].items():
                print(f"    {name}" + (f"  —— {desc}" if desc else ""))
        return 0
    if args.command == "seed":
        # seed() 内部先走 ensure_schema(rebuild=force)：
        # --force 是「删表重建」而不是「只删行」—— 只删行修不了「缺列」，
        # 这也正是 README 那句「要重建请加 --force」曾经失效的原因。
        info = await seed(force=args.force)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    if args.command == "chat":
        return await _chat(args.message, args.user, args.session)
    if args.command == "loadtest":
        return await _loadtest(args.concurrency, args.rounds)
    if args.command == "eval":
        return await _eval(args.cases)
    if args.command == "access-demo":
        return await _access_demo()
    if args.command == "sweep":
        # 先走一遍 seed()：它会 ensure_schema（迁移到 head + 校验结构），
        # 于是「库过期」这种情况在这里就报出可照做的提示，而不是等清扫 SQL 崩。
        await seed()
        return await _sweep()
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
    except SchemaDriftError as exc:
        # 库结构过期是**配置问题，不是程序缺陷**：给一段能照着做的话，
        # 而不是让用户从五十行 SQLAlchemy 堆栈里自己看出「该重建库」。
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        with suppress(Exception):
            asyncio.run(dispose_engine())


if __name__ == "__main__":
    sys.exit(main())
