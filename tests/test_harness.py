"""harness 单元测试：工具注册表 / 上下文预算 / span 埋点 / 分层边界。

这一组测试是「harness 真的成立」的证据，而不是覆盖率数字。三个重点：

1. **可插拔**：新增一个业务工具不需要改 harness 一行代码；
2. **裁剪顺序**：超预算时先丢什么、保什么，被确定性地钉住；
3. **分层边界**：用 AST 静态检查 harness 不依赖任何业务模块 ——
   分层如果只写在文档里，迟早会被一次「顺手 import」破掉。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

import lagent.harness as harness_pkg
from lagent.harness import (
    ContextBuilder,
    SpanCollector,
    ToolRegistry,
    ToolSpec,
    estimate_messages_tokens,
    estimate_tokens,
    gather_tools,
)


# ==========================================================================
# 分层边界：harness 不许依赖业务
# ==========================================================================
def test_harness_does_not_import_business_modules():
    """harness 包只能依赖标准库与 pydantic。

    这条用 AST 而不是 `grep import` 是因为要区分「相对导入跳出本包」——
    ``from .tools import X``（包内）合法，``from ..domain import Y``（跳出）违法，
    两者在文本上只差一个点。
    """
    pkg_dir = Path(harness_pkg.__file__).parent.resolve()
    offenders: list[str] = []

    for path in sorted(pkg_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level >= 2:
                    offenders.append(f"{path.name}: 相对导入跳出 harness -> {node.module}")
                elif (
                    node.level == 0
                    and node.module
                    and node.module.split(".")[0] == "lagent"
                    and node.module != "lagent.harness"
                ):
                    offenders.append(f"{path.name}: {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "lagent":
                        offenders.append(f"{path.name}: {alias.name}")

    assert not offenders, (
        "harness 依赖了业务模块，依赖方向反了（业务实现 harness 的接口，不是反过来）："
        f"{offenders}"
    )


# ==========================================================================
# 工具注册表
# ==========================================================================
class _EchoParams(BaseModel):
    text: str = Field(description="要回显的文本")


class _CountingParams(BaseModel):
    limit: int = Field(default=3, description="重复次数")


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()

    async def echo(*, text: str) -> dict[str, Any]:
        return {"echo": text}

    async def boom(*, text: str) -> str:
        raise RuntimeError("实现里故意抛错")

    async def counted(*, limit: int) -> list[int]:
        return list(range(limit))

    reg.register(ToolSpec(name="echo", description="回显", params=_EchoParams, handler=echo))
    reg.register(ToolSpec(name="boom", description="抛错", params=_EchoParams, handler=boom))
    reg.register(
        ToolSpec(
            name="bulk",
            description="返回超长结果",
            params=_CountingParams,
            handler=counted,
            result_limit=40,
        )
    )
    reg.register(
        ToolSpec(
            name="write_thing",
            description="有副作用",
            params=_EchoParams,
            handler=echo,
            side_effect=True,
        )
    )
    return reg


def test_registering_a_new_tool_needs_no_harness_change(registry):
    """这就是「可复用运行时」与「写死的工作流」的分界线。

    新增工具只做了一件事：声明一个参数模型 + 调一次 register。
    harness 的 schema 生成、校验、截断、副作用管控全部自动生效 ——
    没有任何一处需要为这个新工具加分支。
    """
    assert "echo" in registry.names()

    schema = next(t for t in registry.openai_tools() if t["function"]["name"] == "echo")
    params = schema["function"]["parameters"]
    # schema 是**从参数模型生成的**，不是手写的第二份定义
    assert params["properties"]["text"]["type"] == "string"
    assert "text" in params["required"]


def test_duplicate_registration_is_rejected(registry):
    """重名直接报错：静默覆盖会把「注册了但没生效」变成一场难查的调试。"""

    async def echo(**_: Any) -> str:
        return ""

    with pytest.raises(ValueError, match="重复注册"):
        registry.register(
            ToolSpec(name="echo", description="再来一个", params=_EchoParams, handler=echo)
        )


def test_side_effect_tools_are_hidden_from_the_model(registry):
    """**安全默认值**：有副作用的工具不进给模型的清单。

    仅在文档里写"不要让模型下单"是没用的约束 —— 它必须是一个可执行的事实。
    """
    exposed = {t["function"]["name"] for t in registry.openai_tools()}
    assert "write_thing" not in exposed
    assert registry.side_effect_names() == ["write_thing"]

    # 显式要求时才给（默认关闭，开启要有理由）
    included = {
        t["function"]["name"] for t in registry.openai_tools(include_side_effects=True)
    }
    assert "write_thing" in included


async def test_valid_call_returns_payload(registry):
    result = await registry.invoke("echo", {"text": "你好"})
    assert result.ok
    assert result.payload == {"echo": "你好"}
    assert not result.truncated


async def test_unknown_tool_error_lists_what_is_available(registry):
    """错误信息要能让模型自己改对，所以必须带上可选项。"""
    result = await registry.invoke("nope", {})
    assert not result.ok
    assert "没有名为 nope 的工具" in result.error
    assert "echo" in result.error


async def test_bad_arguments_become_a_feed_back_error(registry):
    """参数错 → 变成可回喂的数据，而不是异常。

    这是「让模型自己重试」能成立的前提：错误必须以它能读懂、能据以修正的形式返回。
    """
    result = await registry.invoke("echo", {"wrong_field": 1})
    assert not result.ok
    assert "参数校验失败" in result.error
    assert "text" in result.error  # 缺哪个字段要说出来
    assert "重试" in result.error  # 以及下一步该做什么


async def test_handler_exception_is_normalized(registry):
    """工具自己抛错不该穿透到运行时。

    穿透会让一次工具故障变成一次请求故障；归一成数据后，
    循环可以继续，上层也能决定是重试还是降级。
    """
    result = await registry.invoke("boom", {"text": "x"})
    assert not result.ok
    assert "RuntimeError" in result.error
    assert "故意抛错" in result.error


async def test_long_result_is_truncated_with_a_marker(registry):
    """超长结果要截断，并**告诉模型这是节选**。

    不给标记的话，模型会以为自己看到了全部内容，然后基于残缺信息继续推。
    """
    result = await registry.invoke("bulk", {"limit": 200})
    assert result.ok
    assert result.truncated
    text = result.for_model()
    assert "结果已截断" in text
    assert len(text) < 200
    # payload 仍是完整的 —— 代码用的那份不该被模型侧的预算影响
    assert result.payload == list(range(200))


async def test_gather_preserves_order(registry):
    """并行执行但顺序必须与入参一致，否则拼回消息时 tool_call_id 会串位。"""
    results = await gather_tools(registry, [("echo", {"text": "a"}), ("echo", {"text": "b"})])
    assert [r.payload["echo"] for r in results] == ["a", "b"]


# ==========================================================================
# 上下文预算
# ==========================================================================
def test_estimate_tokens_is_cjk_aware():
    """中文按字计、英文按 4 字符计 —— 不区分会让中文的预算严重低估。"""
    assert estimate_tokens("你好世界") == 4
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("") == 0
    # 每条消息有固定的结构性开销
    assert estimate_messages_tokens([{"role": "user", "content": ""}]) == 4


def test_small_context_is_not_trimmed():
    builder = ContextBuilder(max_tokens=1000)
    build = builder.build(
        system="你是助手",
        memory="2026-09-24 · 14:00-16:00",
        recent=[{"role": "user", "content": "明天下午两点"}],
    )
    assert not build.trimmed
    assert build.over_budget is False
    assert build.messages[0]["role"] == "system"
    assert build.messages[-1]["content"] == "明天下午两点"


def test_system_and_current_message_survive_the_tightest_budget():
    """永不丢弃的两条：规则与本次任务。

    这里同时是**一个回归点**：早期实现把当前用户消息既算进必留集合、
    又被当作历史取了一次，于是 prompt 里出现两遍。
    """
    builder = ContextBuilder(max_tokens=60)
    build = builder.build(
        system="规则",
        recent=[
            {"role": "user", "content": "很早以前的一句话" * 20},
            {"role": "assistant", "content": "很早以前的回答" * 20},
            {"role": "user", "content": "当前这句话"},
        ],
    )
    assert build.messages[0]["content"] == "规则"
    texts = [m.get("content") for m in build.messages]
    assert texts.count("当前这句话") == 1
    assert build.dropped_recent >= 1


def test_tool_results_are_sacrificed_before_history():
    """裁剪优先级：工具结果先于对话历史被丢。

    理由是信息半衰期 —— 工具结果的结论通常已经进了上一轮 assistant 的回复，
    而对话历史还承担着指代（"那它呢？"）。
    """
    builder = ContextBuilder(max_tokens=120)
    heavy = "\u4e2d" * 60  # 60 token 左右的单条消息
    build = builder.build(
        system="规则",
        recent=[
            {"role": "user", "content": "上一轮问题"},
            {"role": "assistant", "content": "上一轮回答"},
            {"role": "user", "content": "当前问题"},
        ],
        tool_results=[
            {"role": "tool", "tool_call_id": "1", "content": heavy},
            {"role": "tool", "tool_call_id": "2", "content": heavy},
        ],
    )
    assert build.dropped_tool_results >= 1
    assert build.dropped_recent == 0


def test_live_tool_result_survives_a_starving_budget():
    """这一轮刚拿到的工具结果**永不丢弃** —— 丢了 ReAct 就一定不收敛。

    回归点：早期实现把工具层整体按 token 裁剪，取「最新的后缀」，
    预算一小连最新那条都留不下。模型看不到结果，只会把同一个工具再调一遍，
    直到步数耗尽 —— 表现为「Agent 明明配好了工具却总说没查清」。

    装不下时的正确做法是**照发并如实标注 over_budget**：
    让「预算配小了」这件事被看见，而不是用丢关键上下文的方式假装没超。
    """
    builder = ContextBuilder(max_tokens=60)
    build = builder.build(
        system="规则",
        recent=[{"role": "user", "content": "问题"}],
        tool_results=[{"role": "tool", "tool_call_id": "9", "content": "中" * 200}],
    )
    assert any(m.get("tool_call_id") == "9" for m in build.messages)
    assert build.over_budget


def test_tool_messages_keep_their_assistant_parent():
    """整轮取舍：绝不留下「有 tool 结果、没有对应 assistant 请求」的孤儿消息。

    OpenAI 协议要求每条 ``tool`` 消息前面必须有一条携带同一 ``tool_call_id``
    的 assistant 消息，孤儿的后果是真实 API 直接 400 —— 一个只会在生产暴露、
    离线 mock 下完全看不见的故障。

    这条专门构造了「逐条裁剪会留下孤儿」的场景：较旧那一轮里 assistant 请求
    很大、tool 结果很小。逐条裁剪会丢大的留小的（于是孤儿出现），
    按轮裁剪则整轮一起进出。
    """
    builder = ContextBuilder(max_tokens=130)
    build = builder.build(
        system="规则",
        recent=[{"role": "user", "content": "问题"}],
        tool_results=[
            # 较旧的一轮：assistant 请求很重、tool 结果很轻
            {"role": "assistant", "content": "长" * 200, "tool_calls": [{"id": "a"}]},
            {"role": "tool", "tool_call_id": "a", "content": "短"},
            # 最新一轮：整体很轻，必然留下
            {"role": "assistant", "content": "", "tool_calls": [{"id": "b"}]},
            {"role": "tool", "tool_call_id": "b", "content": "短"},
        ],
    )
    kept_ids = {m.get("tool_call_id") for m in build.messages if m.get("role") == "tool"}
    assert "b" in kept_ids, "最新一轮的结果必须留下"
    for tid in kept_ids:
        has_parent = any(
            m.get("role") == "assistant"
            and any(call.get("id") == tid for call in (m.get("tool_calls") or []))
            for m in build.messages
        )
        assert has_parent, f"tool_call_id={tid} 成了孤儿消息"


def test_memory_is_truncated_not_dropped_entirely():
    """摘要放不下就截尾：留头部（日期/时间/设备在开头），不是整块丢弃。"""
    builder = ContextBuilder(max_tokens=40)
    build = builder.build(system="规则", memory="日期2026-09-24 " + "很长的补充说明" * 20,
                          recent=[{"role": "user", "content": "问题"}])
    assert build.memory_truncated
    memory_msgs = [m for m in build.messages if "[已知诉求]" in str(m.get("content"))]
    assert memory_msgs, "预算再紧，摘要也该留一部分，而不是整块消失"


def test_over_budget_is_reported_instead_of_silently_dropping():
    """预算窄到连 system 都装不下时，如实上报。

    偷偷丢一条继续跑会掩盖「预算配小了」这个真问题。
    """
    build = ContextBuilder(max_tokens=1).build(system="这是一段很长的规则" * 10)
    assert build.over_budget
    assert build.messages  # 仍然返回内容，但标注了状态


def test_build_is_deterministic():
    """同输入必得同输出 —— 裁剪逻辑必须可复现，否则测试与线上会漂移。"""
    kwargs: dict[str, Any] = {
        "system": "规则",
        "memory": "摘要",
        "recent": [{"role": "user", "content": "问题"}],
        "tool_results": [{"role": "tool", "tool_call_id": "1", "content": "结果" * 30}],
    }
    builder = ContextBuilder(max_tokens=80)
    assert builder.build(**kwargs).messages == builder.build(**kwargs).messages


async def test_many_tool_rounds_stay_within_budget():
    """连续多轮工具调用后，上下文仍不超预算（ReAct 循环的真实压力场景）。

    同时钉住一条策略：裁剪取的是**后缀**而不是零散挑选 ——
    最新的工具结果必须留下（它正是这一轮要用的证据），
    而丢掉的总是最旧的那批。所以这里既断言预算，也断言最新一条还在。
    """
    builder = ContextBuilder(max_tokens=200)
    layer: list[dict[str, Any]] = []
    for i in range(10):
        layer.append({"role": "tool", "tool_call_id": str(i), "content": "结果" * 40})
        build = builder.build(
            system="规则", recent=[{"role": "user", "content": "问题"}], tool_results=layer
        )
        assert build.estimated_tokens <= 200, f"第 {i} 轮就超预算了"
        # 最新一条必须在：它是这一轮刚拿到的证据
        assert any(m.get("tool_call_id") == str(i) for m in build.messages), f"第 {i} 轮丢了最新结果"
        if i >= 2:
            assert build.dropped_tool_results >= 1, f"第 {i} 轮本该开始裁剪"


# ==========================================================================
# span 埋点
# ==========================================================================
def test_span_records_elapsed_and_kind():
    trace = SpanCollector(trace_id="t1")
    with trace.span("tool", "query_availability") as span:
        span.detail = "成功"
    spans = trace.spans()
    assert len(spans) == 1
    assert spans[0].kind == "tool"
    assert spans[0].ok
    assert spans[0].elapsed_ms >= 0


def test_failed_span_is_recorded_and_reraised():
    """失败也要留痕：只记成功的那些，排查时最需要的一条恰好不在。"""
    trace = SpanCollector()
    with pytest.raises(ValueError), trace.span("llm", "chat_tools"):
        raise ValueError("模型挂了")
    assert len(trace.failures()) == 1
    assert not trace.spans()[0].ok


def test_summary_aggregates_by_kind():
    """按 kind 聚合回答「慢在哪一类操作上」—— 这是这一层存在的理由。"""
    trace = SpanCollector()
    trace.record("llm", "a", elapsed_ms=100)
    trace.record("llm", "b", elapsed_ms=300)
    trace.record("tool", "c", elapsed_ms=20)
    summary = trace.summary()
    assert summary["llm"]["count"] == 2
    assert summary["llm"]["total_ms"] == 400
    assert summary["llm"]["max_ms"] == 300
    assert summary["tool"]["total_ms"] == 20
