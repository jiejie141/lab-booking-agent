"""工具注册表：Agent 运行时里「工具」这件事的全部负责方。

在抽出这一层之前，工具契约散在两处：``TOOL_SPECS`` 是一份只给人看的清单，
真正的调用写在 graph.py 的节点里。于是三件事都无处安放：

    1. **参数校验** —— 模型给的参数对不对，没有统一的判定点；
    2. **结果截断** —— 一次检索返回几万字，直接塞回上下文会把预算吃光；
    3. **副作用管控** —— 「哪些工具允许模型自己触发」根本不是一个可表达的概念。

这里要特别说清 **第 3 点**，它是把大模型当系统组件时最容易出事的地方：

    让模型自由调用一个会写库的工具（下单、取消），等于把「误操作」
    降级成一次普通的工具调用 —— 模型没有后果意识，而这类错误对用户是真实损失。
    所以工具带 ``side_effect`` 标记，**默认不进入给模型的工具清单**
    （见 :meth:`ToolRegistry.openai_tools`）。有副作用的动作仍可由业务代码
    显式调用，只是模型看不见、也点不到它。

另有一条设计红线：**本模块（以及整个 harness 包）不 import 任何业务模块。**
依赖方向是「业务实现 harness 定义的接口」，不是反过来。
这条边界由 ``tests/test_harness.py`` 里的 AST 静态检查钉住 ——
分层如果只写在文档里，迟早会被一次「顺手 import」破掉。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

# 单次工具结果回喂给模型的默认字符上限。
# 取值理由：一次检索/协商的结构化结果通常在 1–3 KB；超过这个量级的多半是
# 「把整个库倒出来」而不是「回答问题」。截断时保留头部并留一个明确的截断标记 ——
# 模型必须知道自己看到的是节选，否则它会基于残缺内容接着编。
DEFAULT_RESULT_LIMIT = 1200


@dataclass
class ToolResult:
    """一次工具调用的结果。

    **失败也是一条数据，不是异常。** 这是本类的核心决定：
    模型给错参数是最常见的失败形态，而那种情况需要把「错在哪」回喂给它重试。
    若这里抛异常，重试循环就得在调用点再包一层 try —— 不如一次性建模成数据，
    让成功与失败在类型上是同一件事，调用方只有一条路径要写。

    ``payload`` 保留**未截断**的结构化对象给代码用，``for_model()`` 给出
    截断后的文本给模型看。两者分开是因为需求本来就不同：
    响应层要完整的 proposals，模型只需要知道自己拿到了什么。
    """

    name: str
    ok: bool
    payload: Any = None
    error: str = ""
    truncated: bool = False
    elapsed_ms: float = 0.0
    limit: int = DEFAULT_RESULT_LIMIT

    def for_model(self) -> str:
        """回喂给模型的表示：成功给 JSON，失败给可照做的错误说明。"""
        if not self.ok:
            return f"[工具调用失败] {self.error}"
        text = _stringify(self.payload)
        if len(text) <= self.limit:
            return text
        return text[: self.limit] + f"\n…（结果已截断，原始 {len(text)} 字符）"


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的完整契约：名字、说明、参数模型、实现、风险等级。"""

    name: str
    description: str
    params: type[BaseModel]
    handler: Callable[..., Awaitable[Any]]
    # 有副作用 = 会写库或改变外部状态。默认不暴露给模型，见模块 docstring。
    side_effect: bool = False
    result_limit: int = DEFAULT_RESULT_LIMIT

    def json_schema(self) -> dict[str, Any]:
        """转成 OpenAI function calling 的 tools 条目。

        直接复用 pydantic 的 ``model_json_schema()`` 而不手写字典：
        手写的 schema 会和参数模型漂移（改了字段忘了改 schema 是最常见的一种），
        而 pydantic 生成的一定与校验逻辑同源。

        但**不能原样发出去**：pydantic 会默认把参数模型的 docstring 提升成
        schema 顶层的 ``description``。那份 docstring 是写给维护者看的 ——
        里面有「默认不暴露给模型，见 harness/tools.py」这类内部结论，
        发出去既浪费 token，又可能把模型引向不该碰的东西。
        所以这里做一次清洗，见 :func:`_scrub_params_schema`。
        """
        schema = _scrub_params_schema(self.params.model_json_schema())
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": schema,
            },
        }


class ToolRegistry:
    """工具的动态装载点。

    「动态加载」在这里落到一个具体问题上：**新增一个业务工具需要改哪里？**
    答案必须是「只注册，不改 harness」。这条由
    ``test_registering_a_new_tool_needs_no_harness_change`` 钉住 ——
    它就是「可复用运行时」与「写死的工作流」之间的分界线。
    """

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            # 重名直接报错而非覆盖：静默覆盖会把「注册了但没生效」
            # 变成一场很难查的调试 —— 同名工具只应有一个实现。
            raise ValueError(f"工具 {spec.name} 重复注册")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def side_effect_names(self) -> list[str]:
        return sorted(n for n, s in self._specs.items() if s.side_effect)

    def specs(self) -> list[ToolSpec]:
        return [self._specs[n] for n in self.names()]

    def openai_tools(self, *, include_side_effects: bool = False) -> list[dict[str, Any]]:
        """给模型的工具清单。**默认剔除有副作用的工具**（安全默认值）。"""
        return [
            spec.json_schema()
            for spec in self._specs.values()
            if include_side_effects or not spec.side_effect
        ]

    async def invoke(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """校验参数并执行。任何失败都转成 ``ok=False`` 的 ToolResult。"""
        spec = self._specs.get(name)
        if spec is None:
            return ToolResult(
                name=name,
                ok=False,
                error=f"没有名为 {name} 的工具。可用工具：{', '.join(self.names()) or '（无）'}",
            )

        try:
            params = spec.params.model_validate(arguments)
        except ValidationError as exc:
            return ToolResult(
                name=name, ok=False, error=_format_validation(exc), limit=spec.result_limit
            )

        started = time.perf_counter()
        try:
            payload = await _maybe_await(spec.handler(**params.model_dump()))
        except Exception as exc:  # noqa: BLE001 - 工具异常必须归一成数据回喂，不能穿透
            return ToolResult(
                name=name,
                ok=False,
                error=f"工具执行抛错：{type(exc).__name__}: {exc}",
                elapsed_ms=_ms(started),
                limit=spec.result_limit,
            )

        return ToolResult(
            name=name,
            ok=True,
            payload=payload,
            truncated=len(_stringify(payload)) > spec.result_limit,
            elapsed_ms=_ms(started),
            limit=spec.result_limit,
        )


async def gather_tools(
    registry: ToolRegistry, calls: list[tuple[str, dict[str, Any]]]
) -> list[ToolResult]:
    """并行执行一批工具调用。

    模型可以在一次回复里请求多个工具（并行 tool calls），串行等会让
    「查空闲 + 查规范」这种互不依赖的组合白白叠加延迟。
    但**并行不等于没有顺序**：``asyncio.gather`` 保证返回顺序与入参一致，
    否则把结果拼回消息时 tool_call_id 会串位。
    """
    if not calls:
        return []
    return list(await asyncio.gather(*(registry.invoke(name, args) for name, args in calls)))


# --------------------------------------------------------------------------
# 内部工具函数
# --------------------------------------------------------------------------
def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _scrub_params_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """洗掉 schema 里「给模型看没有意义、甚至有害」的部分。

    删两类键，都是 pydantic 的默认行为带出来的副产物：

    - 顶层 ``title`` / ``description``：``title`` 是类名（``CreateReservationParams``），
      对模型零信息量；``description`` 来自 docstring，而那份 docstring 写的是
      **内部工程结论**（"有副作用"、"见 harness/tools.py"）—— 工具的用途已经由
      :attr:`ToolSpec.description` 单独给出，两者重复且口径可能不一致。
    - 每个属性下的 ``title``：``"title": "Equipment Id"`` 只是字段名的驼峰化，
      同样的信息 ``properties`` 的键已经给了，纯噪声。

    保留每个属性的 ``description``：那是刻意写给模型看的（单位、格式、取值范围）。
    """
    schema.pop("title", None)
    schema.pop("description", None)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for prop in properties.values():
            if isinstance(prop, dict):
                prop.pop("title", None)
    return schema


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _stringify(payload: Any) -> str:
    """把任意结果转成回喂用的文本。pydantic 模型走 JSON，其余兜底 str。"""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, BaseModel):
        return json.dumps(payload.model_dump(mode="json"), ensure_ascii=False)
    if isinstance(payload, list):
        items = [
            item.model_dump(mode="json") if isinstance(item, BaseModel) else item
            for item in payload
        ]
        return json.dumps(items, ensure_ascii=False, default=str)
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except TypeError:
        return str(payload)


def _format_validation(exc: ValidationError) -> str:
    """把 pydantic 的校验错误写成模型能照着改的一句话。

    不用 ``str(exc)``：那个多行格式里混着 URL 与类型元信息，对模型是噪声。
    这里只留「字段名 + 哪里不对」，并明确告诉它下一步做什么。
    """
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", ())) or "（根）"
        parts.append(f"{loc}: {err.get('msg', '不合法')}")
    return "参数校验失败 —— " + "；".join(parts) + "。请按 schema 修正后重试。"
