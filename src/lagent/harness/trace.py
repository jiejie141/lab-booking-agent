"""可观测：把「一次会话发生了什么」变成结构化数据。

抽出这一层之前，可观测只有 graph.py 里那个自研的 ``trace`` 列表 ——
它把「哪些节点被走到」记得很清楚（这一点做得不错），但回答不了另外两个问题：

    * **一次 LLM 调用花了多久？** 节点耗时不等于模型耗时，节点里还夹着
      查库与算法；只会看到「parse 节点 800ms」，不知道里面 700ms 是网络。
    * **慢在哪一类操作上？** 没有按 kind 的聚合，就只能一行行读时间戳。

所以这里把记录粒度从「节点」下沉到「操作」，并给每条记录打上 kind：
llm / tool / node / decision。:meth:`SpanCollector.summary` 直接回答第二个问题。

**与现有对外契约的关系**：本模块刻意不 import ``lagent.schemas.TraceStep`` ——
harness 不依赖业务侧任何模块（见 tools.py 的说明）。转换成 TraceStep 由业务层
（agent 层）负责，那边本来就知道控制台想要什么形状。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

SpanKind = Literal["llm", "tool", "node", "decision"]


@dataclass
class Span:
    """一次操作的记录。

    ``parent`` 为将来导出 OpenTelemetry span 预留：有了父子关系，
    一次会话就能还原成调用树（会话 → 节点 → LLM 调用/工具调用），
    而不是一串平铺的时间戳。当前不生成树，但字段先留着 ——
    事后补字段意味着要改所有构造点。
    """

    kind: SpanKind
    name: str
    elapsed_ms: float = 0.0
    detail: str = ""
    ok: bool = True
    parent: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "elapsed_ms": self.elapsed_ms,
            "detail": self.detail,
            "ok": self.ok,
        }


@dataclass
class SpanCollector:
    """收集一次会话内的所有 span。"""

    trace_id: str = ""
    _spans: list[Span] = field(default_factory=list)

    def record(
        self,
        kind: SpanKind,
        name: str,
        *,
        elapsed_ms: float = 0.0,
        detail: str = "",
        ok: bool = True,
    ) -> Span:
        span = Span(kind=kind, name=name, elapsed_ms=elapsed_ms, detail=detail, ok=ok)
        self._spans.append(span)
        return span

    @contextmanager
    def span(self, kind: SpanKind, name: str, *, detail: str = "") -> Iterator[Span]:
        """把一段代码计成一条 span，异常也会被记下来再抛出去。

        异常路径必须留痕：一次失败的工具调用如果什么都不记，
        可观测性就只剩「成功的那些」，排查时最需要的那条恰好不在。
        """
        span = Span(kind=kind, name=name, detail=detail)
        started = time.perf_counter()
        try:
            yield span
        except BaseException:
            span.ok = False
            span.elapsed_ms = _ms(started)
            self._spans.append(span)
            raise
        span.elapsed_ms = _ms(started)
        self._spans.append(span)

    def spans(self) -> list[Span]:
        return list(self._spans)

    def of_kind(self, kind: SpanKind) -> list[Span]:
        return [s for s in self._spans if s.kind == kind]

    def total_ms(self) -> float:
        """所有 span 的耗时之和。

        注意它**不等于**墙上时间：这里没有父子关系去重，
        嵌在节点里的 LLM/tool span 会被重复计入。它回答的是
        「各类操作各占多少」，不是「这次请求一共多久」。
        """
        return round(sum(s.elapsed_ms for s in self._spans), 2)

    def summary(self) -> dict[str, dict[str, float]]:
        """按 kind 聚合：条数、总耗时、最慢一条。

        这就是「慢在哪一类操作上」的答案，也是本模块存在的理由。
        """
        table: dict[str, dict[str, float]] = {}
        for span in self._spans:
            row = table.setdefault(span.kind, {"count": 0.0, "total_ms": 0.0, "max_ms": 0.0})
            row["count"] += 1
            row["total_ms"] = round(row["total_ms"] + span.elapsed_ms, 2)
            row["max_ms"] = max(row["max_ms"], span.elapsed_ms)
        return table

    def failures(self) -> list[Span]:
        return [s for s in self._spans if not s.ok]


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
