"""指标与健康分级（P1-4）。

## 这个文件在证什么

「加了 /metrics」本身不是可验收的成果 —— 随便拼几行文本也叫 /metrics。
真正要证的是五件事，缺任何一件这套东西在排障时都白给：

1. **输出的每一行都符合 Prometheus 文本格式规范。** 名字写错、``_sum`` 的位置
   写错、桶的顺序写错，抓取端的表现是**整条 scrape 被拒**（连带其它指标一起丢），
   而不是"少一个数字"。所以这里写了一个**独立实现的解析器**逐行校验 ——
   它故意不复用 ``metrics.py`` 里的拼接逻辑：复用的话，
   拼接时的那个 bug 会被原样复制到校验器里，两边一起错、于是看起来是对的。
   （这条不是假想：本文件第一次跑起来就抓到 ``_sum`` / ``_count`` 被拼到了标签
   后面 —— ``lagent_x{label="v"}_sum`` 是个解析器会直接拒收的形状。）
2. **标签基数有界。** 真实 path 是客户端可控的，拿它当标签，每个新 id 长出一条
   新的时间序列 —— 这就是"上完监控，监控把进程打爆"的经典死法。
3. **标签集合本身不会失控。** 路由模板是第一道闸，序列数上限是第二道；
   被丢弃的观测必须留下 ``lagent_metrics_dropped_series_total``，
   否则「丢了很多」和「根本没发生」在图上长得一模一样。
4. **分位数的精度是可量化的，不是"支持 p95"四个字。** Prometheus 不存原始值，
   p95 是从桶里插值出来的，桶有多宽误差就有多大 —— 所以这里拿已知分布
   把"算出来的 p95"和"真实 p95"对了一遍，误差写在 README 里。
5. **各层真的被埋上了。** 下单的两种冲突要分开、模型调用的取消不能算成失败、
   清扫的「最近一次成功」必须能用一条告警表达式覆盖 —— 每一条都有对应用例。
"""

from __future__ import annotations

import asyncio
import bisect
import datetime as dt
import json
import logging
import math
import random
import re
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

# 从 test_obs 借日志捕获工具：它里面有一条容易做错的细节
# （必须把**根**级别一起抬上去，否则 DEBUG 记录在产生处就被丢了），
# 抄一份出来只会把那个细节再踩一次。
from test_obs import captured_logs, json_lines

from lagent.agent.llm import LLMError, MockLLMClient, RealLLMClient
from lagent.clock import now_local
from lagent.config import Settings, reset_settings_cache
from lagent.db import session_scope
from lagent.domain import booking as booking_module
from lagent.domain.booking import cancel_reservation, create_reservation
from lagent.metrics import (
    BOOKING_ATTEMPTS,
    BOOKING_RETRIES,
    BUCKETS_HTTP,
    BUCKETS_LLM,
    BUILD_INFO,
    CANCEL_ATTEMPTS,
    HTTP_DURATION,
    HTTP_IN_PROGRESS,
    HTTP_REQUESTS,
    LLM_CALLS,
    LLM_DURATION,
    LLM_RETRIES,
    RATE_LIMITED,
    SWEEP_DURATION,
    SWEEP_LAST_SUCCESS,
    SWEEP_PROCESSED,
    SWEEP_RUNS,
    Counter,
    Gauge,
    Histogram,
    Registry,
    init_sweep_gauges,
    is_finite_buckets,
    record_http_request,
    render,
    reset_metrics,
    set_build_info,
    track_llm_call,
)
from lagent.models import Reservation
from lagent.obs import REQUEST_ID_HEADER
from lagent.ratelimit import SlidingWindowLimiter
from lagent.schemas import BookingOutcome
from lagent.sweep import SweepTask, run_once


# ==========================================================================
# 隔离：指标注册表是**进程级**单例
# ==========================================================================
@pytest.fixture(autouse=True)
def _clean_metrics() -> Iterator[None]:
    """每个用例前后清空指标与配置缓存。

    指标注册表刻意做成进程级单例（理由见 ``metrics.py`` 的 docstring 第 2 条：
    ``/metrics`` 的语义就是"这个进程一共做了什么"，按应用实例分份会让抓取端
    拿到不完整的数字）。**代价就是测试要自己清理** —— 与 ``conftest`` 里
    默认关掉后台清扫是同一条理由：跨用例共享的可变状态就是不确定性。

    顺带清配置缓存：本文件里有几条用例会临时改 ``LAB_METRICS_*`` 环境变量。
    """
    reset_metrics()
    yield
    reset_metrics()
    reset_settings_cache()


# ==========================================================================
# 一个独立的文本格式校验器（不复用被测代码的拼接逻辑）
# ==========================================================================
_METRIC_NAME = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
_LABEL_BODY = r'[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\\n]|\\.)*"'
_LABELS = rf"\{{{_LABEL_BODY}(?:,{_LABEL_BODY})*\}}"
_NUMBER = r"(?:[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|NaN|[+-]Inf)"

_HELP_RE = re.compile(rf"^# HELP {_METRIC_NAME} .*$")
_TYPE_RE = re.compile(rf"^# TYPE {_METRIC_NAME} (counter|gauge|histogram|summary|untyped)$")
_SAMPLE_RE = re.compile(rf"^(?P<name>{_METRIC_NAME})(?P<labels>{_LABELS})? (?P<value>{_NUMBER})$")
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
_ESCAPES = {"n": "\n", "\\": "\\", '"': '"'}


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(1)), value)


def assert_exposition_grammar(text: str) -> None:
    """逐行按规范校验。任何一行不合格就说明抓取端会拒收整条 scrape。"""
    assert text.endswith("\n"), "规范要求最后一行以换行结束"
    for line in text.splitlines():
        if line.startswith("# HELP "):
            assert _HELP_RE.match(line), f"HELP 行不合规：{line!r}"
        elif line.startswith("# TYPE "):
            assert _TYPE_RE.match(line), f"TYPE 行不合规：{line!r}"
        else:
            # 规范里只有 HELP / TYPE 两种注释行，别的 `#` 开头行是不允许的
            assert not line.startswith("#"), f"出现了规范里没有的注释行：{line!r}"
            assert _SAMPLE_RE.match(line), f"样例行不合规：{line!r}"


def parse_samples(text: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """把文本解析成 ``{(指标名, 标签): 值}``。

    **刻意独立实现**：校验的意义就在于它不是被测代码的复读机。
    """
    out: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        assert _SAMPLE_RE.match(line), f"样例行不合规：{line!r}"
        name = line.split("{", 1)[0].split(" ", 1)[0]
        value = float(line.rsplit(" ", 1)[1])
        labels: tuple[tuple[str, str], ...] = ()
        if "{" in line:
            start = line.index("{")
            end = line.index("}", start)
            labels = tuple(
                (match.group(1), _unescape(match.group(2)))
                for match in _LABEL_RE.finditer(line[start + 1 : end])
            )
        assert (name, labels) not in out, f"重复的序列：{name} {labels}"
        out[(name, labels)] = value
    return out


def as_exposition(metric: Counter | Gauge | Histogram) -> str:
    """把单个指标的渲染拼成完整文本。

    **末尾补换行** —— ``Registry.render()`` 就是这么做的（规范要求），
    而单独调 ``metric.render()`` 拿到的是行列表。忘了补这一行，
    校验器第一条断言就会挂，而挂的原因跟被验的对象毫无关系。
    """
    lines = metric.render()
    return "\n".join(lines) + "\n" if lines else ""


def bucket_lines(
    text: str, name: str, labels: tuple[tuple[str, str], ...]
) -> list[tuple[float, int]]:
    """取出某个直方图的 ``[(上界, 累计计数), ...]``，**保持输出顺序**。

    保持顺序是有意的：桶的顺序本身就是要断言的东西之一。
    """
    prefix = f"{name}_bucket{{" + ",".join(f'{key}="{value}"' for key, value in labels)
    found: list[tuple[float, int]] = []
    for line in text.splitlines():
        if not line.startswith(prefix):
            continue
        match = re.search(r'le="([^"]+)"', line)
        assert match is not None, line
        bound = math.inf if match.group(1) == "+Inf" else float(match.group(1))
        found.append((bound, int(float(line.rsplit(" ", 1)[1]))))
    return found


def histogram_quantile(q: float, buckets: list[tuple[float, int]]) -> float:
    """按 PromQL ``histogram_quantile`` 的**文档语义**从累计桶插值。

    ``buckets`` 是 ``[(上界, 累计计数)]``，含 ``+Inf`` 桶。
    写这一版是为了能拿它跟真实分位数对账 —— 断言"支持 p95"没有意义，
    断言"用这些桶算出来的 p95 与真值的偏差不超过 X%"才有。
    """
    total = buckets[-1][1]
    if total == 0:
        return math.nan
    rank = q * total
    lower_bound, lower_count = 0.0, 0
    for upper, cumulative in buckets:
        if cumulative >= rank:
            if math.isinf(upper):
                return lower_bound
            if cumulative == lower_count:
                return upper
            span = (rank - lower_count) / (cumulative - lower_count)
            return lower_bound + (upper - lower_bound) * span
        lower_bound, lower_count = upper, cumulative
    return lower_bound


# ==========================================================================
# 注册表本身
# ==========================================================================
class TestRegistryBasics:
    def test_counter_renders_help_type_and_value(self):
        registry = Registry()
        counter = registry.counter("demo_total", "一个演示计数器")
        counter.inc()
        counter.inc(2)
        text = registry.render()
        assert_exposition_grammar(text)
        assert "# HELP demo_total 一个演示计数器" in text
        assert "# TYPE demo_total counter" in text
        assert text.endswith("demo_total 3\n")

    def test_gauge_can_go_up_and_down(self):
        gauge = Gauge("demo_gauge", "演示")
        gauge.inc()
        gauge.inc(4)
        gauge.dec(2)
        gauge.set(7)
        assert gauge.value() == 7

    def test_counter_refuses_to_decrease(self):
        """一个会减小的计数器会让抓取端算出的速率变成负数。"""
        with pytest.raises(ValueError, match="不能减少"):
            Counter("demo_total").inc(-1)

    def test_metric_without_data_is_omitted_entirely(self):
        """一个从未被观测过的指标不该出现在输出里（连 HELP 行都不该有）。

        有 HELP/TYPE 却没有样本行，会让 `count(...)` 这类查询得到空结果，
        而空结果与"该指标不存在"在告警规则里是两回事。
        """
        registry = Registry()
        registry.counter("never_total", "从未被观测")
        assert registry.render() == ""

    def test_illegal_metric_name_is_rejected_at_construction(self):
        for bad in ("有中文", "1leading", "with space", "with-dash"):
            with pytest.raises(ValueError, match="非法的指标名"):
                Counter(bad)

    def test_illegal_label_name_is_rejected(self):
        with pytest.raises(ValueError, match="非法的标签名"):
            Counter("demo_total", "", ("bad-label",))

    def test_reserved_label_name_is_rejected(self):
        """``amount`` 之类的名字被 API 形参占了，用它会在调用时静默撞车。

        在**构造时**就报错，而不是等某天某个调用点把标签值喂给了 ``amount``。
        """
        with pytest.raises(ValueError, match="与 API 形参冲突"):
            Counter("demo_total", "", ("amount",))

    def test_unknown_label_is_rejected(self):
        counter = Counter("demo_total", "", ("route",))
        with pytest.raises(ValueError, match="不认识标签"):
            counter.inc(status="200")

    def test_missing_label_is_rejected(self):
        counter = Counter("demo_total", "", ("route", "status"))
        with pytest.raises(ValueError, match="缺少标签"):
            counter.inc(route="/api/labs")

    def test_series_are_sorted_in_the_output(self):
        """输出顺序稳定，diff 与断言才有意义。"""
        counter = Counter("demo_total", "", ("k",))
        for key in ("b", "a", "c"):
            counter.inc(k=key)
        lines = [line for line in counter.render() if not line.startswith("#")]
        assert lines == [
            'demo_total{k="a"} 1',
            'demo_total{k="b"} 1',
            'demo_total{k="c"} 1',
        ]


class TestConcurrency:
    def test_counter_survives_concurrent_increments(self):
        """★ 并发自增不能丢。

        指标写入会从三处发生：事件循环里的请求、FastAPI 丢进线程池的同步端点、
        后台清扫任务。所以这里用真线程去撞 —— 它同时也在钉"锁必须是
        ``threading.Lock`` 而不是 ``asyncio.Lock``"这个选型。
        """
        counter = Counter("demo_total")
        threads = 8
        per_thread = 500

        def worker() -> None:
            for _ in range(per_thread):
                counter.inc()

        with ThreadPoolExecutor(max_workers=threads) as pool:
            for future in [pool.submit(worker) for _ in range(threads)]:
                future.result()
        assert counter.value() == threads * per_thread

    def test_histogram_survives_concurrent_observations(self):
        histogram = Histogram("demo_seconds", "演示", buckets=(0.1, 1.0))

        def worker() -> None:
            for _ in range(200):
                histogram.observe(0.05)

        with ThreadPoolExecutor(max_workers=4) as pool:
            for future in [pool.submit(worker) for _ in range(4)]:
                future.result()
        series = histogram.snapshot()[()]
        assert series.total == 800
        assert series.counts == [800, 0]

    def test_no_deadlock_when_render_walks_metrics_being_written(self):
        """渲染与写入并发也不能卡住：抓取时的输出是"某个瞬间的快照"，不是事务。"""
        registry = Registry()
        for index in range(5):
            registry.counter(f"demo_{index}_total", "演示").inc()
        stop = threading.Event()

        def writer() -> None:
            while not stop.is_set():
                registry.metrics[0].inc()  # type: ignore[attr-defined]

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            for _ in range(50):
                assert_exposition_grammar(registry.render())
        finally:
            stop.set()
            thread.join(timeout=5)
        assert not thread.is_alive()


class TestSeriesCap:
    def test_series_cap_drops_and_counts(self):
        """★ 超限要**丢弃并计数**，不能无限长。

        路由模板已经把基数封顶了，但那个封顶依赖"每个埋点都守规矩"。
        这一道兜底防的是最坏情况：监控组件自己变成故障源。
        """
        counter = Counter("demo_total", "", ("k",), series_cap=3)
        for index in range(10):
            counter.inc(k=f"v{index}")
        assert len(counter.snapshot()) == 3
        assert counter.dropped == 7

    def test_dropped_series_show_up_as_a_queryable_number(self):
        """★ 「丢了很多」必须与「根本没发生」长得不一样。

        纯静默丢弃是这里最容易犯的错：图上少几条序列，没人会怀疑是监控自己丢的。
        """
        registry = Registry()
        registry.counter("quiet_total", "演示")
        assert "lagent_metrics_dropped_series_total" not in registry.render()

        loud = registry.add(Counter("loud_total", "演示", ("k",), series_cap=1))
        for index in range(4):
            loud.inc(k=str(index))
        text = registry.render()
        assert_exposition_grammar(text)
        assert 'lagent_metrics_dropped_series_total{metric="loud_total"} 3' in text

    def test_drop_does_not_lose_already_tracked_series(self):
        counter = Counter("demo_total", "", ("k",), series_cap=1)
        counter.inc(k="kept")
        counter.inc(k="kept")
        counter.inc(k="dropped")
        assert counter.value(k="kept") == 2


class TestReset:
    def test_reset_clears_values_and_keeps_objects_usable(self):
        """★ 重置必须**清内容**而不是换注册表。

        换注册表会让模块级的指标对象绑在旧注册表上，于是 render() 变成空 ——
        一个"重置之后什么都看不到"的静默错。
        """
        record_http_request(method="GET", route="/api/health", status=200, duration_seconds=0.01)
        assert HTTP_REQUESTS.value(method="GET", route="/api/health", status="200") == 1
        reset_metrics()
        assert HTTP_REQUESTS.value(method="GET", route="/api/health", status="200") == 0
        record_http_request(method="GET", route="/api/health", status=200, duration_seconds=0.01)
        assert 'method="GET"' in render()


# ==========================================================================
# 文本格式（0.0.4）
# ==========================================================================
class TestExpositionFormat:
    def test_histogram_sum_and_count_suffix_goes_on_the_name(self):
        """★ 回归用例：``_sum`` / ``_count`` 必须紧跟**指标名**，再跟标签。

        写错的样子是 ``lagent_x{label="v"}_sum 1.0`` —— 两行长得几乎一样，
        肉眼极难发现，而抓取端会直接拒收。第一版就是这么写的，
        靠"独立实现的解析器"这条用例抓出来的。
        """
        histogram = Histogram("demo_seconds", "演示", ("route",), buckets=(0.1, 1.0))
        histogram.observe(0.05, route="/a")
        text = as_exposition(histogram)
        assert_exposition_grammar(text)
        assert 'demo_seconds_sum{route="/a"}' in text
        assert 'demo_seconds_count{route="/a"}' in text
        assert "}_sum" not in text
        assert "}_count" not in text

    def test_labels_are_separated_by_a_bare_comma(self):
        """★ 回归用例：逗号后面**不能有空格**。

        文本格式的语法是 ``{ "," label_name "=" ... }``，没有空白。
        多一个空格人眼看不出来，严格解析器却会拒收整行 ——
        同样是那条独立校验器抓到的（第一版用的是 ``", ".join``）。
        """
        histogram = Histogram("demo_seconds", "演示", ("a", "b"), buckets=(1.0,))
        histogram.observe(0.1, a="1", b="2")
        text = as_exposition(histogram)
        assert_exposition_grammar(text)
        assert ', b="2"' not in text
        assert 'a="1",b="2"' in text

    def test_buckets_are_cumulative_and_end_with_inf(self):
        histogram = Histogram("demo_seconds", "演示", buckets=(0.1, 1.0))
        for value in (0.05, 0.5, 7.0):
            histogram.observe(value)
        text = as_exposition(histogram)
        assert_exposition_grammar(text)
        assert bucket_lines(text, "demo_seconds", ()) == [(0.1, 1), (1.0, 2), (math.inf, 3)]

    def test_bucket_order_is_ascending_and_inf_is_last(self):
        """顺序错的话 histogram_quantile 会算出荒谬的值。"""
        histogram = Histogram("demo_seconds", "演示", buckets=BUCKETS_HTTP)
        histogram.observe(0.03)
        text = as_exposition(histogram)
        bounds = [bound for bound, _ in bucket_lines(text, "demo_seconds", ())]
        assert bounds == [*BUCKETS_HTTP, math.inf]

    def test_observations_above_the_last_bucket_only_count_in_inf(self):
        histogram = Histogram("demo_seconds", "演示", buckets=(0.1, 1.0))
        histogram.observe(999.0)
        series = histogram.snapshot()[()]
        assert series.counts == [0, 0]
        assert series.total == 1

    def test_unsorted_buckets_are_rejected(self):
        with pytest.raises(ValueError, match="严格递增"):
            Histogram("demo_seconds", "演示", buckets=(1.0, 0.1))
        assert not is_finite_buckets((0.1, 0.1))
        assert not is_finite_buckets((0.1, math.inf))

    def test_label_values_are_escaped(self):
        """标签值里的引号/反斜杠/换行必须转义 —— 否则整行都解析不了。"""
        counter = Counter("demo_total", "", ("note",))
        counter.inc(note='a"b\\c\nd')
        text = as_exposition(counter)
        assert_exposition_grammar(text)
        assert next(iter(parse_samples(text)))[1] == (("note", 'a"b\\c\nd'),)

    def test_backslash_is_escaped_before_the_quote(self):
        """反斜杠必须先转：顺序反了会把转出来的反斜杠再转一遍。"""
        counter = Counter("demo_total", "", ("note",))
        counter.inc(note="\\")
        assert 'note="\\\\"' in counter.render()[-1]

    def test_help_text_newline_is_escaped(self):
        counter = Counter("demo_total", "第一行\n第二行")
        counter.inc()
        text = as_exposition(counter)
        assert_exposition_grammar(text)
        assert "# HELP demo_total 第一行\\n第二行" in text

    def test_a_metric_with_no_data_renders_no_lines_at_all(self):
        """没被观测过就什么都不输出 —— 连 HELP 行都不该有。

        有 HELP/TYPE 却没有样本行会让 `count(...)` 得到空结果，
        而"空结果"与"该指标不存在"在告警规则里是两回事。
        """
        assert Counter("demo_total", "从未观测").render() == []

    def test_render_is_stable_across_calls(self):
        counter = Counter("demo_total", "", ("k",))
        for key in ("x", "y"):
            counter.inc(k=key)
        assert counter.render() == counter.render()


def _lognormal(median: float, sigma: float, seed: int, count: int = 2000) -> list[float]:
    """确定性的对数正态样本 —— 延迟本来就近似对数正态（乘性抖动 + 长尾）。"""
    rng = random.Random(seed)
    return [median * math.exp(rng.gauss(0, sigma)) for _ in range(count)]


def _mixed_http_load(seed: int, count: int = 4000) -> list[float]:
    """本应用真实形状的混合负载。

    80% 快请求（静态/查库）+ 14% 中等查询 + 5% 登录（scrypt ≈140ms）+ 1% 慢请求。
    刻意**不是**单一分布：真实流量就是几个不同量级的成分叠在一起，
    而分位数恰恰在多峰分布上最容易被插值算歪。
    """
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(count):
        roll = rng.random()
        if roll < 0.80:
            values.append(0.003 + rng.random() * 0.02)
        elif roll < 0.94:
            values.append(0.03 + rng.random() * 0.09)
        elif roll < 0.99:
            values.append(0.14 + rng.gauss(0, 0.01))
        else:
            values.append(0.6 + rng.random() * 1.6)
    return values


class TestQuantileAccuracy:
    """★ 桶的布局决定 p95 的精度上限 —— 这里把它量出来，而不是嘴上说"支持分位数"。"""

    def _estimate(self, values: list[float], buckets: tuple[float, ...], q: float) -> float:
        histogram = Histogram("demo_seconds", "演示", buckets=buckets)
        for value in values:
            histogram.observe(value)
        text = as_exposition(histogram)
        return histogram_quantile(q, bucket_lines(text, "demo_seconds", ()))

    def test_the_estimate_never_leaves_the_bucket_holding_the_true_quantile(self):
        """★ 这是**唯一可证**的保证，也正是必须说清的边界。

        Prometheus 不存原始值，p95 是插值出来的，不是算出来的：它只保证落在
        "真正包含 p95 的那个桶"里。桶有多宽，误差的硬上界就有多大 ——
        所以断言"落在区间内"，而不是断言"等于真值"（后者是假的）。
        """
        values = _mixed_http_load(seed=1)
        for q in (0.5, 0.9, 0.95, 0.99):
            estimate = self._estimate(values, BUCKETS_HTTP, q)
            truth = sorted(values)[int(q * len(values)) - 1]
            index = bisect.bisect_left(BUCKETS_HTTP, truth)
            lower = BUCKETS_HTTP[index - 1] if index else 0.0
            upper = BUCKETS_HTTP[index] if index < len(BUCKETS_HTTP) else math.inf
            assert lower <= estimate <= upper, f"q={q} 的估计跑到了桶外面：{estimate}"

    def test_p95_error_on_this_apps_real_latency_profile(self):
        """在真实形状的负载上，p95 的偏差有多大 —— 结论是可引用的数字。

        实测（三个随机种子）：**0.1% ~ 2.7%**。这条用例把 5% 当上限钉住：
        桶布局一改，这里就会红，README 里那句话也就该跟着改。
        """
        for seed in (1, 5, 9):
            values = _mixed_http_load(seed=seed)
            estimate = self._estimate(values, BUCKETS_HTTP, 0.95)
            truth = sorted(values)[int(0.95 * len(values)) - 1]
            relative = abs(estimate - truth) / truth
            assert relative < 0.05, f"seed={seed} 的 p95 偏差 {relative:.2%}"

    def test_llm_p95_error_in_the_slo_band(self):
        """1~16 秒是"该不该告警"的判断区间。

        实测：加了 10 这一档之后，median=6s / sigma=0.5 的样本上偏差约 3~6%；
        上限钉在 10%。不加那一档时同一样本能到 13.7%（见 BUCKETS_LLM 的注释）。
        """
        for seed in (3, 17, 29):
            values = _lognormal(median=6.0, sigma=0.5, seed=seed)
            estimate = self._estimate(values, BUCKETS_LLM, 0.95)
            truth = sorted(values)[int(0.95 * len(values)) - 1]
            assert abs(estimate - truth) / truth < 0.10, f"seed={seed}"

    def test_a_tight_distribution_is_the_worst_case(self):
        """分布越集中，同一个桶宽造成的**相对**误差越大 —— 这条要如实承认。

        与桶的绝对宽度无关，这是分位数桶的固有性质：桶宽相对值越小，
        误差越大。所以「p95 是插值出来的」这句话在报告里必须带上量级。
        """
        tight = _lognormal(median=0.02, sigma=0.05, seed=7)
        estimate = self._estimate(tight, BUCKETS_HTTP, 0.95)
        truth = sorted(tight)[int(0.95 * len(tight)) - 1]
        relative = abs(estimate - truth) / truth
        # 落在 (0.025, 0.05] 里，桶宽 25ms 而真值才 ~22ms —— 相对误差可以很大
        assert relative < 1.0
        assert relative > 0.05, "如果这里变准了，说明桶更密了，README 的数字要改"

    def test_empty_histogram_has_no_quantile(self):
        assert math.isnan(histogram_quantile(0.95, [(1.0, 0), (math.inf, 0)]))


# ==========================================================================
# HTTP 埋点
# ==========================================================================
class TestHttpInstrumentation:
    async def test_request_is_counted_under_the_route_template(self, http):
        await http.get("/api/health")
        assert HTTP_REQUESTS.value(method="GET", route="/api/health", status="200") == 1

    async def test_status_codes_are_separate_series(self, http):
        await http.get("/api/health")
        await http.get("/api/auth/me")  # 未登录 → 401
        assert HTTP_REQUESTS.value(method="GET", route="/api/health", status="200") == 1
        assert HTTP_REQUESTS.value(method="GET", route="/api/auth/me", status="401") == 1

    async def test_unmatched_paths_collapse_into_one_series(self, http):
        """★ 基数防线：真实 path 是客户端可控的，绝不能进标签。

        十个不存在的路径必须只产出**一条**序列。否则一个扫描器随手就能把
        时间序列数量顶到几万条 —— 监控把自己拖死。
        """
        for index in range(10):
            resp = await http.get(f"/api/no-such-thing/{index}")
            assert resp.status_code == 404
        assert HTTP_REQUESTS.value(method="GET", route="__unmatched__", status="404") == 10
        assert {key for key in HTTP_REQUESTS.snapshot() if key[1] == "__unmatched__"} == {
            ("GET", "__unmatched__", "404")
        }

    async def test_boundary_rejected_request_is_counted_but_unmatched(self, http, as_user):
        """413 是在读 body 之前被挡下的，根本没进路由。

        所以它在指标里只能落到 ``__unmatched__``（拿不到路由模板）——
        这是个**诚实的缺口**：「哪个接口被体积上限拦了」要靠日志回答
        （访问日志里记的是真实 path）。两条通道各自不可替代，这就是一个例子。
        """
        resp = await http.post(
            "/api/agent/chat",
            content=json.dumps({"message": "x" * 200_000}).encode("utf-8"),
            headers={"Content-Type": "application/json", **(await as_user("李娜"))},
        )
        assert resp.status_code == 413
        assert HTTP_REQUESTS.value(method="POST", route="__unmatched__", status="413") == 1

    async def test_in_progress_returns_to_zero_even_on_a_rejection(self, http, as_user):
        """异常/拒绝路径上少减一次，这个 gauge 就会一路往上爬，读起来像"服务卡住了"。"""
        await http.get("/api/health")
        await http.post(
            "/api/agent/chat",
            content=json.dumps({"message": "x" * 200_000}).encode("utf-8"),
            headers={"Content-Type": "application/json", **(await as_user("李娜"))},
        )
        assert HTTP_IN_PROGRESS.value(method="GET") == 0
        assert HTTP_IN_PROGRESS.value(method="POST") == 0

    async def test_the_scrape_itself_is_not_counted(self, http, as_user):
        """★ Prometheus 每 15 秒来一次，而业务可能一分钟才几次。

        把抓取记进 QPS，那条曲线反映的就只是抓取频率 —— 指标直接废掉。
        （探针请求**记**，因为它的状态码是有用的信号：探针在失败应该看得见。）
        """
        headers = await as_user("管理员")
        assert (await http.get("/metrics", headers=headers)).status_code == 200
        assert HTTP_REQUESTS.value(method="GET", route="/metrics", status="200") == 0

    async def test_duration_is_recorded_in_seconds_not_milliseconds(self, http):
        """单位记错的表现很隐蔽：秒级的桶全部落空，图上变成"所有请求都很慢"。"""
        await http.get("/api/health")
        observed = HTTP_DURATION.snapshot()[("GET", "/api/health")]
        assert observed.total == 1
        assert observed.total_sum < 1.0


# ==========================================================================
# /metrics 端点
# ==========================================================================
class TestMetricsEndpoint:
    async def test_requires_a_token(self, http):
        resp = await http.get("/metrics")
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == "Bearer"

    async def test_admin_token_is_accepted(self, http, as_user):
        resp = await http.get("/metrics", headers=await as_user("管理员"))
        assert resp.status_code == 200
        assert "lagent_http_requests_total" in resp.text

    async def test_non_admin_token_is_rejected(self, http, as_user):
        resp = await http.get("/metrics", headers=await as_user("张伟"))
        assert resp.status_code == 401

    async def test_shared_key_is_accepted_and_a_wrong_one_is_not(self, http, monkeypatch):
        monkeypatch.setenv("LAB_METRICS_API_KEY", "scrape-key-1")
        reset_settings_cache()
        assert (await http.get("/metrics", headers={"X-Metrics-Key": "scrape-key-1"})).status_code == 200
        assert (await http.get("/metrics", headers={"X-Metrics-Key": "wrong"})).status_code == 401
        assert (await http.get("/metrics")).status_code == 401

    async def test_content_type_is_the_prometheus_text_format(self, http, as_user):
        resp = await http.get("/metrics", headers=await as_user("管理员"))
        assert resp.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"

    async def test_output_follows_the_exposition_grammar(self, http, as_user):
        headers = await as_user("管理员")
        await http.get("/api/health")
        await http.get("/api/health/ready")
        resp = await http.get("/metrics", headers=headers)
        assert_exposition_grammar(resp.text)
        samples = parse_samples(resp.text)
        assert any(name == "lagent_info" for name, _ in samples)

    async def test_response_carries_a_request_id(self, http, as_user):
        """抓取失败时，运维要能把这次抓取和日志对上。"""
        resp = await http.get("/metrics", headers=await as_user("管理员"))
        assert resp.headers.get(REQUEST_ID_HEADER)

    async def test_no_store_header_prevents_a_cached_scrape(self, http, as_user):
        resp = await http.get("/metrics", headers=await as_user("管理员"))
        assert resp.headers["cache-control"] == "no-store"

    async def test_disabled_endpoint_is_404_not_an_empty_table(self, http, monkeypatch):
        """★ 「功能关掉了」与「开着但一条数据都没有」必须分得开。

        返回 200 + 空表的话，抓取端打到了错的服务上也没人发现 ——
        监控会安静地画出一条空线，而"没有数据"和"没有请求"长得一样。
        """
        monkeypatch.setenv("LAB_METRICS_ENABLED", "false")
        reset_settings_cache()
        assert (await http.get("/metrics", headers={"Authorization": "Bearer x"})).status_code == 404

    async def test_not_listed_in_the_openapi_schema(self, http):
        assert "/metrics" not in (await http.get("/openapi.json")).json()["paths"]

    async def test_scrape_is_demoted_to_debug_in_the_access_log(self, http, as_user):
        """与探针同一个理由：按固定节奏打的请求不该把业务日志冲掉。"""
        headers = await as_user("管理员")
        before = await http.get("/metrics", headers=headers)
        assert before.status_code == 200
        with captured_logs(level=logging.INFO) as stream:
            await http.get("/metrics", headers=headers)
            await http.get("/api/health/ready")
        rows = json_lines(stream)
        assert all(row.get("msg") != "探针" for row in rows), rows


class TestBuildInfo:
    async def test_lifespan_publishes_the_build_info(self, http):
        """排障时第一个要回答的问题：指标变化的那个时刻，代码/配置变了没有。"""
        await http.get("/api/health")
        assert BUILD_INFO.value(version="1.3.0", app_mode="mock", retrieval_backend="bm25") == 1

    def test_set_build_info_is_a_plain_gauge(self):
        set_build_info(version="9.9.9", app_mode="live", retrieval_backend="vector")
        assert BUILD_INFO.value(version="9.9.9", app_mode="live", retrieval_backend="vector") == 1


# ==========================================================================
# 下单 / 取消
# ==========================================================================
EQUIPMENT_ID = 2  # 紫外可见分光光度计：不需资质、单次 4 小时
START = dt.time(10, 0)
END = dt.time(11, 0)


def _target_day() -> dt.date:
    """固定远期日期，避开种子数据与评测残留。"""
    return now_local().date() + dt.timedelta(days=45)


async def _book(user_id: int = 1, start: dt.time = START, end: dt.time = END):
    return await create_reservation(
        user_id=user_id,
        equipment_id=EQUIPMENT_ID,
        date_=_target_day(),
        start=start,
        end=end,
        purpose="指标测试",
    )


class TestBookingMetrics:
    async def test_success_is_counted(self, isolated_db):
        outcome = await _book()
        assert outcome.ok
        assert BOOKING_ATTEMPTS.value(outcome="ok") == 1

    async def test_business_conflict_is_a_separate_outcome(self, isolated_db):
        """★ 「坑已经没了」与「系统在打架」必须分开。

        前者是真实业务冲突（该多给备选），后者是并发争抢（该扩容）。
        合成一个数字，看板上就分不出该做哪件事。
        """
        assert (await _book()).ok
        second = await _book(user_id=2)
        assert not second.ok
        assert second.reason == "conflict"
        assert BOOKING_ATTEMPTS.value(outcome="conflict") == 1
        assert BOOKING_RETRIES.value() == 0, "复检就发现被占，不该记成并发争抢"

    async def test_invalid_request_is_a_separate_outcome(self, isolated_db):
        outcome = await _book(start=dt.time(10, 7), end=dt.time(11, 0))  # 粒度不对齐
        assert not outcome.ok
        assert outcome.reason == "invalid"
        assert BOOKING_ATTEMPTS.value(outcome="invalid") == 1

    async def test_contention_is_counted_when_the_index_is_the_only_guard(self, isolated_db, monkeypatch):
        """★ 把写前复检整个关掉，验证「唯一索引才是唯一保证」这句话是真的。

        这也是唯一能稳定走到 ``contention`` 与重试计数器的路径：
        正常并发下你可能一次都撞不上，而这条要求我们必须证明
        "复检坏了也不会超卖，且失败形态被如实分类"。
        """
        assert (await _book()).ok
        monkeypatch.setattr(booking_module, "find_conflict", _never_conflicts)
        outcome = await _book(user_id=2)
        assert not outcome.ok
        assert outcome.reason == "contention"
        assert BOOKING_ATTEMPTS.value(outcome="contention") == 1
        assert BOOKING_RETRIES.value() >= 1

        # 最关键的一条：**没有超卖**。写前校验被彻底绕过，唯一索引仍然拦住了它。
        async with session_scope() as session:
            count = await session.scalar(
                select(func.count()).select_from(Reservation).where(Reservation.date == _target_day())
            )
        assert count == 1

    async def test_cancel_outcomes_are_classified(self, isolated_db):
        created = await _book()
        assert created.reservation is not None
        reservation_id = created.reservation.id

        assert (await cancel_reservation(reservation_id=reservation_id, user_id=1)).ok
        assert (await cancel_reservation(reservation_id=reservation_id, user_id=1)).reason == "state"
        assert (await cancel_reservation(reservation_id=reservation_id, user_id=2)).reason == "forbidden"
        assert (await cancel_reservation(reservation_id=999_999, user_id=1)).reason == "not_found"

        assert CANCEL_ATTEMPTS.value(outcome="ok") == 1
        assert CANCEL_ATTEMPTS.value(outcome="state") == 1
        assert CANCEL_ATTEMPTS.value(outcome="forbidden") == 1
        assert CANCEL_ATTEMPTS.value(outcome="not_found") == 1

    async def test_an_unclassified_return_would_be_visible(self):
        """漏了 ``reason`` 的返回点必须露出来，而不是混进"成功"里。"""
        assert BookingOutcome(ok=False, message="忘了分类").outcome_label == "unknown"
        assert BookingOutcome(ok=True, message="忘了分类").outcome_label == "ok"


async def _never_conflicts(*args, **kwargs):
    """把写前复检换成"永远没冲突"，用来暴露唯一索引这一层。"""
    return None


# ==========================================================================
# 模型调用
# ==========================================================================
def _stub_httpx(monkeypatch, handler) -> None:
    """让 RealLLMClient 内部的 httpx 客户端改走 MockTransport。

    客户端在方法体里自己 ``httpx.AsyncClient(...)``，没有注入口，
    所以在类上换掉构造函数 —— 这样走的仍是**真实**的客户端代码路径
    （含它的重试循环），只是把网络那一跳换掉了。
    """
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "llm_base_url": "http://stub.invalid/v1",
        "llm_api_key": "test-key",
        "llm_max_retry": 2,
    }
    base.update(overrides)
    return Settings(**base)


def _json_reply(content: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return handler


class TestLlmMetrics:
    async def test_successful_call_is_measured_once(self, monkeypatch):
        _stub_httpx(monkeypatch, _json_reply('{"intent":"smalltalk","confidence":0.5,"reason":"x"}'))
        client = RealLLMClient(_settings())
        result = await client.classify_intent("你好")
        assert result.intent == "smalltalk"
        assert LLM_CALLS.value(op="classify_intent", outcome="ok") == 1
        # 量的是**逻辑调用**：内部重试次数为 0，所以耗时直方图只有一条观测
        assert LLM_DURATION.snapshot()[("classify_intent",)].total == 1
        assert LLM_RETRIES.value(op="classify_intent") == 0

    async def test_failed_call_is_measured_as_an_error_and_retries_are_counted(self, monkeypatch):
        """★ 重试次数要和最终结果分开记。

        「重试两次最后成功了」在成功率上是成功的，在"模型端有多不稳"上却是
        一个信号 —— 而后者才是能提前看到故障的那个。合成一个数就看不见它了。
        """

        def failing(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("模型端不可达", request=request)

        _stub_httpx(monkeypatch, failing)
        client = RealLLMClient(_settings())
        with pytest.raises(LLMError):
            await client.classify_intent("你好")
        assert LLM_CALLS.value(op="classify_intent", outcome="error") == 1
        assert LLM_RETRIES.value(op="classify_intent") == 2  # llm_max_retry=2

    async def test_a_call_that_recovers_still_counts_its_retries(self, monkeypatch):
        attempts = {"n": 0}

        def flaky(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ConnectError("第一次失败", request=request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"intent":"smalltalk","confidence":0.5,"reason":"x"}'}}]},
            )

        _stub_httpx(monkeypatch, flaky)
        client = RealLLMClient(_settings())
        assert (await client.classify_intent("你好")).intent == "smalltalk"
        assert LLM_CALLS.value(op="classify_intent", outcome="ok") == 1
        assert LLM_RETRIES.value(op="classify_intent") == 1

    async def test_each_operation_gets_its_own_series(self, monkeypatch):
        _stub_httpx(monkeypatch, _json_reply('{"intent":"smalltalk","confidence":0.5,"reason":"x"}'))
        client = RealLLMClient(_settings())
        await client.classify_intent("你好")
        await client.classify_intent("再见")
        assert LLM_CALLS.value(op="classify_intent", outcome="ok") == 2
        assert LLM_CALLS.value(op="compose", outcome="ok") == 0

    async def test_cancellation_is_not_reported_as_a_model_error(self):
        """★ 客户端中途断开和模型挂了完全不是一回事。

        混在一起会把"用户手速快"报成"模型不稳定"，然后所有人去看模型。
        """
        with pytest.raises(asyncio.CancelledError):
            async with track_llm_call("compose"):
                raise asyncio.CancelledError
        assert LLM_CALLS.value(op="compose", outcome="cancelled") == 1
        assert LLM_CALLS.value(op="compose", outcome="error") == 0

    async def test_the_mock_client_is_not_measured(self):
        """★ 假模型不该进"模型耗时"。

        它是本地确定性规则，记进来会让那张图里混进一片 0.1ms 的假调用 ——
        于是没人能从中看出真实模型的表现。**假的指标比没有指标更坏。**
        """
        client = MockLLMClient([("荧光光谱仪", "光谱")])
        await client.classify_intent("明天下午两点想用荧光光谱仪两小时")
        await client.compose({"kind": "smalltalk"})
        assert LLM_CALLS.snapshot() == {}
        assert LLM_DURATION.snapshot() == {}


# ==========================================================================
# 限流
# ==========================================================================
class TestRateLimitMetric:
    async def test_rejections_are_counted_per_limiter(self, http, as_user):
        headers = await as_user("张伟")
        http.app.state.chat_limiter = SlidingWindowLimiter(1, 60.0)
        first = await http.post("/api/agent/chat", json={"message": "你好"}, headers=headers)
        assert first.status_code == 200, first.text
        second = await http.post("/api/agent/chat", json={"message": "你好"}, headers=headers)
        assert second.status_code == 429
        assert RATE_LIMITED.value(limiter="chat") == 1

    async def test_no_rejection_no_series(self, http, as_user):
        headers = await as_user("张伟")
        assert (await http.post("/api/agent/chat", json={"message": "你好"}, headers=headers)).status_code == 200
        assert RATE_LIMITED.snapshot() == {}


# ==========================================================================
# 清扫
# ==========================================================================
class TestSweepMetrics:
    async def test_each_task_is_recorded_separately(self):
        async def ok_task() -> tuple[int, str]:
            return 3, "处理了 3 条"

        async def bad_task() -> tuple[int, str]:
            raise RuntimeError("归档目录没权限")

        await run_once(tasks=[SweepTask("好的", ok_task), SweepTask("坏的", bad_task)])
        assert SWEEP_RUNS.value(task="好的", outcome="ok") == 1
        assert SWEEP_RUNS.value(task="坏的", outcome="error") == 1
        assert SWEEP_PROCESSED.value(task="好的") == 3

    async def test_last_success_only_moves_on_success(self):
        """★ 这是 P1-2 那套东西唯一能被机器读到的"它还活着"。

        「清扫静默停了」在图上的表现必须是 ``time() - last_success`` 一路涨，
        而不是"什么都没有"（什么都没有没法告警）。
        """

        async def ok_task() -> tuple[int, str]:
            return 0, ""

        async def bad_task() -> tuple[int, str]:
            raise RuntimeError("坏了")

        await run_once(tasks=[SweepTask("好的", ok_task)])
        first = SWEEP_LAST_SUCCESS.value(task="好的")
        assert first is not None and first > 0

        await run_once(tasks=[SweepTask("坏的", bad_task)])
        assert SWEEP_LAST_SUCCESS.value(task="坏的") is None, "失败不该更新最近成功时间"
        assert SWEEP_LAST_SUCCESS.value(task="好的") == first, "别的任务失败不该影响它"

    def test_init_marks_never_run_tasks_as_zero(self):
        """有了 0，「从未成功过」与「很久没成功过」可以共用一条告警表达式。"""
        init_sweep_gauges(["甲", "乙"])
        assert SWEEP_LAST_SUCCESS.value(task="甲") == 0
        assert SWEEP_LAST_SUCCESS.value(task="乙") == 0

    async def test_cancellation_is_not_recorded_as_a_failure(self):
        """进程正常退出时不该留下一条假的失败记录。"""

        async def cancelled() -> tuple[int, str]:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await run_once(tasks=[SweepTask("被取消", cancelled)])
        assert SWEEP_RUNS.value(task="被取消", outcome="error") == 0

    async def test_duration_is_recorded_in_seconds(self):
        async def slowish() -> tuple[int, str]:
            await asyncio.sleep(0.02)
            return 1, ""

        await run_once(tasks=[SweepTask("慢的", slowish)])
        series = SWEEP_DURATION.snapshot()[("慢的",)]
        assert 0.01 < series.total_sum < 5.0, "如果这里是毫秒级，说明单位记错了"
