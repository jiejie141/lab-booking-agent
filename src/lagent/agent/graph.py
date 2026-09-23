"""LangGraph 编排：一次会话的状态流转。

图结构（条件边即「工具路由」，见 tools.py 的说明）：

    parse ──┬─ cancel_reservation ─→ cancel ─────────────┐
            ├─ check_admission ────→ retrieve ───────────┤
            ├─ smalltalk ────────────────────────────────┤
            └─ query/create ─→ check_missing ─┬─ 缺 ─→ ask ─→ END
                                             └─ 齐 ─→ negotiate ─┬─ 可下单 ─→ book ─┐
                                                                 └─ 需确认/协商 ─────┤
                                                                                    ↓
                                                                                 compose ─→ END

两个「多轮」的设计点：

1. **追问是环**：缺槽位不是直接失败，而是把问题抛回给用户；下一轮把新信息与
   上一轮的诉求**合并**（新值优先、旧值补位），所以「明天下午两点」和「用光谱仪」
   分两句话说也能凑齐。

2. **备选可被选中**：协商结果会存进会话，用户回一句「第 2 个」即可下单。
   这是本图里唯一需要跨轮记忆的部分，因此它的存储限制被显式写在 state.py。
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from typing import Any

from langgraph.graph import END, StateGraph

from ..clock import now_local
from ..config import get_settings
from ..schemas import (
    ChatRequest,
    ChatResponse,
    NegotiationResult,
    Requirement,
    TraceStep,
)
from . import tools
from .llm import LLMClient
from .state import STORE, AgentState, SessionStore, pick_proposal

DEGRADED_REPLY = (
    "智能助手当前不可用（模型未配置或已关闭），已切换为引导式预约：\n"
    "请按「日期 → 起止时间 → 设备名」的格式直接告诉我，"
    "例如「2026-09-25 14:00-16:00 荧光光谱仪」，我仍会照常做约束校验与冲突检测。"
)


def merge_requirements(prev: Requirement | None, new: Requirement) -> Requirement:
    """多轮槽位合并：新的一轮优先，缺的用上一轮补。"""
    if prev is None:
        return new.normalized()
    merged = {}
    for field in ("date", "start", "end", "duration_hours", "equipment_name",
                  "category", "lab_hint", "capacity", "purpose"):
        value = getattr(new, field, None)
        if value in (None, "", 0):
            value = getattr(prev, field, None)
        merged[field] = value
    return Requirement(**merged).normalized()


class LabBookingAgent:
    """把图、模型客户端与会话存储绑成一个可调用的对象。"""

    def __init__(
        self,
        client: LLMClient | None,
        store: SessionStore | None = None,
        *,
        now_provider=now_local,
    ) -> None:
        self.client = client
        self.store = store if store is not None else STORE
        self.now_provider = now_provider
        self.graph = self._build()

    # ------------------------------------------------------------------
    # 图装配
    # ------------------------------------------------------------------
    def _build(self):
        graph = StateGraph(AgentState)
        graph.add_node("parse", self._parse)
        graph.add_node("ask", self._ask)
        graph.add_node("negotiate", self._negotiate)
        graph.add_node("book", self._book)
        graph.add_node("cancel", self._cancel)
        graph.add_node("retrieve", self._retrieve)
        graph.add_node("compose", self._compose)

        graph.set_entry_point("parse")
        graph.add_conditional_edges(
            "parse",
            self._after_parse,
            {
                "cancel": "cancel",
                "retrieve": "retrieve",
                "check_missing": "ask",   # 实际由 check_missing 节点决定，这里保留直连
                "negotiate": "negotiate",
                "compose": "compose",
            },
        )
        graph.add_conditional_edges(
            "ask",
            self._after_ask,
            {"ask": END, "negotiate": "negotiate"},
        )
        graph.add_edge("negotiate", "book")
        graph.add_edge("book", "compose")
        graph.add_edge("cancel", "compose")
        graph.add_edge("retrieve", "compose")
        graph.add_edge("compose", END)
        return graph.compile()

    # ------------------------------------------------------------------
    # 节点实现
    # ------------------------------------------------------------------
    async def _parse(self, state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        session_id = state.get("session_id", "default")

        # 控制台点选具体方案：字段是显式传进来的，不必再花一次模型调用去抽
        accepted = _from_accept(state)
        if accepted is not None:
            return self._trace(
                "parse",
                started,
                {
                    "intent": "create_reservation",
                    "requirement": Requirement(
                        date=accepted.date, start=accepted.start, end=accepted.end
                    ).normalized(),
                    "stage": "accepted",
                },
            )

        # 再看是不是在「用序号选上一轮的备选」
        pending = self.store.proposals(session_id)
        picked = pick_proposal(state.get("message", ""), pending)
        if picked is not None:
            return self._trace(
                "parse",
                started,
                {
                    "intent": "create_reservation",
                    "requirement": Requirement(
                        date=picked.date,
                        start=picked.start,
                        end=picked.end,
                        equipment_name=picked.equipment_name,
                    ).normalized(),
                    "selected_index": pending.index(picked) + 1,
                    "stage": "parsed",
                },
            )

        intent = await self.client.classify_intent(state["message"])
        # 只有上一轮在「等用户补槽位」时才跨轮合并；否则新请求应当独立成立，
        # 不能把上一轮的设备/时间带进来（否则「我想约个设备」会直接跳到出方案）。
        prev = self.store.requirement(session_id) if self.store.awaiting(session_id) else None
        extracted = await self.client.extract_requirement(
            state["message"], history=prev.summary() if prev else ""
        )
        merged = merge_requirements(prev, extracted)

        return self._trace(
            "parse",
            started,
            {
                "intent": intent.intent,
                "requirement": merged,
                "stage": "parsed",
            },
        )

    def _after_parse(self, state: AgentState) -> str:
        intent = state.get("intent", "smalltalk")
        # 点选方案或已用序号选中：需求已经明确，直接去下单
        if state.get("stage") == "accepted":
            return "negotiate"
        if intent == "cancel_reservation":
            return "cancel"
        if intent == "check_admission":
            return "retrieve"
        if intent in ("query_availability", "create_reservation"):
            return "negotiate" if state.get("selected_index") else "check_missing"
        return "compose"

    async def _ask(self, state: AgentState) -> dict[str, Any]:
        """缺槽位 → 追问，同时把已知信息存起来，等下一轮合并。"""
        started = time.perf_counter()
        requirement = state["requirement"]
        missing = requirement.missing_slots()
        if not missing:
            return {"missing": [], "stage": "slots_ready"}

        reply = await self.client.ask_missing(missing, requirement)
        # 标记「在等用户补槽位」，下一轮才允许把这次已知的信息合并进来
        self.store.save(state.get("session_id", "default"), [], requirement, awaiting=True)
        return self._trace(
            "ask",
            started,
            {
                "missing": [field for field, _ in missing],
                "reply": reply,
                "stage": "awaiting_slots",
            },
        )

    def _after_ask(self, state: AgentState) -> str:
        return "negotiate" if not state.get("missing") else "ask"

    async def _negotiate(self, state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        requirement = state["requirement"]

        # 已选定具体时段（控制台点选、或对话里用序号选中）：直接去下单，不再重新协商。
        # 注意这里**不能**把 stage 改写成别的值：
        # stage 是贯穿全图的状态标记，_book 还要靠 "accepted" 认出「这是点选下单」。
        # 早期版本在这里统一写成 "selected"，于是点选路径到了 _book 认不出自己，
        # 直接把用户点选的方案丢掉，回一句「没找到可用时段」。
        if state.get("stage") == "accepted":
            return self._trace("negotiate", started, {"stage": "accepted"})
        if state.get("selected_index"):
            return self._trace("negotiate", started, {"stage": "selected"})

        result: NegotiationResult = await tools.tool_query_availability(
            state["user_id"], requirement, now=self.now_provider()
        )
        self.store.save(
            state.get("session_id", "default"), result.proposals, requirement, awaiting=False
        )
        return self._trace(
            "negotiate",
            started,
            {
                "satisfied": result.satisfied,
                "proposals": result.proposals,
                "blockers": result.blockers,
                # 结构化原因一并带上：回复层要靠它区分「缺资质」与「时段被占」，
                # 只给一串人话 blocker，回复就只能猜，最后一律劝人改时间。
                "checks": result.checks,
                "blocker_kind": result.blocker_kind,
                "stage": "negotiated",
            },
        )

    async def _book(self, state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        requirement = state["requirement"]
        intent = state.get("intent")
        proposals = state.get("proposals") or []
        satisfied = state.get("satisfied", False)

        # 决定要不要真的下单：
        #  * 用户点选了某个方案（字段是显式传进来的）→ 下；
        #  * 用户用序号选了上一轮的方案 → 下；
        #  * 用户明确说「帮我约」且只有一个精确解 → 下；
        #  * 有多个选择或有放宽 → 先让用户确认（compose 会列出方案）
        #
        # 点选路径用「字段在不在」判定，而不是看 stage 等于什么：
        # accept_* 字段是请求里带的既成事实，比一个可能被中途改写的字符串可靠。
        target = _from_accept(state)
        if target is None and state.get("selected_index"):
            target = _from_selection(state, self.store)
        if target is None and intent == "create_reservation" and satisfied and len(proposals) == 1:
            target = proposals[0]

        if target is None:
            return self._trace("book", started, {"stage": "needs_confirmation"})

        outcome = await tools.tool_create_reservation(
            user_id=state["user_id"],
            equipment_id=target.equipment_id,
            date_=target.date,
            start=target.start,
            end=target.end,
            purpose=requirement.purpose,
        )
        if outcome.ok:
            self.store.clear(state.get("session_id", "default"))
        return self._trace(
            "book", started, {"booking": outcome, "stage": "booked" if outcome.ok else "booking_failed"}
        )

    async def _cancel(self, state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        message = state.get("message", "")
        digits = "".join(ch for ch in message if ch.isdigit())
        if not digits:
            rows = await tools.tool_list_reservations(state["user_id"])
            active = [r for r in rows if r.status in ("pending", "confirmed")]
            if not active:
                return self._trace("cancel", started, {
                    "reply": "你名下没有可取消的预约。", "stage": "cancel_none"})
            lines = ["你名下的有效预约："]
            for idx, row in enumerate(active, start=1):
                lines.append(f"{idx}. #{row.id} {row.slot} {row.equipment_name}")
            lines.append("告诉我编号（例如「取消 #12」）我就去取消。")
            return self._trace("cancel", started, {"reply": "\n".join(lines), "stage": "cancel_need_id"})

        outcome = await tools.tool_cancel_reservation(
            reservation_id=int(digits), user_id=state["user_id"], reason="用户对话取消"
        )
        return self._trace("cancel", started, {"booking": outcome, "stage": "cancelled"})

    async def _retrieve(self, state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        hits = await tools.tool_check_admission(state.get("message", ""))
        return self._trace("retrieve", started, {"citations": hits, "stage": "retrieved"})

    async def _compose(self, state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        # 节点里已经写好确定文案的（追问 / 取消说明）直接沿用，不再过一次模型
        if state.get("stage") in ("awaiting_slots", "cancel_none", "cancel_need_id"):
            return self._trace("compose", started, {"reply": state.get("reply", "")})

        if state.get("intent") == "check_admission":
            kind = "admission"
        elif state.get("stage") == "booked" or state.get("stage") == "booking_failed":
            kind = "booking"
        elif state.get("intent") == "smalltalk":
            kind = "smalltalk"
        else:
            kind = "availability"

        ctx = {
            "kind": kind,
            "satisfied": state.get("satisfied", False),
            "proposals": state.get("proposals") or [],
            "blockers": state.get("blockers") or [],
            "checks": state.get("checks") or [],
            "blocker_kind": state.get("blocker_kind"),
            "booking": state.get("booking"),
            "citations": state.get("citations") or [],
        }
        reply = await self.client.compose(ctx)
        return self._trace("compose", started, {"reply": reply, "stage": "composed"})

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    async def ainvoke(self, req: ChatRequest) -> ChatResponse:
        if self.client is None:
            return ChatResponse(
                reply=DEGRADED_REPLY,
                intent=None,
                stage="degraded",
                degraded=True,
                trace=[TraceStep(node="degrade", detail="模型未配置或已关闭，走引导式表单")],
            )

        initial: AgentState = {
            "message": req.message,
            "user_id": req.user_id,
            "session_id": req.session_id,
            "accept_equipment_id": req.accept_equipment_id,
            "accept_date": req.accept_date,
            "accept_start": req.accept_start,
            "accept_end": req.accept_end,
            "trace": [],
        }
        final = await self.graph.ainvoke(initial)

        intent = final.get("intent")
        return ChatResponse(
            reply=final.get("reply", ""),
            intent=intent,  # type: ignore[arg-type]
            stage=final.get("stage", ""),
            missing=list(final.get("missing") or []),
            proposals=final.get("proposals") or [],
            citations=final.get("citations") or [],
            booking=final.get("booking"),
            trace=final.get("trace") or [],
        )

    # ------------------------------------------------------------------
    def _trace(self, node: str, started: float, update: dict[str, Any]) -> dict[str, Any]:
        """只返回自己这一步的记录。

        trace 在 AgentState 里声明了 ``operator.add`` 的累加语义，所以这里
        **必须**只给单元素列表；再手动把前序内容拼进去会导致重复累加。
        两者只能选一个，选了累加器就不能自己再拼。
        """
        step = TraceStep(
            node=node,
            detail=_describe(update),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        update["trace"] = [step]
        return update


# --------------------------------------------------------------------------
def _describe(update: dict[str, Any]) -> str:
    stage = update.get("stage", "")
    if "proposals" in update:
        return f"得到 {len(update['proposals'])} 个方案（阶段 {stage}）"
    if "booking" in update and update["booking"] is not None:
        return f"下单结果 {'成功' if update['booking'].ok else '失败'}：{update['booking'].message[:40]}"
    if "citations" in update:
        return f"检索到 {len(update['citations'])} 条规范"
    if "missing" in update:
        return f"缺失字段 {update['missing']}"
    if "reply" in update:
        return f"生成回复 {len(update['reply'])} 字"
    return f"阶段 {stage}"


def _from_selection(state: AgentState, store: SessionStore):
    """按序号取出上一轮列出的某个方案。

    注意 store 必须由调用方传入：早期版本这里直接读了模块级单例 STORE，
    于是测试里注入的 SessionStore 被忽略，选方案永远查不到东西。
    """
    session_id = state.get("session_id", "default")
    proposals = store.proposals(session_id)
    index = state.get("selected_index") or 0
    if 1 <= index <= len(proposals):
        return proposals[index - 1]
    return None


@dataclass
class _Target:
    """下单目标。字段名与 Proposal 一致，_book 里靠鸭子类型统一处理两种来源。"""

    equipment_id: int
    date: dt.date
    start: dt.time
    end: dt.time


def _from_accept(state: AgentState) -> _Target | None:
    """控制台点选方案时走这条：参数即事实，不再依赖会话里存过什么。"""
    equipment_id = state.get("accept_equipment_id")
    date_ = state.get("accept_date")
    start = state.get("accept_start")
    if not (equipment_id and date_ and start):
        return None
    return _Target(
        equipment_id=equipment_id,
        date=date_,
        start=start,
        end=state.get("accept_end") or start,
    )


def build_agent(client: LLMClient | None, store: SessionStore | None = None) -> LabBookingAgent:
    return LabBookingAgent(client, store)


def build_agent_from_settings(store: SessionStore | None = None):
    """按配置建 Agent；模型不可用时返回的 Agent 会走降级回复。"""
    settings = get_settings()

    from .llm import build_client

    catalog = _catalog_cache
    client = build_client(settings, catalog)
    return LabBookingAgent(client, store)


_catalog_cache: list[tuple[str, str]] | None = None


def set_catalog(catalog: list[tuple[str, str]]) -> None:
    """启动时把设备目录灌进来，让 Mock/真实模型都能识别用户点名的设备。"""
    global _catalog_cache
    _catalog_cache = catalog
