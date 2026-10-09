"""Agent 状态与会话存储。

会话存两样东西：

1. 「上一轮给用户列出的备选方案」—— 让用户回一句「第 2 个」时能对上号；
2. 「最近的对话轮次」—— ReAct 模式的上下文来源（确定性模式不需要它，
   因为那条路径的跨轮状态只有上面那个诉求）。

**两种后端，同一套接口**（由 `LAB_STATE_BACKEND` 选，见 api.py）：

* `SessionStore` —— 进程内 dict，默认。单进程最简单；
* `SqliteSessionStore` —— 同机多进程共享一份（WAL），重启不丢。

两者都不是分布式方案：跨**机器**的多副本仍需要 Redis / 共享数据库
（PG 下更该用 LangGraph checkpointer）。那是 P2 的事，本项目不假装
一个 SQLite 就解决了分布式 —— 但「多进程」这个真实需求已经被接住了。
"""

from __future__ import annotations

import datetime as dt
import json
import operator
import threading
import time
from typing import Annotated, Any, TypedDict

from ..localstate import connect, init_state_db
from ..metrics import record_state_backend
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

    # 会话存储的后端名。基类是 memory，持久化子类覆盖它。
    #
    # 为什么是**类属性**而不是在 __init__ 里写死 "memory"：SqliteSessionStore
    # 继承本类，写死的话它会先被登记成 memory、再被自己登记成 sqlite ——
    # 同一个 component 出现两条序列，单副本就被
    # LagentStateBackendSplitBrain 误报成「已扩容、状态分裂」。
    # 后端由类决定，指标就不会随构造顺序漂移。
    _BACKEND = "memory"

    def __init__(
        self, max_sessions: int = 512, max_turns: int = 12, *, report_backend: bool = True
    ) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._max = max_sessions
        self._max_turns = max_turns
        # 与限流器同一条理由：后端由组件自己报，换实现时指标自动跟着变。
        #
        # report_backend=False 只给模块级兜底单例 STORE 用：它在 import 时就被
        # 构造，而这个进程真正用的后端要等 api.py 按配置选完才知道。让它照报
        # "memory"，sqlite 模式下就会同时存在 memory 与 sqlite 两条 session 序列，
        # 单副本被 LagentStateBackendSplitBrain 误判成「已扩容、状态分裂」。
        if report_backend:
            record_state_backend("session", type(self)._BACKEND)

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


# 进程级兜底单例。测试里可以直接 new 一个，不用清全局状态。
#
# report_backend=False：它是 import 期产物，此刻还不知道进程最终用哪个后端
# （api.py 要读到配置才决定）。真实后端由 api.py 构造的那个实例上报。
STORE = SessionStore(report_backend=False)


class SqliteSessionStore(SessionStore):
    """SessionStore 的 SQLite 版：多进程共享一份，重启不丢（P2-1）。

    用**继承**而不是平行实现：save / append_turn / history / ... 是
    「同语义、不同存储」的关系，槽位上限、返回值形状这些约束由基类
    docstring 与测试统一约定 —— 针对 memory 版接口写的测试，换这个类
    原样重跑应当成立。

    与内存版的四点差异：

    * **跨进程**：WAL + busy_timeout（见 localstate.connect），同机多个
      worker / 脚本看到同一份会话 —— 「第 2 个」不再依赖上一轮落在哪个进程；
    * **TTL**：超过 session_ttl_hours 的会话在读写时惰性清理（视为不存在
      并删行）。跨轮记忆是缓存性质，不该变成永不回收的垃圾；
    * **坏数据自删**：JSON 解不开就删行返回空 —— 会话是缓存级数据，
      一条脏行炸掉整个服务，比让用户重说一遍需求糟糕得多；
    * **update-not-replace 语义不变**：先读旧 payload 合并再写回 ——
      React 的轮次历史被 save 冲掉，就是基类注释里那个「多轮突然失忆」，
      换了存储介质它也不会自己好。

    全部读写都是短连接（理由见 localstate），时间统一用 ``time.time()``：
    跨进程共享的过期判定必须用墙钟，monotonic 的起点是每个进程自己的。
    """

    # 覆盖基类的 "memory"：指标必须如实反映当前后端，否则「换实现但指标不变」
    # 就是指标层最典型的一种骗人。覆盖**类属性**而不是在 __init__ 里补一次
    # record_state_backend —— 补一次只会让两条序列同时存在（见基类的注释）。
    _BACKEND = "sqlite"

    def __init__(
        self,
        path: str,
        *,
        ttl_seconds: int = 24 * 3600,
        max_sessions: int = 512,
        max_turns: int = 12,
    ) -> None:
        self._path = path
        self._ttl = ttl_seconds
        init_state_db(path)  # 幂等：存储自己把表建好，不依赖调用方记得初始化
        super().__init__(max_sessions=max_sessions, max_turns=max_turns)

    # -- 内部：单行读写 ------------------------------------------------------
    def _load(self, conn, session_id: str) -> dict[str, Any] | None:
        """读单个会话。过期/损坏一律返回 None 并顺手删行。"""
        row = conn.execute(
            "SELECT payload, updated_at FROM agent_session WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        payload_text, updated_at = row
        if time.time() - updated_at > self._ttl:
            conn.execute("DELETE FROM agent_session WHERE session_id = ?", (session_id,))
            return None
        try:
            data = json.loads(payload_text)
        except (ValueError, TypeError):
            conn.execute("DELETE FROM agent_session WHERE session_id = ?", (session_id,))
            return None
        return data if isinstance(data, dict) else None

    def _write(self, conn, session_id: str, data: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO agent_session (session_id, payload, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET payload = excluded.payload, "
            "updated_at = excluded.updated_at",
            (session_id, json.dumps(data, ensure_ascii=False), time.time()),
        )
        # 容量上限与内存版同语义：LRU，只留最近 max_sessions 个会话。
        conn.execute(
            "DELETE FROM agent_session WHERE session_id IN ("
            "  SELECT session_id FROM agent_session "
            "  ORDER BY updated_at DESC LIMIT -1 OFFSET ?)",
            (self._max,),
        )

    # -- 对外接口（与内存版逐一同义） ------------------------------------------
    def save(
        self,
        session_id: str,
        proposals: list[Proposal],
        requirement: Requirement,
        *,
        awaiting: bool = False,
    ) -> None:
        conn = connect(self._path)
        try:
            data = self._load(conn, session_id) or {}
            # update 而不是整体替换：turns（React 历史）必须保留，理由见基类。
            data.update(
                {
                    "proposals": [p.model_dump(mode="json") for p in proposals],
                    "requirement": requirement.model_dump(mode="json"),
                    "awaiting": awaiting,
                }
            )
            self._write(conn, session_id, data)
        finally:
            conn.close()

    def append_turn(self, session_id: str, role: str, content: str) -> None:
        if not content:
            return
        conn = connect(self._path)
        try:
            data = self._load(conn, session_id) or {}
            turns: list[dict[str, str]] = data.setdefault("turns", [])
            turns.append({"role": role, "content": content})
            if len(turns) > self._max_turns:
                del turns[: len(turns) - self._max_turns]
            self._write(conn, session_id, data)
        finally:
            conn.close()

    def history(self, session_id: str) -> list[dict[str, str]]:
        conn = connect(self._path)
        try:
            data = self._load(conn, session_id) or {}
            return [dict(turn) for turn in data.get("turns", [])]
        finally:
            conn.close()

    def proposals(self, session_id: str) -> list[Proposal]:
        conn = connect(self._path)
        try:
            data = self._load(conn, session_id) or {}
            return [Proposal.model_validate(p) for p in data.get("proposals", [])]
        finally:
            conn.close()

    def requirement(self, session_id: str) -> Requirement | None:
        conn = connect(self._path)
        try:
            data = self._load(conn, session_id) or {}
            raw = data.get("requirement")
            return Requirement.model_validate(raw) if raw else None
        finally:
            conn.close()

    def awaiting(self, session_id: str) -> bool:
        """上一轮是否在等用户补槽位。只有这种时候才允许跨轮合并诉求。"""
        conn = connect(self._path)
        try:
            data = self._load(conn, session_id) or {}
            return bool(data.get("awaiting", False))
        finally:
            conn.close()

    def clear(self, session_id: str) -> None:
        conn = connect(self._path)
        try:
            conn.execute("DELETE FROM agent_session WHERE session_id = ?", (session_id,))
        finally:
            conn.close()


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
