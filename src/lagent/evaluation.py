"""评测：把「模型能听懂中文」变成可核对的数字。

三档分开计分，**不合成一个总分**：

    intent   意图识别命中期望意图
    slots    槽位抽取的关键字段正确（日期 / 时间窗 / 设备）
    e2e      端到端：最终给出期望形态的结果（备选 / 直接下单 / 追问 / 明确拒绝）

为什么不合成：把三者混起来会掩盖退化。假设意图全对、槽位全错，
合成分看着还有个七八成，实际系统已经不可用了。分开报才能一眼看出坏在哪一层。
这也是上个项目「两层结构要分开评测」那条结论的直接沿用。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .agent.graph import LabBookingAgent, set_catalog
from .agent.llm import build_client
from .agent.state import SessionStore
from .agent.tools import load_catalog
from .clock import now_local, parse_time
from .config import get_settings
from .schemas import ChatRequest, Requirement

DEFAULT_CASES = Path(__file__).resolve().parents[2] / "eval" / "cases.yaml"

OUTCOMES = ("proposals", "available", "booked", "ask", "blocked", "answer", "smalltalk")


@dataclass
class CaseResult:
    case_id: str
    message: str
    intent_ok: bool
    slots_ok: bool
    e2e_ok: bool
    notes: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.intent_ok and self.slots_ok and self.e2e_ok

    def flags(self) -> str:
        return "".join(
            "✓" if flag else "✗"
            for flag in (self.intent_ok, self.slots_ok, self.e2e_ok)
        )


@dataclass
class EvalReport:
    results: list[CaseResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    def rate(self, attr: str) -> float:
        if not self.results:
            return 0.0
        hit = sum(1 for r in self.results if getattr(r, attr))
        return hit / len(self.results)

    @property
    def passed(self) -> bool:
        return all(r.ok for r in self.results)

    def render(self) -> str:
        lines = [
            "=" * 78,
            f"评测报告 · {self.total} 条用例",
            "=" * 78,
            f"{'用例':<6}{'意图':<5}{'槽位':<5}{'端到端':<7} 说明",
            "-" * 78,
        ]
        for row in self.results:
            note = "；".join(row.notes[:2]) if row.notes else row.detail.get("outcome", "")
            lines.append(f"{row.case_id:<6}{row.flags():<15}{note}")
        lines.append("-" * 78)
        lines.append(
            f"意图准确率 {self.rate('intent_ok'):.1%} · "
            f"槽位准确率 {self.rate('slots_ok'):.1%} · "
            f"端到端通过率 {self.rate('e2e_ok'):.1%}"
        )
        failed = [r.case_id for r in self.results if not r.ok]
        lines.append(f"全部通过 {self.total - len(failed)}/{self.total}" + (f" · 未过 {failed}" if failed else ""))
        lines.append("=" * 78)
        return "\n".join(lines)


def load_cases(path: str | Path | None = None) -> list[dict]:
    target = Path(path) if path else DEFAULT_CASES
    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    return data.get("cases", [])


# --------------------------------------------------------------------------
def _check_slots(req: Requirement, expect: dict, today: dt.date) -> tuple[bool, dict]:
    """逐字段核对槽位。只有用例声明了的字段才参与判定。"""
    cur = req.normalized()
    detail: dict = {"date": cur.date.isoformat() if cur.date else None,
                    "start": cur.start.strftime("%H:%M") if cur.start else None,
                    "end": cur.end.strftime("%H:%M") if cur.end else None,
                    "duration": cur.duration_hours,
                    "equipment": cur.equipment_name,
                    "category": cur.category}
    ok = True

    if "date_offset" in expect:
        want_date = today + dt.timedelta(days=int(expect["date_offset"]))
        if cur.date != want_date:
            ok = False
            detail["date_expected"] = want_date.isoformat()
    if "date" in expect:
        want_date = dt.date.fromisoformat(str(expect["date"]))
        if cur.date != want_date:
            ok = False
            detail["date_expected"] = want_date.isoformat()
    if "start" in expect:
        want_time = parse_time(str(expect["start"]))
        if cur.start != want_time:
            ok = False
            detail["start_expected"] = want_time.strftime("%H:%M")
    if "end" in expect:
        want_time = parse_time(str(expect["end"]))
        if cur.end != want_time:
            ok = False
            detail["end_expected"] = want_time.strftime("%H:%M")
    if "duration" in expect:
        want_hours = float(expect["duration"])
        if cur.duration_hours is None or abs(cur.duration_hours - want_hours) > 1e-6:
            ok = False
            detail["duration_expected"] = want_hours
    if "equipment_contains" in expect:
        needle = str(expect["equipment_contains"])
        got = cur.equipment_name or ""
        # 名字没抽到但类别抽到了也算通过（用户可能只说类别）
        if (
            needle not in got
            and got not in needle
            and not (cur.category and needle[:2] in str(cur.category))
        ):
            ok = False
            detail["equipment_expected"] = needle
    if "category" in expect:
        want_category = str(expect["category"])
        if want_category != (cur.category or ""):
            ok = False
            detail["category_expected"] = want_category
    return ok, detail


def _classify_outcome(resp) -> str:
    """把一次对话的结果归类成可比较的形态。

    归类顺序即优先级：先看降级、再看是否在等用户补信息、再看是否真的下单成功，
    最后才区分「给了备选」「给了出处」「给了明确拒绝」。
    """
    if resp.degraded:
        return "degraded"
    if resp.stage == "awaiting_slots":
        return "ask"
    if resp.booking is not None and resp.booking.ok:
        return "booked"
    if resp.proposals:
        return "available" if not _has_relaxations(resp) else "proposals"
    if resp.intent == "smalltalk":
        return "smalltalk"
    if resp.intent == "check_admission" and resp.citations:
        return "answer"
    if resp.stage in ("cancel_need_id", "cancel_none"):
        return "answer"
    if resp.booking is not None and not resp.booking.ok:
        # 取消失败 / 下单被拒也属于「给了明确答复」，不等于「什么都没给」
        return "answer"
    return "blocked"


def _has_relaxations(resp) -> bool:
    return any(p.relaxations for p in resp.proposals)


async def run_eval(cases_path: str | Path | None = None, *, verbose: bool = False) -> EvalReport:
    settings = get_settings()
    catalog = await load_catalog()
    set_catalog(catalog)
    client = build_client(settings, catalog)
    if client is None:
        raise RuntimeError("模型不可用，无法评测（app_mode=degraded 或缺 API key）")

    store = SessionStore()
    agent = LabBookingAgent(client, store)
    today = now_local().date()
    report = EvalReport()

    for case in load_cases(cases_path):
        case_id = case.get("id", "?")
        turns = case.get("turns") or [case.get("message", "")]
        user_id = int(case.get("user", 2))
        session = f"eval-{case_id}"
        notes: list[str] = []

        # ---- 意图与槽位：直接查模型层，单独计分 ----
        intent_ok = True
        slots_ok = True
        if "expect_intent" in case:
            got = await client.classify_intent(turns[-1])
            intent_ok = got.intent == case["expect_intent"]
            if not intent_ok:
                notes.append(f"意图 {got.intent}≠{case['expect_intent']}")

        slot_detail: dict = {}
        if "expect_slots" in case:
            req = await client.extract_requirement(turns[-1])
            slots_ok, slot_detail = _check_slots(req, case["expect_slots"], today)
            if not slots_ok:
                notes.append(f"槽位不符 {slot_detail}")

        # ---- 端到端：跑完整图 ----
        resp = None
        for turn in turns:
            resp = await agent.ainvoke(
                ChatRequest(message=turn, user_id=user_id, session_id=session)
            )
        if resp is None:
            raise ValueError(f"用例 {case_id} 没有任何对话轮次，无法评测")
        outcome = _classify_outcome(resp)
        expect_outcome = case.get("expect_outcome")
        e2e_ok = True
        if expect_outcome:
            e2e_ok = outcome == expect_outcome
            if not e2e_ok:
                notes.append(f"结果 {outcome}≠{expect_outcome}｜{resp.reply[:50]}")

        report.results.append(
            CaseResult(
                case_id=case_id,
                message=turns[-1],
                intent_ok=intent_ok,
                slots_ok=slots_ok,
                e2e_ok=e2e_ok,
                notes=notes,
                detail={"outcome": outcome, "slots": slot_detail,
                        "proposals": len(resp.proposals),
                        "trace": [t.node for t in resp.trace]},
            )
        )
        if verbose:
            print(f"  {case_id:<6}{'✓' if e2e_ok else '✗'} {outcome:<11} {turns[-1][:40]}")

    return report
