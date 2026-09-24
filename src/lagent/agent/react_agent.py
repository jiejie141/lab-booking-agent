"""ReAct 模式的 Agent：由模型决定调哪个工具。

与 ``LabBookingAgent``（确定性编排）的关系是**同一系统的两种执行模式**，
不是两个系统：两者共用工具注册表、共用会话存储、对外返回同一个
:class:`~lagent.schemas.ChatResponse`，所以 API / CLI / 评测都不需要知道
当前跑的是哪一种。

**降级链在这里落地**（对应「保障 Agent 线上运行稳定性」那条要求）：

    react ──模型故障 / 步数耗尽 / 不按格式回──▶ deterministic ──模型显式关闭──▶ 引导式表单

中间那一跳才是关键：ReAct 依赖模型会调工具，而模型**可能不按格式回** ——
返回纯文本、给非法参数、或反复调同一个工具直到步数用尽。这些都不该让用户
看到报错，而应该退回到那条不依赖「模型选工具」、只用模型翻译语言的路径。
把这条链写出来，比声称「我的 Agent 很稳」有说服力得多。

**这条链能覆盖什么、不能覆盖什么，要说准确**（否则就是一句挡不住追问的漂亮话）：

- 能覆盖：模型**还在**、只是不肯好好调工具。此时退回确定性编排真的能救回来。
- 不能覆盖：模型端点**连不上**。因为确定性路径同样依赖模型做意图识别与槽位抽取
  （``classify_intent`` / ``extract_requirement``），两条路都需要模型做 NLU，
  退回没有意义。这种情况下的既有契约是「接口明确失败（502）+ 前端切引导式表单」，
  见 ``api.py`` 里 chat 端点的注释；``scripts/smoke_http.py`` 的 [11] 段
  把两种模式在同样条件下的返回值逐字对照过，确认这是原有行为而非本模块引入的。

    换句话说：**只有当故障是「模型不听话」而不是「模型不在」时，这一层才有用。**
    把它说成"模型挂了也能兜住"是不诚实的 —— 那样得让 NLU 也能离线跑，是另一件事。
"""

from __future__ import annotations

from ..clock import now_local
from ..config import Settings, get_settings
from ..harness import (
    ContextBuilder,
    ReActOutcome,
    ReActRuntime,
    Span,
    ToolCallingLLM,
    ToolResult,
)
from ..schemas import (
    ChatRequest,
    ChatResponse,
    DocHit,
    NegotiationResult,
    Proposal,
    TraceStep,
)
from .graph import DEGRADED_REPLY, LabBookingAgent
from .state import STORE, SessionStore
from .tools import build_registry

# 系统提示词按「什么时候该用哪个工具」写，而不是按「有哪些工具」写。
# 后者是 schema 的职责（tools 参数已经给了模型），重复一遍只会让提示词更长、
# 且两处描述一旦不一致，模型会以提示词为准 —— 于是 schema 与提示词打架。
REACT_SYSTEM_PROMPT = (
    "你是实验室设备预约助手。你可以调用工具获取信息，再依据工具结果回答用户。\n"
    "规则：\n"
    "1. 要判断某天有没有可用时段，必须先调用 query_availability；"
    "不要在没查之前声称某个时段可用。\n"
    "2. 用户问资质、准入规定或安全要求时，调用 check_admission，并引用检索到的条文出处。\n"
    "3. 用户问自己约了什么时，调用 list_reservations。\n"
    "4. 创建与取消预约会改变记录，**不要尝试调用**；需要时告诉用户你会先向他确认。\n"
    "5. 信息不足（例如没说日期）时直接追问用户，不要猜。\n"
    "6. 只能用工具结果里出现过的事实作答，不要补充不存在的时段、设备或条文。"
)

MAX_STEPS_REPLY = (
    "这个问题我没能在限定步数内查清。\n"
    "可以把需求说得更具体一些（例如「明天下午两点到四点用荧光光谱仪」）我再试一次；"
    "也可以直接按「日期 → 起止时间 → 设备名」的格式告诉我。"
)


class ReActAgent:
    """模型自主决策的执行入口，对外契约与 :class:`LabBookingAgent` 一致。"""

    def __init__(
        self,
        # 这里刻意**不**要完整的 ``LLMClient``，只要 ``ToolCallingLLM``：
        # ReAct 路径用到客户端的地方只有 ``chat_tools`` 一处（第 106 行传给
        # ReActRuntime）。少写一个过度宽的类型有两个好处 ——
        #   * 测试可以用只实现 chat_tools 的脚本化假模型，不必为了满足协议
        #     再补出四个用不到的 NLU 方法（那种"为通过类型检查而写"的空实现
        #     是最容易腐烂的代码）；
        #   * 类型本身说明了职责边界：NLU（意图/槽位）是 deterministic 路径与
        #     降级链的事，不是 ReAct 的事。
        client: ToolCallingLLM | None,
        store: SessionStore | None = None,
        *,
        settings: Settings | None = None,
        fallback: LabBookingAgent | None = None,
        now_provider=now_local,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client
        self.store = store if store is not None else STORE
        self.now_provider = now_provider
        # 未收敛时的确定性回退。允许为 None（测试里常这样），此时直接报降级。
        self.fallback = fallback

    async def ainvoke(self, req: ChatRequest) -> ChatResponse:
        # 身份是硬前提，与确定性路径同一条不变量：
        # 缺了就既判不了资质、也归属不了预约，必须 fail-closed。
        if req.user_id is None:
            raise ValueError("ChatRequest.user_id 缺失：身份必须由调用方显式提供")
        if self.client is None:
            return _degraded()

        session_id = req.session_id
        # 注册表按请求构建：user_id 要闭包注入（身份不能来自模型参数），
        # 而 now 也要跟着请求走，否则测试里注入的时钟不起作用。
        registry = build_registry(
            user_id=req.user_id,
            now=self.now_provider(),
            result_limit=self.settings.tool_result_limit,
        )
        runtime = ReActRuntime(
            llm=self.client,
            registry=registry,
            context=ContextBuilder(max_tokens=self.settings.context_max_tokens),
            max_steps=self.settings.react_max_steps,
            allow_side_effects=self.settings.react_allow_side_effects,
        )
        outcome = await runtime.run(
            user_message=req.message,
            system=REACT_SYSTEM_PROMPT,
            history=self.store.history(session_id),
        )

        if outcome.stopped_reason == "final":
            self.store.append_turn(session_id, "user", req.message)
            self.store.append_turn(session_id, "assistant", outcome.reply)
            return _to_response(outcome)

        # ---- 未收敛：走降级链 ----
        if self.fallback is not None:
            delegated = await self.fallback.ainvoke(req)
            # 降级必须在 trace 里留痕。否则线上看到一条"正常"的回复，
            # 没人知道它其实是退回来的 —— 降级不可观测等于没有降级。
            delegated.trace.insert(
                0,
                TraceStep(
                    node="degrade",
                    detail=f"react 未收敛（{outcome.stopped_reason}），退回确定性编排",
                ),
            )
            return delegated

        if outcome.stopped_reason == "max_steps":
            return ChatResponse(
                reply=MAX_STEPS_REPLY,
                stage="react_max_steps",
                trace=_to_trace_steps(outcome.trace.spans()),
            )
        return _degraded(detail=f"react 失败：{outcome.error}")


# --------------------------------------------------------------------------
def _to_response(outcome: ReActOutcome) -> ChatResponse:
    """把运行时的产物映射成对外契约。

    ``intent`` 刻意留空：在 ReAct 模式下，意图不再是流程里的一个显式中间产物 ——
    模型直接决定了动作，没有人再去算它。硬猜一个（比如"调了 query_availability
    就当 create_reservation"）会把推测当成事实写进响应，宁可如实留空。
    """
    proposals, citations = _collect(outcome.tool_results)
    return ChatResponse(
        reply=outcome.reply,
        intent=None,
        stage=f"react_{outcome.stopped_reason}",
        proposals=proposals,
        citations=citations,
        trace=_to_trace_steps(outcome.trace.spans()),
    )


def _collect(results: list[ToolResult]) -> tuple[list[Proposal], list[DocHit]]:
    """从**未截断**的工具结果里取结构化事实。

    取最后一个成功的调用而不是全部累加：模型可能查了两次可用性（比如先按
    原条件、再按放宽条件），最终回答依据的是后一次；把两次的 proposals
    合并会让控制台列出一堆互相矛盾的时段。
    """
    proposals: list[Proposal] = []
    citations: list[DocHit] = []
    for res in results:
        if not res.ok:
            continue
        if res.name == "query_availability" and isinstance(res.payload, NegotiationResult):
            proposals = list(res.payload.proposals)
        elif res.name == "check_admission" and isinstance(res.payload, list):
            citations = [hit for hit in res.payload if isinstance(hit, DocHit)]
    return proposals, citations


def _to_trace_steps(spans: list[Span]) -> list[TraceStep]:
    """把 harness 的 span 转成控制台在用的 TraceStep。

    转换放在业务层而不是 harness 里：harness 不依赖 ``lagent.schemas``
    （见 harness/__init__.py 的设计红线），而控制台想要什么形状属于业务侧知识。
    """
    return [
        TraceStep(
            node=f"{span.kind}:{span.name}",
            detail=span.detail or ("失败" if not span.ok else "ok"),
            elapsed_ms=span.elapsed_ms,
        )
        for span in spans
    ]


def _degraded(detail: str = "模型未配置或已关闭，走引导式表单") -> ChatResponse:
    return ChatResponse(
        reply=DEGRADED_REPLY,
        intent=None,
        stage="degraded",
        degraded=True,
        trace=[TraceStep(node="degrade", detail=detail)],
    )
