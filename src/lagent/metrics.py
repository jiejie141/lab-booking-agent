"""进程内指标注册表 + Prometheus 文本格式输出（P1-4）。

## 为什么还需要它 —— 日志已经能串起一次请求了

`request_id`（P1-3）回答的是「**这一次**请求发生了什么」，它答不了跨请求的问题：

    最近 5 分钟的 p95 是多少？错误率涨了没？限流拒了几次？预约冲突率是不是变高了？

这不是「日志再多记一点」能解决的。两者回答的是不同的问题，且都不可替代：

| | 日志 | 指标 |
|---|---|---|
| 粒度 | 逐条、精确到某一次请求 | 聚合（按标签分组） |
| 取值 | 可以无界（真实 path、user_id） | **必须有界**（否则打爆自己） |
| 用途 | **下钻**：拿着 id 看这次做了什么 | **发现**：先告诉我哪儿不对，再下钻 |
| 存储 | 落盘 / 收集到日志系统 | 由抓取端（Prometheus）拉走聚合 |

所以本项目两份都要：指标负责「**发现异常**」，日志负责「**解释异常**」。

## 七个刻意的设计（每一条都有它防的那个具体事故）

### 1. 不引 `prometheus_client`，自己写

与手写 BM25 / 手写 JWT / 手写滑动窗口同一条线：核心只是「计数器 + 桶 + 一段有公开
规范的文本格式」，加起来两百行且完全可测。

**代价要写清楚**：拿不到 exemplar（把 trace 挂在桶上）、native histogram、
多进程模式（`PROMETHEUS_MULTIPROC_DIR`）这些能力。日后真要，替换的是本模块的
注册表与 `render()`，**调用点一行不动** —— 与 `obs.py` 对待 structlog 的取舍一致。

### 2. 注册表是**进程级单例**（和限流器刻意相反）

限流器挂在 `app.state` 上，一实例一份 —— 那是为了让测试之间不互相吃掉配额。
指标不能这样：`/metrics` 的语义就是「**这个进程**从启动到现在一共做了什么」，
按应用实例分份，抓取端拿到的就不是完整数字。所以这里用模块级单例，
**代价是测试要自己清理**（见 `tests/test_metrics.py` 的 autouse fixture，
以及 P1-3 学到的那条：测试自己写的进程级状态必须自己清）。

也正因为指标是进程级的，**本地跑个 CLI 打印指标永远是空的**（那是另一个进程、
一次请求都没处理过）。所以本项目不提供 `main.py metrics` 子命令 ——
一个注定打印空表的命令比没有更误导人。指标只能由抓取端来聚合。

### 3. `threading.Lock`，不是 `asyncio.Lock`

`observe` 会从**三个地方**被调用：事件循环里的请求、FastAPI 丢进线程池的同步端点、
后台清扫任务。`asyncio.Lock` 在同步代码里根本无法 `await` 获取，
而且会把锁绑死在某一个事件循环上。指标写入必须能在任何上下文里安全发生。

### 4. 路由标签用**路由模板**，不用真实 path ★

真实 path 是**客户端可控**的：`/api/labs/1`、`/api/labs/2`…… 每个 id 长出一条新的
时间序列。这就是「上完监控，监控把进程打爆」的经典事故 —— 一个把可观测性做成了
故障源的死法。路由模板（`/api/labs/{id}`）的取值集合 = 路由条数，天然有界。

实测（`api.py` 的 `_route_label`）：FastAPI 会在路由匹配时把命中的 route 写回
**同一个** `scope`，所以最外层中间件在响应阶段读得到模板；没匹配上（404 / 被体积
校验在进路由之前拦下的 413）则读不到，统一落到 `__unmatched__`。

> 这里有个诚实的缺口：被 413 挡下的请求**没有**路由模板，所以「哪个接口被拦了」
> 在指标里答不出来（只能看到 `__unmatched__{status="413"}`）。
> 那要靠**日志**——访问日志里记的是这次请求**真实的** path。
> 这恰好是"两个通道都要有"的一个具体例子：指标给聚合，日志给细节。

### 5. 序列数硬上限：超限**丢弃并计数**

路由模板已经把基数封顶了，但那个封顶依赖「每个埋点都规规矩矩用模板」。
再加一道兜底：单个指标的不同标签组合超过上限就丢弃，并把它计进
`lagent_metrics_dropped_series_total`。

宁可少几个序列（并让人看得见少了），也不要让监控组件自己变成故障源。
注意丢弃是**静默错**的一类典型 —— 所以它必须有个可被查询的数字，
否则「指标少了」和「没发生」长得一模一样。

### 6. 时间戳用 `time.time()`，耗时用 `time.perf_counter()`

Prometheus 导出的时间戳是 **unix 纪元秒**。用 `monotonic()` 去填它，
会得到一个 1970 年附近、或者比现在早几十年的数字 —— 而这类错误的形态是
「告警一直响/一直不响」，排查时几乎没人会先怀疑时钟源。
反过来量耗时绝不能用 `time.time()`：墙钟会被 NTP 校正拨动，量出来甚至可能是负数。

### 7. 桶的宽度 = 分位数精度的上限

Prometheus **不存原始值**，它只存「每个桶里有多少个」。所以 p95 是
`histogram_quantile()` 从桶边界**插值**出来的，桶有多宽，误差就有多大。
因此桶不能抄一份默认值，要围绕「我们实际要看的那个量级」布局：

* HTTP：5ms ~ 30s。慢接口（真实模型 5–30 秒）与快接口（查库 <10ms）差三个数量级，
  低处密一点才有分辨力；
* 模型调用：0.25s ~ 32s，按 2 倍递增。延迟是**乘性**的量，等宽桶在低处浪费、
  在高处不够用，等比桶让相对误差恒定；
* 清扫任务：10ms ~ 60s。

`tests/test_metrics.py` 里有一条用例把「用这些桶算出来的 p95」和「真实 p95」
摆在一起比，误差写在 README 里 —— 而不是含糊地说「支持分位数」。
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import math
import re
import threading
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import TypeVar

__all__ = [
    "BUCKETS_HTTP",
    "BUCKETS_LLM",
    "BUCKETS_SWEEP",
    "REGISTRY",
    "Counter",
    "Gauge",
    "Histogram",
    "Registry",
    "init_sweep_gauges",
    "is_finite_buckets",
    "record_booking_outcome",
    "record_booking_retry",
    "record_cancel_outcome",
    "record_http_request",
    "record_llm_retry",
    "record_rate_limited",
    "record_sweep_task",
    "render",
    "reset_metrics",
    "set_build_info",
    "track_llm_call",
]

# ---------------------------------------------------------------------------
# 校验与转义
# ---------------------------------------------------------------------------
# 名字与标签名的字符集是 Prometheus 的硬约定（文本格式规范）。不合法的话
# 抓取端会拒收**整个** scrape —— 连带其它指标一起丢，所以宁可在构造时就报错。
_NAME_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# 这些名字被 API 的形参占用了，拿它们当标签名会在调用时撞车。
# 在**构造时**就报错，而不是等某天某个调用点把标签值悄悄喂给了 amount。
_RESERVED_LABELS = frozenset({"amount", "buckets", "labels", "value"})

# 单个指标允许的不同标签组合数上限。见模块 docstring 第 5 条。
DEFAULT_SERIES_CAP = 600

# 桶布局，理由见模块 docstring 第 7 条。三套是**按本应用的真实延迟分布**定的，
# 不是抄默认值：
#   HTTP —— 静态/查库接口 1~20ms，但**登录接口**用 scrypt（生产参数下 ≈140ms），
#           真实模型对话 5~30s。所以 50~300ms 这一段必须密（登录就在那里），
#           而秒级以上只留几个粗桶。
#   LLM  —— 0.25s ~ 32s，中间按 ~1.6 倍递增，让 1~16s 这个"要看 SLO"的区间
#           相对误差控制在十几个百分点内。8→10→12→16 这几档是特意加的：
#           实测不加 10 的话，p95 落在 (8, 12] 时误差能到 13.7%；
#           加上之后同一批样本的最差误差降到个位数（见 test_metrics.py）。
#   SWEEP—— 平时几十毫秒，归档任务可能几十秒，跨度比前两者都小。
BUCKETS_HTTP = (
    0.002, 0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15,
    0.2, 0.3, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0,
)
BUCKETS_LLM = (
    0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0,
    6.0, 8.0, 10.0, 12.0, 16.0, 24.0, 32.0,
)
BUCKETS_SWEEP = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0, 60.0)


def _escape_label_value(value: str) -> str:
    """标签值的转义。**反斜杠必须先转** —— 否则会把后面转出来的反斜杠再转一遍。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _escape_help(text: str) -> str:
    """HELP 文本按规范只转义 ``\\`` 与换行（引号不用转）。"""
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _format_number(value: float) -> str:
    """整数写成整数（`3` 而不是 `3.0`）：抓取端两者都收，但人看着省事。"""
    if value == math.inf:
        return "+Inf"
    if value == -math.inf:
        return "-Inf"
    if math.isnan(value):
        return "NaN"
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _format_series(keys: Sequence[str], values: Sequence[str]) -> str:
    """拼标签集。

    **分隔符是 ``","``，不是 ``", "``**：文本格式的语法里逗号后面没有空白
    （见规范的 ``{ "," label_name "=" ... }``）。多一个空格人眼看不出差别，
    但严格解析器会拒收这一行 —— 而拒收的代价是**整条 scrape 作废**，
    不是"少一个数字"。这条同样是被 tests/test_metrics.py 那个独立校验器抓出来的。
    """
    if not keys:
        return ""
    inner = ",".join(
        f'{key}="{_escape_label_value(value)}"'
        for key, value in zip(keys, values, strict=True)
    )
    return "{" + inner + "}"


def is_finite_buckets(buckets: Sequence[float]) -> bool:
    """桶必须是严格递增的正有限数 —— 顺序错了（比如降序）会让分位数算出荒谬的值。"""
    return all(
        math.isfinite(b) and b > 0 and (index == 0 or b > buckets[index - 1])
        for index, b in enumerate(buckets)
    )


# ---------------------------------------------------------------------------
# 指标类型
# ---------------------------------------------------------------------------
@dataclass
class _Series:
    """一个直方图序列：每个**有限**桶的非累计计数（+Inf 就是 total）。"""

    counts: list[int] = field(default_factory=list)
    total: int = 0
    total_sum: float = 0.0


class _Metric:
    kind = ""

    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: Sequence[str],
        series_cap: int = DEFAULT_SERIES_CAP,
    ) -> None:
        if not _NAME_RE.match(name):
            raise ValueError(f"非法的指标名：{name!r}（只允许 [a-zA-Z_:][a-zA-Z0-9_:]*）")
        for label in label_names:
            if not _LABEL_RE.match(label):
                raise ValueError(f"非法的标签名：{label!r}")
            if label in _RESERVED_LABELS:
                raise ValueError(f"标签名 {label!r} 与 API 形参冲突，换一个")
        self.name = name
        self.help = help_text
        self.label_names = tuple(label_names)
        self._lock = threading.Lock()
        self._series_cap = series_cap
        self._dropped = 0

    # ---- 内部 ---------------------------------------------------------
    def _key(self, labels: dict[str, str]) -> tuple[str, ...]:
        """把标签字典规范化成定序元组，并拒收不认识的标签。

        多给一个标签就报错（而不是忽略）：忽略的话，调用点把 `status_code`
        写成 `status` 会**静默**产出一条新序列，而"多出来一个没人用的序列"
        在抓取端上很难被注意到。
        """
        unknown = set(labels) - set(self.label_names)
        if unknown:
            raise ValueError(
                f"{self.name} 不认识标签 {sorted(unknown)}（允许：{list(self.label_names)}）"
            )
        missing = [name for name in self.label_names if name not in labels]
        if missing:
            raise ValueError(f"{self.name} 缺少标签 {missing}")
        return tuple(str(labels[name]) for name in self.label_names)

    def _drop(self) -> None:
        """超限：丢弃这次观测，只留下「丢了多少」这个数字。**调用方需持锁。**"""
        self._dropped += 1

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def clear(self) -> None:
        with self._lock:
            self._dropped = 0
            self._clear_locked()

    def _clear_locked(self) -> None:
        """清空本指标的数据。**调用方需持锁。**"""

    def render(self) -> list[str]:
        raise NotImplementedError

    def snapshot(self) -> object:
        raise NotImplementedError


class Counter(_Metric):
    """只增不减。

    「只增」不是洁癖：抓取端算的是 `rate()`，一个会减小的计数器会让速率出现负值
    （进程重启导致的回退是另一回事，那由抓取端的 `resets()` 处理）。
    """

    kind = "counter"

    def __init__(
        self,
        name: str,
        help_text: str = "",
        label_names: Sequence[str] = (),
        series_cap: int = DEFAULT_SERIES_CAP,
    ) -> None:
        super().__init__(name, help_text, label_names, series_cap)
        self._values: dict[tuple[str, ...], float] = {}

    def _clear_locked(self) -> None:
        self._values.clear()

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        if amount < 0:
            raise ValueError(f"{self.name} 是计数器，不能减少（amount={amount}）")
        key = self._key(labels)
        with self._lock:
            if key not in self._values:
                if len(self._values) >= self._series_cap:
                    self._drop()
                    return
                self._values[key] = 0.0
            self._values[key] += amount

    def value(self, **labels: str) -> float:
        with self._lock:
            return self._values.get(self._key(labels), 0.0)

    def snapshot(self) -> dict[tuple[str, ...], float]:
        with self._lock:
            return dict(self._values)

    def render(self) -> list[str]:
        return _render_values(self, sorted(self.snapshot().items()))


class Gauge(_Metric):
    """可增可减可设值的瞬时量。"""

    kind = "gauge"

    def __init__(
        self,
        name: str,
        help_text: str = "",
        label_names: Sequence[str] = (),
        series_cap: int = DEFAULT_SERIES_CAP,
    ) -> None:
        super().__init__(name, help_text, label_names, series_cap)
        self._values: dict[tuple[str, ...], float] = {}

    def _clear_locked(self) -> None:
        self._values.clear()

    def _touch(self, key: tuple[str, ...]) -> float | None:
        """**调用方需持锁。** 返回 None 表示超限被丢弃。"""
        if key not in self._values:
            if len(self._values) >= self._series_cap:
                self._drop()
                return None
            self._values[key] = 0.0
        return self._values[key]

    def set(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            if self._touch(key) is None:
                return
            self._values[key] = float(value)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = self._key(labels)
        with self._lock:
            current = self._touch(key)
            if current is None:
                return
            self._values[key] = current + amount

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        self.inc(-amount, **labels)

    def value(self, **labels: str) -> float | None:
        with self._lock:
            return self._values.get(self._key(labels))

    def snapshot(self) -> dict[tuple[str, ...], float]:
        with self._lock:
            return dict(self._values)

    def render(self) -> list[str]:
        return _render_values(self, sorted(self.snapshot().items()))


class Histogram(_Metric):
    """累计桶（cumulative buckets）。

    存的是**每个有限桶的非累计计数**，渲染时再累加 —— 存累计值的话，
    每次观测都要回改后面所有桶，而且没法回头改桶的布局。
    """

    kind = "histogram"

    def __init__(
        self,
        name: str,
        help_text: str = "",
        label_names: Sequence[str] = (),
        buckets: Sequence[float] = BUCKETS_HTTP,
        series_cap: int = DEFAULT_SERIES_CAP,
    ) -> None:
        super().__init__(name, help_text, label_names, series_cap)
        if not is_finite_buckets(buckets):
            raise ValueError(f"{self.name} 的桶必须是严格递增的正有限数：{list(buckets)}")
        self._buckets = tuple(float(b) for b in buckets)
        self._values: dict[tuple[str, ...], _Series] = {}

    @property
    def buckets(self) -> tuple[float, ...]:
        return self._buckets

    def _clear_locked(self) -> None:
        self._values.clear()

    def observe(self, value: float, **labels: str) -> None:
        key = self._key(labels)
        index = bisect.bisect_left(self._buckets, float(value))
        with self._lock:
            series = self._values.get(key)
            if series is None:
                if len(self._values) >= self._series_cap:
                    self._drop()
                    return
                series = _Series(counts=[0] * len(self._buckets))
                self._values[key] = series
            if index < len(self._buckets):
                series.counts[index] += 1
            series.total += 1
            series.total_sum += float(value)

    def snapshot(self) -> dict[tuple[str, ...], _Series]:
        with self._lock:
            return {
                key: _Series(list(series.counts), series.total, series.total_sum)
                for key, series in self._values.items()
            }

    def render(self) -> list[str]:
        snapshot = self.snapshot()
        if not snapshot:
            return []
        lines = [
            f"# HELP {self.name} {_escape_help(self.help)}",
            f"# TYPE {self.name} histogram",
        ]
        for key in sorted(snapshot):
            series = snapshot[key]
            labels_text = _format_series(self.label_names, key)
            # ⚠️ `_sum` / `_count` 必须紧跟**指标名**，再跟标签：
            #     lagent_x_sum{label="v"} 1.0      ← 对
            #     lagent_x{label="v"}_sum 1.0      ← 错，解析器会直接拒收这一行
            # 这个错误靠肉眼看输出很难发现（两行长得几乎一样），
            # 所以 test_metrics.py 里写了一条**逐行按规范校验**的用例。
            #
            # 必须按桶**升序**输出，最后一条固定是 le="+Inf"（= 观测总数）。
            # 顺序错的话抓取端算 histogram_quantile 会得到荒谬的结果。
            cumulative = 0
            for bound, count in zip(self._buckets, series.counts, strict=True):
                cumulative += count
                bound_label = _format_number(bound)
                lines.append(
                    f"{self.name}_bucket"
                    f"{_format_series((*self.label_names, 'le'), (*key, bound_label))}"
                    f" {cumulative}"
                )
            inf_label = _format_series((*self.label_names, "le"), (*key, "+Inf"))
            lines.append(f"{self.name}_bucket{inf_label} {series.total}")
            lines.append(f"{self.name}_sum{labels_text} {_format_number(series.total_sum)}")
            lines.append(f"{self.name}_count{labels_text} {series.total}")
        return lines


def _render_values(
    metric: _Metric, items: list[tuple[tuple[str, ...], float]]
) -> list[str]:
    if not items:
        return []
    lines = [
        f"# HELP {metric.name} {_escape_help(metric.help)}",
        f"# TYPE {metric.name} {metric.kind}",
    ]
    for key, value in items:
        label_text = _format_series(metric.label_names, key)
        lines.append(f"{metric.name}{label_text} {_format_number(value)}")
    return lines


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
_M = TypeVar("_M", bound=_Metric)


class Registry:
    """指标集合与 Prometheus 文本格式（0.0.4）渲染。"""

    def __init__(self) -> None:
        self._metrics: list[_Metric] = []

    def _add(self, metric: _M) -> _M:
        self._metrics.append(metric)
        return metric

    def add(self, metric: _M) -> _M:
        """把**已经构造好**的指标登记进来（构造时能自己指定序列上限）。

        ``counter()`` / ``gauge()`` / ``histogram()`` 走的就是这里。
        公开它的理由和它是同一件事：一个注册表本来就该能接受外部造好的指标，
        否则"给某个指标单独调小上限"这种需求就只能去改私有属性。
        """
        return self._add(metric)

    def counter(self, name: str, help_text: str, label_names: Sequence[str] = ()) -> Counter:
        return self._add(Counter(name, help_text, label_names))

    def gauge(self, name: str, help_text: str, label_names: Sequence[str] = ()) -> Gauge:
        return self._add(Gauge(name, help_text, label_names))

    def histogram(
        self,
        name: str,
        help_text: str,
        label_names: Sequence[str] = (),
        buckets: Sequence[float] = BUCKETS_HTTP,
    ) -> Histogram:
        return self._add(Histogram(name, help_text, label_names, buckets))

    @property
    def metrics(self) -> list[_Metric]:
        return list(self._metrics)

    def clear(self) -> None:
        """把每个指标的数据清空（**保留对象的身份**）。

        刻意不是「换一个新 Registry」：那样模块级的指标对象（HTTP_REQUESTS 等）
        会绑在旧注册表上，于是 `render()` 变成空 —— 一个"重置之后什么都看不到"
        的静默错。清内容、不换对象，就没有这个坑。
        """
        for metric in self._metrics:
            metric.clear()

    def render(self) -> str:
        """渲染成 Prometheus 文本格式。

        末尾**必须**有换行：规范要求最后一行以 ``\\n`` 结束，
        少了它某些抓取端会丢最后一条。
        """
        chunks: list[str] = []
        for metric in self._metrics:
            chunks.extend(metric.render())
        dropped = sorted((m.name, m.dropped) for m in self._metrics if m.dropped)
        if dropped:
            chunks.append(
                "# HELP lagent_metrics_dropped_series_total "
                "因超出序列上限而被丢弃的标签组合数（>0 说明有埋点的标签基数失控）"
            )
            chunks.append("# TYPE lagent_metrics_dropped_series_total counter")
            for name, count in dropped:
                escaped = _escape_label_value(name)
                chunks.append(
                    f'lagent_metrics_dropped_series_total{{metric="{escaped}"}} {count}'
                )
        if not chunks:
            return ""
        return "\n".join(chunks) + "\n"


REGISTRY = Registry()

# ---- 元信息：把「这批指标是哪个版本、什么模式」钉住 ------------------------
# 排障时第一个要回答的问题是「指标变化的那个时刻，代码变了没有」。
BUILD_INFO = REGISTRY.gauge(
    "lagent_info",
    "构建与运行模式信息（恒为 1，值在标签里）",
    ("version", "app_mode", "retrieval_backend"),
)


def set_build_info(*, version: str, app_mode: str, retrieval_backend: str) -> None:
    BUILD_INFO.set(1, version=version, app_mode=app_mode, retrieval_backend=retrieval_backend)


# ---- HTTP ----------------------------------------------------------------
HTTP_REQUESTS = REGISTRY.counter(
    "lagent_http_requests_total",
    "HTTP 请求数（route 是路由模板而不是真实路径）",
    ("method", "route", "status"),
)
HTTP_DURATION = REGISTRY.histogram(
    "lagent_http_request_duration_seconds",
    "HTTP 请求耗时（含被边界拒绝、没进路由的请求）",
    ("method", "route"),
    BUCKETS_HTTP,
)
# 并发量按 method 分标签，但不按路由：请求开始时还不知道会命中哪条路由
# （路由是进了 router 才确定的）。而"进程还扛不扛得住"也本来就是个整体问题，
# 按 method 分是为了能看出"是不是某类请求在堆积"。
HTTP_IN_PROGRESS = REGISTRY.gauge(
    "lagent_http_in_progress", "正在处理中的 HTTP 请求数", ("method",)
)


def record_http_request(
    *, method: str, route: str, status: int, duration_seconds: float
) -> None:
    HTTP_REQUESTS.inc(method=method, route=route, status=str(status))
    HTTP_DURATION.observe(duration_seconds, method=method, route=route)


def http_in_progress_inc(method: str) -> None:
    HTTP_IN_PROGRESS.inc(1, method=method)


def http_in_progress_dec(method: str) -> None:
    HTTP_IN_PROGRESS.dec(1, method=method)


# ---- 限流 ----------------------------------------------------------------
RATE_LIMITED = REGISTRY.counter(
    "lagent_rate_limited_total", "被限流拒绝的请求数（按限流器分组）", ("limiter",)
)


def record_rate_limited(limiter: str) -> None:
    RATE_LIMITED.inc(limiter=limiter)


# ---- 预约 ----------------------------------------------------------------
# outcome 的取值刻意把两种「冲突」分开：
#   conflict  —— 复检时就已经被别人占了（用户看到的是"这坑没了"，真实业务冲突）
#   retry     —— 撞了占用格唯一索引才发现（并发争抢，说明系统在承压）
# 合成一个数字会丢掉方向：前者该改产品（多给备选），后者该扩容。
BOOKING_ATTEMPTS = REGISTRY.counter(
    "lagent_booking_attempts_total",
    "下单请求数（按最终结果分组；conflict=复检发现被占，exhausted=重试耗尽）",
    ("outcome",),
)
BOOKING_RETRIES = REGISTRY.counter(
    "lagent_booking_retries_total",
    "因撞上占用格唯一索引而重试的次数（并发争抢的强度）",
)
CANCEL_ATTEMPTS = REGISTRY.counter(
    "lagent_cancel_attempts_total", "取消请求数（按最终结果分组）", ("outcome",)
)


def record_booking_outcome(outcome: str) -> None:
    BOOKING_ATTEMPTS.inc(outcome=outcome)


def record_booking_retry() -> None:
    BOOKING_RETRIES.inc()


def record_cancel_outcome(outcome: str) -> None:
    CANCEL_ATTEMPTS.inc(outcome=outcome)


# ---- 审批（P1-5）----------------------------------------------------------
# 单独一个计数器，而不是复用 BOOKING_ATTEMPTS：那边衡量的是"用户想约，
# 约没约上"，这里是"管理员处理申请"。合并之后"驳回变多"和"冲突变多"
# 会挤在同一个数字里，而两者的处置完全不同（加设备 vs 加审批人）。
REVIEW_ATTEMPTS = REGISTRY.counter(
    "lagent_review_attempts_total", "审批处理数（按结果分组）", ("outcome",)
)


def record_review_outcome(outcome: str) -> None:
    REVIEW_ATTEMPTS.inc(outcome=outcome)


# ---- 模型调用 ------------------------------------------------------------
# 只统计**真实**模型客户端的调用。MockLLMClient 是本地确定性规则，
# 把它记进来会让「模型耗时」里混进一片 0.1ms 的假调用，那张图就再也没法看了。
LLM_CALLS = REGISTRY.counter(
    "lagent_llm_calls_total",
    "模型调用数（按操作与最终结果；cancelled=调用中途被取消/断开）",
    ("op", "outcome"),
)
LLM_RETRIES = REGISTRY.counter(
    "lagent_llm_retries_total", "模型调用失败后重发的额外 HTTP 次数", ("op",)
)
LLM_DURATION = REGISTRY.histogram(
    "lagent_llm_call_duration_seconds",
    "模型调用耗时（一次逻辑调用的总耗时，含其内部重试）",
    ("op",),
    BUCKETS_LLM,
)


@contextlib.asynccontextmanager
async def track_llm_call(op: str) -> AsyncIterator[None]:
    """量一次**逻辑**模型调用：包住内部的重试，结果分三档。

    分 ``cancelled`` 而不是一律算 ``error``：客户端中途断开和模型真的挂了
    完全不是一回事，混在一起会把「用户手速快」报成「模型不稳定」。
    """
    started = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except BaseException:
        # 其余（含 LLMError）都算 error。**一律原样抛出去** ——
        # 指标是旁观者，绝不能改变被观测代码的控制流。
        outcome = "error"
        raise
    finally:
        LLM_CALLS.inc(op=op, outcome=outcome)
        LLM_DURATION.observe(time.perf_counter() - started, op=op)


def record_llm_retry(op: str) -> None:
    LLM_RETRIES.inc(op=op)


# ---- 清扫（P1-2）---------------------------------------------------------
# `last_success` 是这里面最值钱的一个：P1-2 的整个论点是
# 「清扫挂掉不会让门禁失守，但绝不能**静默**挂掉」—— 这个 gauge 就是
# 「静默挂掉」唯一的机器可读证据。
# 告警表达式：time() - lagent_sweep_last_success_timestamp_seconds > 1800
SWEEP_RUNS = REGISTRY.counter(
    "lagent_sweep_runs_total", "清扫任务执行次数（按任务与结果分组）", ("task", "outcome")
)
SWEEP_PROCESSED = REGISTRY.counter(
    "lagent_sweep_processed_total", "清扫累计处理的行数（按任务）", ("task",)
)
SWEEP_DURATION = REGISTRY.histogram(
    "lagent_sweep_duration_seconds", "清扫任务单次耗时", ("task",), BUCKETS_SWEEP
)
SWEEP_LAST_SUCCESS = REGISTRY.gauge(
    "lagent_sweep_last_success_timestamp_seconds",
    "清扫任务最近一次成功的 unix 时间戳（从未成功过则为 0）",
    ("task",),
)


def init_sweep_gauges(tasks: Sequence[str]) -> None:
    """把从未跑过的任务显式置 0。

    有了这一步，「从未成功过」与「很久没成功过」可以共用**同一个**告警表达式
    （`time() - 0` 是个极大的数），不必再写 `absent()` 那条分支。
    """
    for name in tasks:
        SWEEP_LAST_SUCCESS.set(0, task=name)


def record_sweep_task(
    name: str, *, ok: bool, duration_seconds: float, processed: int = 0
) -> None:
    SWEEP_RUNS.inc(task=name, outcome="ok" if ok else "error")
    SWEEP_DURATION.observe(duration_seconds, task=name)
    if processed:
        SWEEP_PROCESSED.inc(processed, task=name)
    if ok:
        # ★ 必须用 time.time()：Prometheus 的时间戳是 unix 纪元秒。
        # 用 perf_counter/monotonic 会导出一个 1970 年附近的数
        # （见模块 docstring 第 6 条）。
        SWEEP_LAST_SUCCESS.set(time.time(), task=name)


# ---- 对外入口 ------------------------------------------------------------
def render() -> str:
    return REGISTRY.render()


def reset_metrics() -> None:
    """清空全部指标。**只给测试用** —— 理由见模块 docstring 第 2 条。

    不提供「清空某一个」：真实运行中任何"把指标清零"的动作都会让抓取端的
    计数器回退，在图上表现为一次假的进程重启。要么全清（测试，进程内没有
    抓取端），要么不动。
    """
    REGISTRY.clear()
