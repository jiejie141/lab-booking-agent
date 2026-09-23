"""执行模式与 ReAct 运行时：把「选哪个工具」这件事交给模型。

本项目原来只有一种执行模式：条件边决定工具（见 agent/graph.py），模型只做
语言层的活。那是**刻意为强约束业务做的收敛**，不是能力缺失 —— 但它意味着
模型从未拥有过工具选择权，也就没有 function calling 这件事。

本模块补上另一条路径，两者共用同一份 :class:`ToolRegistry`：

    ``deterministic``  条件边路由（原路径）。高风险动作、golden path 走这条。
    ``react``          模型通过 function calling 自己选工具、看结果、再决定。
                       通用 Agent 场景要的就是这条。

**为什么要两条而不是把旧的换掉**：预约是强约束业务，让模型自由选工具会引入
「该查可用性却直接下单」这类幻觉风险；而通用场景（比如按模糊需求找人/找线索）
又必须让模型自己决策。把两者都留在代码里、用一个配置切换，比争论哪种更好
有价值得多 —— 这也是「沉淀可复用的 Agent 执行能力」在工程上的具体含义。

安全边界放在这一层，而不是指望 prompt：
    :meth:`ReActRuntime.run` 在执行前检查工具的 ``side_effect`` 标记，
    **拒绝**模型自行触发的写操作，并把拒绝原因回喂给它。
    仅靠「不要把写工具告诉模型」是不够的 —— 模型可能凭常识猜出工具名
    （``create_reservation`` 这种名字太好猜了），而注册表里它确实存在。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from .context import ContextBuilder
from .tools import ToolRegistry, ToolResult, gather_tools
from .trace import SpanCollector

ExecutionMode = Literal["deterministic", "react"]
StopReason = Literal["final", "max_steps", "error"]


# --------------------------------------------------------------------------
# 模型侧的工具调用契约
# --------------------------------------------------------------------------
class ToolCall(BaseModel):
    """模型请求的一次工具调用。``arguments`` 保持成 dict。

    与 wire format 的差别要在这里抹平：OpenAI 协议里 ``arguments`` 是
    **JSON 字符串**（而且模型偶尔会给出非法 JSON），解析属于协议适配，
    属于具体客户端（agent/llm.py）的职责。这一层只处理结构化之后的形状 ——
    否则运行时要为「字符串还是字典」到处写分支。
    """

    id: str = ""
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class TurnResult(BaseModel):
    """模型一轮的回复：要么给最终文本，要么要求调工具（也可能两者都有）。"""

    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class ToolCallingLLM(Protocol):
    """运行时期待的模型能力。业务侧的实现（Mock / Real）负责满足它。"""

    name: str

    async def chat_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> TurnResult: ...


@dataclass
class ReActOutcome:
    """一次 ReAct 执行的产物。

    ``stopped_reason`` 是这里最重要的字段：**「跑完了」和「跑不动了」必须可区分**。
    只返回一段文本的话，上层无法判断这次是该正常呈现，还是该走降级。
    """

    reply: str
    steps: int = 0
    stopped_reason: StopReason = "final"
    trace: SpanCollector = field(default_factory=SpanCollector)
    tool_names_used: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    error: str = ""
    # 保留**未截断**的工具结果。上层要靠它取结构化事实（proposals / citations）——
    # 从回喂给模型的那段截断文本里再解析回对象，是自找的麻烦且必然出错。
    tool_results: list[ToolResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.stopped_reason == "final"


class ReActRuntime:
    """think → tool_call → tool_result → think … → final 的循环。"""

    def __init__(
        self,
        *,
        llm: ToolCallingLLM,
        registry: ToolRegistry,
        context: ContextBuilder | None = None,
        max_steps: int = 6,
        allow_side_effects: bool = False,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.context = context or ContextBuilder()
        self.max_steps = max_steps
        # 默认 False：模型不得自行触发写操作。这是安全默认值，改它要有理由。
        self.allow_side_effects = allow_side_effects

    async def run(
        self,
        *,
        user_message: str,
        system: str,
        history: list[dict[str, Any]] | None = None,
        memory: str = "",
        trace: SpanCollector | None = None,
    ) -> ReActOutcome:
        trace = trace if trace is not None else SpanCollector()
        history = list(history or [])
        user_msg: dict[str, Any] = {"role": "user", "content": user_message}
        # 本轮产生的「assistant(tool_calls) + tool(result)」交替序列。
        # 单独成层而不是并进 history，是为了让 ContextBuilder 能把它当作
        # **最低优先级**整体裁剪 —— 工具结果是原料，最先该被牺牲。
        tool_layer: list[dict[str, Any]] = []
        collected: list[ToolResult] = []
        used: list[str] = []
        refused: list[str] = []

        for step in range(1, self.max_steps + 1):
            built = self.context.build(
                system=system,
                memory=memory,
                recent=[*history, user_msg],
                tool_results=tool_layer,
            )
            trace.record("decision", f"step {step}", detail=built.describe())

            try:
                with trace.span("llm", f"chat_tools#{step}") as span:
                    turn = await self.llm.chat_tools(
                        built.messages, self.registry.openai_tools()
                    )
                    span.detail = f"tool_calls={len(turn.tool_calls)}"
            except Exception as exc:  # noqa: BLE001 - 模型故障要变成可判断的返回值，交由上层降级
                return ReActOutcome(
                    reply="",
                    steps=step - 1,
                    stopped_reason="error",
                    trace=trace,
                    tool_names_used=used,
                    refused=refused,
                    error=f"{type(exc).__name__}: {exc}",
                    tool_results=collected,
                )

            if not turn.tool_calls:
                return ReActOutcome(
                    reply=turn.content or "",
                    steps=step,
                    stopped_reason="final",
                    trace=trace,
                    tool_names_used=used,
                    refused=refused,
                    tool_results=collected,
                )

            tool_layer.append(_assistant_tool_calls(turn.tool_calls))
            results = await self._dispatch(turn.tool_calls)
            collected.extend(results)
            for call, res in zip(turn.tool_calls, results, strict=True):
                spec = self.registry.get(res.name)
                if spec is not None and spec.side_effect and not self.allow_side_effects:
                    # 被安全边界拦下的调用单独记一笔。它和「工具执行失败」不是一回事：
                    # 前者说明模型越界了（该看 prompt 或分工），后者是工具自己的问题。
                    refused.append(res.name)
                else:
                    used.append(res.name)
                trace.record(
                    "tool",
                    res.name,
                    elapsed_ms=res.elapsed_ms,
                    detail="失败" if not res.ok else "成功",
                    ok=res.ok,
                )
                tool_layer.append(
                    {"role": "tool", "tool_call_id": call.id, "content": res.for_model()}
                )

        # 步数用尽仍未收敛：把「没跑完」如实报出去，让上层决定降级，
        # 而不是硬编一句话假装完成了。
        return ReActOutcome(
            reply="",
            steps=self.max_steps,
            stopped_reason="max_steps",
            trace=trace,
            tool_names_used=used,
            refused=refused,
            tool_results=collected,
        )

    async def _dispatch(self, calls: list[ToolCall]) -> list[ToolResult]:
        """执行一批工具调用，**顺带拦下模型不该触发的那些**。

        返回值顺序与 ``calls`` 严格一致：拼回消息时 tool_call_id 必须对得上。
        """
        blocked: dict[int, ToolResult] = {}
        allowed: list[tuple[int, ToolCall]] = []
        for idx, call in enumerate(calls):
            spec = self.registry.get(call.name)
            if spec is not None and spec.side_effect and not self.allow_side_effects:
                blocked[idx] = ToolResult(
                    name=call.name,
                    ok=False,
                    error=(
                        f"{call.name} 会改变系统状态，属于需要通过确认的写操作，"
                        "不能由模型自行触发。请先向用户复述将要执行的动作并取得确认。"
                    ),
                )
            else:
                allowed.append((idx, call))

        executed = await gather_tools(
            self.registry, [(call.name, call.arguments) for _, call in allowed]
        )

        results: list[ToolResult | None] = [None] * len(calls)
        for (idx, _), res in zip(allowed, executed, strict=True):
            results[idx] = res
        for idx, res in blocked.items():
            results[idx] = res
        return [r for r in results if r is not None]


def _assistant_tool_calls(calls: list[ToolCall]) -> dict[str, Any]:
    """按 wire format 拼回 assistant 消息。

    ``arguments`` 必须是 JSON 字符串（协议要求），所以这里要 dump 回去 ——
    在 :class:`ToolCall` 里转成 dict 是为了运行时好写，代价就是在这一处还原。
    """
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in calls
        ],
    }
