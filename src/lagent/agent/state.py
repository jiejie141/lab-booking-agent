"""Agent 状态与轻量会话存储。

会话存储是**进程内**的，保存两样东西：

1. 「上一轮给用户列出的备选方案」—— 让用户回一句「第 2 个」时能对上号；
2. 「最近的对话轮次」—— ReAct 模式的上下文来源（确定性模式不需要它，
   因为那条路径的跨轮状态只有上面那个诉求）。

这是一个显式的已知限制：多进程部署时会话会漂移，生产形态应当把它换成
Redis 或数据库表（PG 下更该用 LangGraph checkpointer）。之所以先这样做：
它让多轮真的可用，而代价被写在文档里而不是藏起来。
"""

from __future__ import annotations

import datetime as dt
import operator
import threading
from typing import Annotated, Any, TypedDict

from ..schemas import (
    BookingOutcome,
    ConstraintCheck,
    DocHit,
    Proposal,
    Requirement,
    TraceStep,
)


class AgentState(TypedDict, total=False):
    """图状态。每个节点只写自己负责的字段，避免互相覆盖。

    ``trace`` 用 ``Annotated[..., operator.add]`` 声明 **累加** 语义。
    LangGraph 的默认合并策略是「同名键后写覆盖前写」，所以如果只写成
    ``trace: list[TraceStep]``，每个节点返回的都会把前一个节点的记录冲掉 ——
    最后只留下最后一步。这个坑很隐蔽：图能跑通、结果也对，只是可观测性静默丢失。
    """

    # 输入
    message: str
    history: str
    user_id: int
    session_id: str
    accept_equipment_id: int | None
    accept_date: dt.date | None
    accept_start: dt.time | None
    accept_end: dt.time | None

    # 解析
    intent: str
    requirement: Requirement
    missing: list[tuple[str, str]]

    # 协商
    satisfied: bool
    proposals: list[Proposal]
    blockers: list[str]
    checks: list[ConstraintCheck]
    blocker_kind: str
    citations: list[DocHit]

    # 结果
    booking: BookingOutcome | None
    reply: str
    stage: str
    trace: Annotated[list[TraceStep], operator.add]
    degraded: bool
    selected_index: int | None


class SessionStore:
    """进程内会话：记「上一轮列出的备选」「最近的诉求」「是否在等用户补槽位」。

    ``awaiting`` 这个标志不是可有可无的 —— 没有它就无法区分两种情况：

      * 上一轮我们问了「哪天？几点？」，用户这轮只是补一句「明天下午两点」→ **该合并**；
      * 用户这轮说的是一个全新请求 → **绝不能**把上一轮的诉求带进来。

    早期版本无条件合并，于是「我想约个设备」会继承上一轮的设备与时间，
    直接跳过追问给出了方案。这是多轮状态最容易出错的地方，值得一个显式标志。
    """

    def __init__(self, max_sessions: int = 512, max_turns: int = 12) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._max = max_sessions
        self._max_turns = max_turns

    def save(
        self,
        session_id: str,
        proposals: list[Proposal],
        requirement: Requirement,
        *,
        awaiting: bool = False,
    ) -> None:
        with self._lock:
            # **更新而不是替换**：ReactAgent 会在同一会话里累积对话历史，
            # 整体替换会把历史一起清掉（那种 bug 表现为「多轮突然失忆」，
            # 而且只在 React 模式下出现，很难联想到是这里）。
            entry = self._data.setdefault(session_id, {})
            entry.update(
                {
                    "proposals": list(proposals),
                    "requirement": requirement,
                    "awaiting": awaiting,
                }
            )
            if session_id in self._order:
                self._order.remove(session_id)
            self._order.append(session_id)
            while len(self._order) > self._max:
                self._data.pop(self._order.pop(0), None)

    def append_turn(self, session_id: str, role: str, content: str) -> None:
        """追加一条对话消息（ReAct 模式的上下文来源）。

        只留最近 ``max_turns`` 条：这里的目的是给模型近场语境，
        不是做完整存档 —— 长期记忆该由 checkpointer 或数据库承担。
        """
        if not content:
            return
        with self._lock:
            entry = self._data.setdefault(session_id, {})
            turns: list[dict[str, str]] = entry.setdefault("turns", [])
            turns.append({"role": role, "content": content})
            if len(turns) > self._max_turns:
                del turns[: len(turns) - self._max_turns]

    def history(self, session_id: str) -> list[dict[str, str]]:
        with self._lock:
            turns = self._data.get(session_id, {}).get("turns", [])
            # 返回副本：调用方可能把它并进 messages 再被 ContextBuilder 裁剪，
            # 直接给内部列表会让那次裁剪意外改动存储内容。
            return [dict(turn) for turn in turns]

    def proposals(self, session_id: str) -> list[Proposal]:
        with self._lock:
            return list(self._data.get(session_id, {}).get("proposals", []))

    def requirement(self, session_id: str) -> Requirement | None:
        with self._lock:
            return self._data.get(session_id, {}).get("requirement")

    def awaiting(self, session_id: str) -> bool:
        """上一轮是否在等用户补槽位。只有这种时候才允许跨轮合并诉求。"""
        with self._lock:
            return bool(self._data.get(session_id, {}).get("awaiting", False))

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._data.pop(session_id, None)
            if session_id in self._order:
                self._order.remove(session_id)


# 进程级单例。测试里可以直接 new 一个，不用清全局状态。
STORE = SessionStore()


def pick_proposal(message: str, proposals: list[Proposal]) -> Proposal | None:
    """把「第 2 个」「2」「就第二个吧」这类回复对应到具体方案。"""
    if not proposals:
        return None
    text = message.strip()
    if any(word in text for word in ("不用", "算了", "都不", "再想想")):
        return None
    digits = [ch for ch in text if ch.isdigit()]
    if not digits:
        return None
    index = int(digits[0])
    if 1 <= index <= len(proposals):
        return proposals[index - 1]
    return None
