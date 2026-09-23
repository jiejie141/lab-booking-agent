"""ReAct 路径的集成测试：模型真的能选工具、越界会被拦、不收敛会降级。

这一组是「function calling 真的实现了」的证据 —— 与 deterministic 路径的
测试分开写，因为两者要证明的事情不同：那边证明**规则**正确，
这边证明**模型决策 + 安全边界 + 降级链**成立。

跑在 mock 模型上（``MockLLMClient`` 的 ``chat_tools`` 是确定性替身），
所以数字可复现、不需要网络；真实模型的行为差异由 live 模式另行评测。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from lagent.agent.graph import LabBookingAgent
from lagent.agent.llm import MockLLMClient
from lagent.agent.react_agent import MAX_STEPS_REPLY, ReActAgent
from lagent.agent.state import SessionStore
from lagent.agent.tools import build_registry, tool_catalog, tool_list_reservations
from lagent.config import Settings
from lagent.harness import ContextBuilder, ReActRuntime, ToolCall, TurnResult
from lagent.schemas import ChatRequest

BOOKING_MESSAGE = "明天下午两点想用荧光光谱仪两小时"


class _ScriptedLLM:
    """按剧本返回若干轮结果的假模型，用来精确制造边界场景。"""

    name = "scripted"

    def __init__(self, turns: list[TurnResult]) -> None:
        self._turns = list(turns)
        self.seen_tools: list[list[str]] = []
        self.seen_messages: list[list[dict[str, Any]]] = []

    async def chat_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> TurnResult:
        self.seen_tools.append([t["function"]["name"] for t in tools])
        self.seen_messages.append(messages)
        if not self._turns:
            return TurnResult(content="（剧本用尽）")
        return self._turns.pop(0)


class _ToolsUnsupportedLLM(MockLLMClient):
    """除 ``chat_tools`` 外一切正常：专门用来触发 react → deterministic 降级。

    它模拟的是真实世界很常见的一种故障：模型端点不支持 function calling
    （或返回了协议外的响应），但普通对话仍然可用。
    """

    async def chat_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> TurnResult:
        raise RuntimeError("该端点不支持 function calling")


# ==========================================================================
# 工具清单：安全边界（不需要数据库）
# ==========================================================================
def test_side_effect_tools_are_not_offered_to_the_model():
    """给模型的清单里**不能**出现下单/取消。

    「不要让模型下单」如果只写在提示词里，就不是一条约束，而是一个请求。
    """
    registry = build_registry(user_id=2)
    offered = {t["function"]["name"] for t in registry.openai_tools()}
    assert offered == {"query_availability", "check_admission", "list_reservations"}
    assert set(registry.side_effect_names()) == {"create_reservation", "cancel_reservation"}


def test_no_tool_schema_exposes_user_id():
    """**身份不能是模型可填字段**（P0-2 那条结论在工具层的延续）。

    如果 ``user_id`` 出现在 schema 里，模型就获得了「以他人身份操作」的能力 ——
    这正是当初 ``ChatRequest.user_id`` 默认取 1 造成的越权入口，换个地方重开一次。

    这里查的是**参数的属性名**，而不是对整段 JSON 做子串匹配：后者会因为
    描述文案里随口提到一句而误报，且报了也说不清是哪个工具的哪个字段。
    """
    registry = build_registry(user_id=2)
    for tool in registry.openai_tools(include_side_effects=True):
        function = tool["function"]
        declared = set(function["parameters"].get("properties", {}))
        assert "user_id" not in declared, f"{function['name']} 暴露了 user_id"
        assert not any("user" in field for field in declared), function["name"]


def test_tool_schema_carries_no_internal_notes():
    """发给模型的 schema 不带任何「给维护者看」的内容。

    pydantic 默认会把参数模型的 docstring 提成 schema 的 ``description``，
    而我们的 docstring 里写的是内部结论（"默认不暴露给模型"、"见 harness/tools.py"）。
    这些文本一旦发出去，既白烧 token，也可能把模型引向不该碰的地方 ——
    所以 :meth:`ToolSpec.json_schema` 会做一次清洗，这条测试钉住它。
    """
    registry = build_registry(user_id=2)
    payload = json.dumps(registry.openai_tools(include_side_effects=True), ensure_ascii=False)
    for leak in ("harness/", "默认不暴露", "side_effect", "见 harness"):
        assert leak not in payload, f"内部备注泄进了工具描述：{leak}"
    for tool in registry.openai_tools():
        parameters = tool["function"]["parameters"]
        assert "title" not in parameters
        assert "description" not in parameters
        for field_schema in parameters.get("properties", {}).values():
            assert "title" not in field_schema


def test_tool_catalog_is_derived_from_the_registry():
    """展示清单从注册表派生，不再手写第二份 —— 两份定义必然漂移。"""
    catalog = tool_catalog()
    names = {item["name"] for item in catalog}
    assert names == set(build_registry(user_id=1).names())
    assert {item["name"] for item in catalog if item["side_effect"]} == {
        "create_reservation",
        "cancel_reservation",
    }


def test_catalog_shape_is_a_stable_contract():
    """``/api/tools`` 的**响应结构**必须有测试钉住，而不只是内容对。

    这是一个真踩过的坑：``params`` 一度从 ``{字段名: 说明}`` 被改成字段名列表，
    结果 **364 个测试全绿、只有 CLI 崩了** —— 因为当时所有断言都只看
    ``name`` / ``side_effect``，没人看 ``params`` 的类型。
    对外响应结构的变更不该靠"碰巧有人用到"来发现。
    """
    catalog = tool_catalog()
    assert catalog, "清单不该为空"
    for item in catalog:
        assert set(item) == {"name", "description", "params", "side_effect"}
        assert isinstance(item["params"], dict), f"{item['name']} 的 params 必须是 dict"
        for field, doc in item["params"].items():
            assert isinstance(field, str) and isinstance(doc, str)

    by_name = {item["name"]: item for item in catalog}
    # 参数说明与实际校验同源：展示层不该出现"控制台写着 A、模型看到的是 B"
    assert set(by_name["query_availability"]["params"]) == {
        "date", "start", "end", "duration_hours", "equipment_name", "category",
    }
    assert by_name["create_reservation"]["params"]["purpose"] == "用途说明"
    assert by_name["list_reservations"]["params"] == {}


# ==========================================================================
# 运行时的边界行为
# ==========================================================================
async def test_side_effect_call_from_model_is_refused_not_executed(isolated_db):
    """模型硬猜出 ``create_reservation`` 并调用时，必须被拦下。

    这一条是**安全边界的核心**：「不把工具告诉模型」只降低了它调用的概率 ——
    ``create_reservation`` 这种名字太好猜了，而且注册表里它确实存在。
    所以真正的防线在运行时：按 ``side_effect`` 标记拒绝，并把拒绝原因回喂。
    """
    llm = _ScriptedLLM([
        TurnResult(tool_calls=[ToolCall(
            id="c1",
            name="create_reservation",
            arguments={
                "equipment_id": 1,
                "date": "2026-12-31",
                "start": "09:00",
                "end": "10:00",
            },
        )]),
        TurnResult(content="好的，我先跟你确认再下单。"),
    ])
    runtime = ReActRuntime(llm=llm, registry=build_registry(user_id=2), max_steps=4)
    outcome = await runtime.run(user_message="帮我直接订了", system="测试")

    assert outcome.refused == ["create_reservation"]
    assert outcome.reply == "好的，我先跟你确认再下单。"
    # 未真的落库 —— 用「李娜名下有没有 12-31 的记录」反证
    rows = await tool_list_reservations(user_id=2)
    assert not [r for r in rows if str(r.date) == "2026-12-31"]


async def test_the_same_tool_still_works_for_business_code(isolated_db):
    """上一条拦的是「模型触发」，不是「工具坏了」。

    对照组：同一次构建出的注册表，由业务代码直接调用是通的。
    没有这个对照，上一条测试可能因为工具本身报错而假通过。
    """
    registry = build_registry(user_id=2)
    result = await registry.invoke(
        "create_reservation",
        {"equipment_id": 1, "date": "2026-12-31", "start": "09:00", "end": "10:00"},
    )
    # 可能因设备/资质约束失败，但**不是**被安全边界拒绝
    assert "不能由模型自行触发" not in result.error


async def test_max_steps_is_reported_not_faked():
    """步数用尽要如实上报 ``max_steps``，而不是硬编一句话假装完成。

    只返回文本的话，上层无法区分「跑完了」和「跑不动了」——
    降级链也就无从触发。
    """
    always_call = TurnResult(tool_calls=[ToolCall(id="x", name="不存在的工具", arguments={})])
    llm = _ScriptedLLM([always_call] * 10)
    runtime = ReActRuntime(llm=llm, registry=build_registry(user_id=1), max_steps=3)
    outcome = await runtime.run(user_message="随便", system="测试")

    assert outcome.stopped_reason == "max_steps"
    assert outcome.steps == 3
    assert outcome.reply == ""
    assert not outcome.ok


async def test_llm_failure_becomes_a_judgeable_value():
    """模型故障要变成可判断的返回值，而不是异常穿透到请求层。"""
    class _Boom:
        name = "boom"

        async def chat_tools(self, messages, tools):  # type: ignore[no-untyped-def]
            raise RuntimeError("连接被重置")

    runtime = ReActRuntime(llm=_Boom(), registry=build_registry(user_id=1), max_steps=2)
    outcome = await runtime.run(user_message="你好", system="测试")
    assert outcome.stopped_reason == "error"
    assert "连接被重置" in outcome.error
    assert not outcome.ok


async def test_bad_arguments_are_fed_back_and_the_loop_recovers():
    """非法参数 → 回喂 → 模型改对 → 收敛。这是 function calling 的自我修复回路。"""
    llm = _ScriptedLLM([
        TurnResult(tool_calls=[ToolCall(id="c1", name="query_availability", arguments={"day": "明天"})]),
        TurnResult(content="我换个参数再试：先向你要一个日期。"),
    ])
    runtime = ReActRuntime(llm=llm, registry=build_registry(user_id=1), max_steps=4)
    outcome = await runtime.run(user_message="查一下", system="测试")

    assert outcome.stopped_reason == "final"
    # 失败的工具调用同样要留痕，否则排查时最需要那条恰好不在
    failed = [s for s in outcome.trace.spans() if s.kind == "tool" and not s.ok]
    assert failed, "参数校验失败应被记录为一条失败的 tool span"
    # 回喂给模型的内容里要包含「缺哪个字段」与下一步建议
    tool_msgs = [m for m in llm.seen_messages[1] if m.get("role") == "tool"]
    assert tool_msgs and "参数校验失败" in tool_msgs[0]["content"]


# ==========================================================================
# Agent 层：与对外契约的衔接 + 降级链
# ==========================================================================
async def test_react_agent_returns_proposals_through_the_same_contract(mock_client):
    """对外契约不变：调用方不需要知道当前是哪种执行模式。"""
    agent = ReActAgent(mock_client, store=SessionStore())
    resp = await agent.ainvoke(ChatRequest(message=BOOKING_MESSAGE, user_id=2))

    assert resp.stage == "react_final"
    assert resp.proposals, "ReAct 路径也应把结构化 proposals 带出来"
    assert not resp.degraded
    # 意图在 ReAct 模式下没有中间产物，如实留空而不是硬猜
    assert resp.intent is None
    nodes = [t.node for t in resp.trace]
    assert any(n.startswith("tool:query_availability") for n in nodes)
    assert any(n.startswith("llm:") for n in nodes)
    assert any(n.startswith("decision:") for n in nodes)


async def test_react_agent_keeps_multiturn_history(mock_client):
    """多轮上下文：历史被存下来，并在下一次调用时进入 prompt。"""
    store = SessionStore()
    agent = ReActAgent(mock_client, store=store)
    await agent.ainvoke(ChatRequest(message=BOOKING_MESSAGE, user_id=2, session_id="s1"))

    turns = store.history("s1")
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["content"] == BOOKING_MESSAGE


async def test_unconverged_react_falls_back_to_deterministic(isolated_db, catalog):
    """降级链的第一跳：react 跑不动 → 确定性编排接手，用户看不到报错。

    这是「保障线上运行稳定性」的落点。没有它，一次模型端点不支持工具调用
    就会变成一次用户可见的失败。
    """
    llm = _ToolsUnsupportedLLM(catalog)
    store = SessionStore()
    agent = ReActAgent(
        llm, store=store, fallback=LabBookingAgent(llm, store=store)
    )
    resp = await agent.ainvoke(ChatRequest(message=BOOKING_MESSAGE, user_id=2))

    # 降级必须在 trace 里留痕，否则线上没人知道这条回复是退回来的
    assert resp.trace[0].node == "degrade"
    assert "退回确定性编排" in resp.trace[0].detail
    # 确定性路径照常给出方案
    assert resp.proposals
    assert not resp.degraded


async def test_unconverged_react_says_so_when_there_is_no_fallback():
    """没有回退可用时，如实告知并给可照做的下一步，而不是静默给空回复。"""
    llm = _ToolsUnsupportedLLM([])
    agent = ReActAgent(llm, store=SessionStore(), fallback=None)
    resp = await agent.ainvoke(ChatRequest(message=BOOKING_MESSAGE, user_id=2))
    assert resp.degraded
    assert resp.stage == "degraded"


async def test_max_steps_gets_a_usable_reply():
    """步数耗尽的文案要能照做，不能只说「失败了」。"""
    always_call = TurnResult(tool_calls=[ToolCall(id="x", name="不存在的工具", arguments={})])
    llm = _ScriptedLLM([always_call] * 10)
    agent = ReActAgent(llm, store=SessionStore(), fallback=None)
    resp = await agent.ainvoke(ChatRequest(message="随便", user_id=1))
    assert resp.reply == MAX_STEPS_REPLY
    assert "日期" in resp.reply


async def test_missing_identity_is_rejected(mock_client):
    """身份缺失 fail-closed（与确定性路径同一条不变量）。"""
    agent = ReActAgent(mock_client, store=SessionStore())
    with pytest.raises(ValueError, match="user_id"):
        await agent.ainvoke(ChatRequest(message=BOOKING_MESSAGE, user_id=None))


async def test_no_client_degrades_before_doing_anything():
    agent = ReActAgent(None, store=SessionStore())
    resp = await agent.ainvoke(ChatRequest(message=BOOKING_MESSAGE, user_id=1))
    assert resp.degraded
    assert resp.trace[0].node == "degrade"


async def test_live_tool_result_survives_a_starving_budget(mock_client):
    """预算不够时，最先被牺牲的绝不是「这一轮刚拿到的工具结果」。

    这里是**真实踩过的坑**：早期实现把工具层整体按 token 裁剪，预算一小
    连最新那条也丢了。模型看不到结果，只看见自己上一句的问题，于是把同一个
    工具再调一遍 —— 直到步数耗尽，用户拿到一句"没查清"。
    裁剪策略直接决定了 ReAct 能不能收敛，这不是性能调优，是正确性。

    这条用的 120 预算比系统提示词（约 230 token）还小，是最极端的 starving 情形：
    要求仍然收敛，**并且**如实把「超预算」标出来 —— 不假装预算够用。
    """
    settings = Settings(app_mode="mock", context_max_tokens=120, react_max_steps=4)
    agent = ReActAgent(mock_client, store=SessionStore(), settings=settings)
    resp = await agent.ainvoke(ChatRequest(message=BOOKING_MESSAGE, user_id=2))

    assert resp.stage == "react_final", f"未收敛，步数被耗尽了：{resp.trace}"
    decisions = [t for t in resp.trace if t.node.startswith("decision:")]
    assert decisions and all("token" in t.detail for t in decisions)
    assert any("超预算" in t.detail for t in decisions), "装不下就该报出来"


async def test_a_sane_budget_is_not_flagged_as_over_budget(mock_client):
    """预算够用时不该报超限 —— 否则「超预算」这个信号会被自己喊成噪声。"""
    settings = Settings(app_mode="mock", context_max_tokens=2000, react_max_steps=4)
    agent = ReActAgent(mock_client, store=SessionStore(), settings=settings)
    resp = await agent.ainvoke(ChatRequest(message=BOOKING_MESSAGE, user_id=2))

    assert resp.stage == "react_final"
    decisions = [t for t in resp.trace if t.node.startswith("decision:")]
    assert decisions and all("token" in t.detail for t in decisions)
    assert not any("超预算" in t.detail for t in decisions)


def test_react_runtime_default_context_is_bounded():
    """默认构造也要有预算，不能只在显式传参时才生效。"""
    runtime = ReActRuntime(llm=_ScriptedLLM([]), registry=build_registry(user_id=1))
    assert isinstance(runtime.context, ContextBuilder)
    assert runtime.context.max_tokens > 0
