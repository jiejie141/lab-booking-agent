"""Agent Harness：把「运行时该管的事」从业务代码里抽出来。

存在的理由：在抽出这一层之前，上下文管理写在 graph.py 的节点里、
工具协议不存在于任何地方（``TOOL_SPECS`` 只是给人看的清单）、
执行模式写死成一种。结果是三样东西都无法复用、也无法单独测试。

抽出之后，业务层只提供「工具」与「策略」，其余由本包负责：

    tools.py    工具注册表：JSON Schema 生成 · 参数校验 · 结果截断 · 副作用管控
    context.py  上下文组装：分层 · token 预算 · 确定的超限裁剪策略
    runtime.py  执行模式：deterministic（条件边路由）│ react（模型选工具）
    trace.py    span 埋点：按操作粒度记时，并可按类别聚合

**设计红线：本包不 import 任何业务模块**（``lagent.domain`` / ``models`` /
``schemas`` / ``agent`` 都不行），只依赖标准库与 pydantic。
依赖方向必须是「业务实现 harness 定义的接口」，不是反过来 ——
否则这一层只是把耦合换了个目录名。这条边界由
``tests/test_harness.py`` 用 AST 静态检查钉住。

判断这一层是否真的抽对了，只需回答一个问题：
**新增一个业务工具，需要改 harness 吗？** 答案必须是否。
"""

from .context import ContextBuild, ContextBuilder, estimate_messages_tokens, estimate_tokens
from .runtime import (
    ExecutionMode,
    ReActOutcome,
    ReActRuntime,
    ToolCall,
    ToolCallingLLM,
    TurnResult,
)
from .tools import DEFAULT_RESULT_LIMIT, ToolRegistry, ToolResult, ToolSpec, gather_tools
from .trace import Span, SpanCollector, SpanKind

__all__ = [
    "DEFAULT_RESULT_LIMIT",
    "ContextBuild",
    "ContextBuilder",
    "ExecutionMode",
    "ReActOutcome",
    "ReActRuntime",
    "Span",
    "SpanCollector",
    "SpanKind",
    "ToolCall",
    "ToolCallingLLM",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "TurnResult",
    "estimate_messages_tokens",
    "estimate_tokens",
    "gather_tools",
]
