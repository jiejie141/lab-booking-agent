"""大模型接入：离线 Mock + 真实客户端。

两种实现共享同一组**语义化方法**，而不是一个裸的 ``chat()``：
    classify_intent / extract_requirement / ask_missing / compose
这样 Mock 不必去猜系统提示词里想让它干什么，测试也不依赖提示词措辞。

MockLLMClient 用确定性规则（中文数字、相对日期、设备目录匹配）模拟模型输出。
它的存在意义是三件事：
    1. 没网、没 key 也能跑通全流程与评测；
    2. 评测的基线稳定 —— 换真实模型时差异只来自模型本身；
    3. 把「中文时间表达怎么落地」这件苦活固定在可测的地方。
live 模式下换成 RealLLMClient，其余代码一行不改。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from typing import Any, Protocol

from ..clock import now_local
from ..config import Settings
from ..harness import ToolCall, TurnResult
from ..metrics import record_llm_retry, track_llm_call
from ..schemas import HARD_CONSTRAINTS, IntentKind, IntentResult, Proposal, Requirement

# 埋点只做在 RealLLMClient 上（见下面每个方法的说明）。
#
# ⚠️ 这里本来写的是一个 ``@_measured("op")`` 装饰器，5 个方法各挂一行，很干净 ——
# 但它挂上去之后 **mypy 立刻报错**：``RealLLMClient`` 不再满足 ``LLMClient`` 协议。
# 原因是 ``functools.wraps`` + ``ParamSpec`` 装饰出来的方法，mypy 没法再和
# Protocol 里那一行 ``async def classify_intent(self, message: str) -> IntentResult: ...``
# 对上（最小复现见 README 的踩坑记录）。
# 所以退回到最直白的写法：在**方法体里**用 ``async with track_llm_call(op)`` 包住。
# 代价是缩进多一层，换来的是类型契约完整 —— 而这里的类型契约是有实际用处的：
# ``build_client()`` 返回的就是 ``LLMClient``，协议一旦被绕过，
# "两种客户端可以互换"这件事就没有任何东西在守着了。


class LLMError(RuntimeError):
    """模型调用失败。调用方据此走降级路径，而不是把异常抛给用户。"""


def _strip_json_fence(text: str) -> str:
    """剥掉模型自作主张裹上的 markdown 代码围栏。

    实测（P2-3）：Moonshot 的 kimi-k2.6 即使带了 ``response_format=json_object``
    也会把输出包成 `` ```json\\n{...}\\n``` `` —— 参数保证了围栏**里面**是合法
    JSON，但围栏本身让 ``json.loads`` 在第 0 个字符就炸。处理顺序：
    先剥围栏，剥完直接解析；更花哨的"提取首个 JSON 对象"先不做 ——
    等真出现围栏之外的花样再加，不为想象中的模型行为写代码。
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    # 首行是 ```json / ``` 之类的围栏，末行是收尾 ```，中间才是负载
    body = "\n".join(lines[1:-1]) if len(lines) >= 3 else ""
    if body.rstrip().endswith("```"):
        body = body.rstrip()[:-3]
    return body.strip()


class LLMClient(Protocol):
    name: str

    async def classify_intent(self, message: str) -> IntentResult: ...

    async def extract_requirement(self, message: str, history: str = "") -> Requirement: ...

    async def ask_missing(
        self, missing: list[tuple[str, str]], requirement: Requirement
    ) -> str: ...

    async def compose(self, ctx: dict[str, Any]) -> str: ...

    async def chat_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> TurnResult:
        """带工具的一轮对话（function calling 的模型侧）。

        这是 harness 的 :class:`~lagent.harness.runtime.ToolCallingLLM` 协议，
        在**语言层之上的第二组能力**：前四个方法把语言翻成结构，这个方法让模型
        自己决定调哪个工具。两种执行模式（deterministic / react）分别只用其中一组。
        """
        ...


# ==========================================================================
# 中文时间/数量解析（Mock 与真实模型的后处理都用得上）
# ==========================================================================
_CN_DIGIT = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_PERIOD = ("凌晨", "早上", "上午", "中午", "下午", "傍晚", "晚上", "夜里")

_TIME_RE = re.compile(
    r"(?P<period>" + "|".join(_PERIOD) + r")?\s*"
    r"(?P<hour>\d{1,2}|[一二三四五六七八九十两]{1,3})\s*"
    r"(?P<mark>[:：点]|时)\s*"
    r"(?P<minute>\d{1,2}|[一二三四五六七八九十]{1,3}|半)?"
)


def cn_number(raw: str | None) -> int | None:
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    if raw in _CN_DIGIT:
        return _CN_DIGIT[raw]
    if "十" in raw:
        head, _, tail = raw.partition("十")
        tens = _CN_DIGIT.get(head, 1) if head else 1
        ones = _CN_DIGIT.get(tail, 0) if tail else 0
        return tens * 10 + ones
    return None


def _apply_period(hour: int, period: str | None) -> int:
    if period in (None, "", "凌晨", "早上", "上午"):
        return 0 if (period == "上午" and hour == 12) else hour
    if period == "中午":
        return hour if hour >= 12 else hour + 12
    # 下午 / 傍晚 / 晚上 / 夜里
    return hour + 12 if hour < 12 else hour


def parse_times(text: str) -> list[dt.time]:
    """抽出文本里所有时间点，按出现顺序返回。用于「3点到5点」这类表达。

    中文口语里时段词通常只出现一次：「下午两点到四点」的后半段「四点」
    是不带时段词的。所以第一遍先各自解析并记下「有没有显式时段词」，
    第二遍再做**时段继承**：后一个时间若没写时段、且不晚于前一个，
    就按下午补 12 小时（14:00 → 16:00 正确，9:00 → 11:00 不受影响）。
    """
    raw: list[tuple[int, int, bool]] = []
    for match in _TIME_RE.finditer(text):
        hour = cn_number(match.group("hour"))
        if hour is None or hour > 24:
            continue
        minute_raw = match.group("minute")
        minute = 30 if minute_raw == "半" else (cn_number(minute_raw) or 0)
        if minute > 59:
            continue
        period = match.group("period")
        hour = _apply_period(hour, period)
        if hour > 23:
            continue
        raw.append((hour, minute, bool(period)))

    found: list[dt.time] = []
    for idx, (hour, minute, explicit) in enumerate(raw):
        if idx > 0 and not explicit and hour < 12:
            prev = raw[idx - 1][0] * 60 + raw[idx - 1][1]
            if hour * 60 + minute <= prev:
                hour += 12
        found.append(dt.time(hour=hour, minute=minute))
    return found


def parse_date_cn(text: str, today: dt.date) -> dt.date | None:
    if "大后天" in text:
        return today + dt.timedelta(days=3)
    if "后天" in text:
        return today + dt.timedelta(days=2)
    if "明天" in text or "明日" in text:
        return today + dt.timedelta(days=1)
    if "今天" in text or "今日" in text:
        return today

    full = re.search(r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})", text)
    if full:
        return dt.date(int(full.group(1)), int(full.group(2)), int(full.group(3)))

    month_day = re.search(r"(\d{1,2})\s*[月/]\s*(\d{1,2})\s*[日号]?", text)
    if month_day:
        try:
            return dt.date(today.year, int(month_day.group(1)), int(month_day.group(2)))
        except ValueError:
            return None

    week = re.search(r"(下|本|这)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天])", text)
    if week:
        target = _WEEKDAY_CN[week.group(2)]
        if week.group(1) == "下":
            # 下周X = 下周一之后再偏移 target 天
            to_next_monday = (7 - today.weekday()) % 7 or 7
            return today + dt.timedelta(days=to_next_monday + target)
        delta = (target - today.weekday()) % 7
        return today + dt.timedelta(days=delta)
    return None


def parse_duration(text: str) -> float | None:
    if "半小时" in text:
        return 0.5
    match = re.search(r"(\d+(?:\.\d+)?|[一二三四五六七八九十两]{1,3})\s*(?:个)?\s*小时", text)
    if match:
        value = cn_number(match.group(1))
        return float(value) if value else None
    match = re.search(r"(\d+)\s*分钟", text)
    if match:
        return int(match.group(1)) / 60
    return None


# ==========================================================================
# 离线确定性实现
# ==========================================================================
def _temperature(settings, default: float) -> float:
    """模型级 temperature 覆盖（config.llm_temperature）。

    None = 回落到各调用点的内置值；设了值就是 model 级硬约束 ——
    某些推理型端点只接受一个固定 temperature，按用途拆配置只会
    配出自相矛盾的组合（比如抽取要 0、回复要 0.2，而端点只收 1）。
    """
    return settings.llm_temperature if settings.llm_temperature is not None else default


class MockLLMClient:
    """离线假模型：确定性、可复现、零网络。

    它不「理解」语言，只是把常见中文表达映射到结构化字段。
    这一点必须在文档里说清楚 —— 它是测试与评测的稳定基线，不是智能。
    """

    name = "mock"

    def __init__(self, catalog: list[tuple[str, str]] | None = None) -> None:
        # catalog: [(设备名, 类别), ...] 由调用方注入，和真实模型的提示词同源
        self.catalog = catalog or []

    # ---- 工具调用（function calling 的确定性替身）-------------------------
    async def chat_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> TurnResult:
        """确定性版本的「模型选工具」。

        它是替身，不是模拟器 —— 这里没有任何"假装理解"的成分，只有两条规则：
        按意图关键词选一个可用工具；拿到工具结果后**停止调工具**并给一段可读文本。

        第二条规则不可省：没有它，离线 ReAct 循环永远不会终止
        （假模型会一直要求调工具）。真实模型靠「读懂结果已经够了」停止，
        这里靠「上一条消息是 tool」判断 —— 机制不同，但循环形态一致，
        所以 ReAct 运行时的逻辑可以在离线环境下被完整测试。
        """
        available = {entry["function"]["name"] for entry in tools}
        last = messages[-1] if messages else {}

        if last.get("role") == "tool":
            return TurnResult(content=_answer_from_tool_result(str(last.get("content", ""))))

        user_text = _last_user_text(messages)
        intent = await self.classify_intent(user_text)

        if intent.intent == "check_admission" and "check_admission" in available:
            return TurnResult(
                tool_calls=[
                    ToolCall(id="call_1", name="check_admission", arguments={"query": user_text})
                ]
            )

        if intent.intent in ("query_availability", "create_reservation"):
            requirement = await self.extract_requirement(user_text)
            if requirement.date is None:
                # 连日期都没有：调工具也只会拿到 no_date，不如直接追问。
                # 这与确定性路径的行为一致，两条路径不该在同一个输入上分叉。
                return TurnResult(
                    content=await self.ask_missing(requirement.missing_slots(), requirement)
                )
            if "query_availability" in available:
                return TurnResult(
                    tool_calls=[
                        ToolCall(
                            id="call_1",
                            name="query_availability",
                            arguments=_query_arguments(requirement),
                        )
                    ]
                )

        if intent.intent == "cancel_reservation":
            # 取消工具带副作用，默认不在 available 里。如实说明要走确认，
            # 而不是假装已经取消了 —— 这一点必须与真实运行时的行为一致。
            return TurnResult(
                content=(
                    "取消属于会改变记录的操作，需要你确认具体是哪一条（例如「取消 #12」），"
                    "我再执行。"
                )
            )

        return TurnResult(content=await self.compose({"kind": "smalltalk"}))

    # ---- 意图 ----------------------------------------------------------
    async def classify_intent(self, message: str) -> IntentResult:
        text = message.strip()
        rules: list[tuple[IntentKind, tuple[str, ...]]] = [
            ("cancel_reservation", ("取消", "退掉", "不约了", "撤销")),
            ("check_admission", ("资质", "准入", "规范", "安全", "培训", "授权")),
            ("query_availability", ("空闲", "有空", "能约吗", "什么时候", "还有位置", "查一下")),
            ("create_reservation", ("预约", "想约", "订", "预订", "帮我约", "占")),
        ]
        for intent, keys in rules:
            if any(key in text for key in keys):
                return IntentResult(intent=intent, confidence=0.9, reason=f"命中关键词 {keys}")
        if parse_date_cn(text, now_local().date()) and parse_times(text):
            # 有时间有日期但没动词，多半就是想约
            return IntentResult(intent="create_reservation", confidence=0.55, reason="含日期与时间")
        return IntentResult(intent="smalltalk", confidence=0.4, reason="未命中任何意图关键词")

    # ---- 槽位 ----------------------------------------------------------
    async def extract_requirement(self, message: str, history: str = "") -> Requirement:
        # 把历史拼在前面：真实模型也需要多轮上下文才能补齐槽位
        merged = f"{history}\n{message}".strip()
        today = now_local().date()

        date_ = parse_date_cn(merged, today)
        times = parse_times(message)
        duration = parse_duration(message)

        start = times[0] if times else None
        end = times[1] if len(times) > 1 else None

        name, category = self._match_equipment(merged)

        capacity = None
        cap = re.search(r"(\d{1,3})\s*(?:个)?\s*人", merged)
        if cap:
            capacity = int(cap.group(1))

        return Requirement(
            date=date_,
            start=start,
            end=end,
            duration_hours=duration,
            equipment_name=name,
            category=category,
            capacity=capacity,
            purpose="实验" if "实验" in merged else "",
        ).normalized()

    def _match_equipment(self, text: str) -> tuple[str | None, str | None]:
        """在设备目录里找最长命中；类别命中优先于泛化词。"""
        if "不限" in text or "随便" in text or "都行" in text:
            return None, None
        best_name: str | None = None
        for name, _category in self.catalog:
            for probe in (name, name[:2], name.rstrip("仪")):
                if probe and len(probe) >= 2 and probe in text:
                    if best_name is None or len(name) > len(best_name):
                        best_name = name
                    break
        if best_name:
            for name, _ in self.catalog:
                if name == best_name:
                    return name, None
        for _name, category in self.catalog:
            if category and category in text:
                return None, category
        return None, None

    # ---- 追问 ----------------------------------------------------------
    async def ask_missing(
        self, missing: list[tuple[str, str]], requirement: Requirement
    ) -> str:
        if not missing:
            return ""
        known = requirement.summary()
        prefix = f"已记下：{known}。" if known else ""
        questions = "；".join(question for _field, question in missing)
        return f"{prefix}还需要确认：{questions}"

    # ---- 组织回答 ------------------------------------------------------
    async def compose(self, ctx: dict[str, Any]) -> str:
        kind = ctx.get("kind", "answer")

        if kind == "smalltalk":
            return (
                "我是实验室预约助手。你可以直接说需求，比如"
                "「明天下午两点想用荧光光谱仪两小时」，"
                "我会帮你查约束、找可用时段，冲突时还会给几个备选方案。"
            )

        if kind == "booking":
            outcome = ctx.get("booking")
            if outcome and outcome.ok:
                return f"{outcome.message}。需要修改或取消随时告诉我。"
            if outcome:
                return f"没能下单：{outcome.message}"
            return "下单失败，请稍后再试。"

        if kind == "admission":
            hits = ctx.get("citations") or []
            if not hits:
                return "没有检索到相关规范条文。"
            lines = [f"《{hits[0].source}》{hits[0].heading}：" + hits[0].text[:120] + "…"]
            for extra in hits[1:3]:
                lines.append(f"另见「{extra.heading}」。")
            return "\n".join(lines)

        # availability / 协商
        proposals: list[Proposal] = ctx.get("proposals") or []
        satisfied = ctx.get("satisfied", False)
        blockers: list[str] = ctx.get("blockers") or []

        if not proposals:
            # 「为什么不行」要分类作答。全用同一句「换个时间试试」是最省事也最没用的：
            # 缺资质、设备停用这类问题，用户换一百个时间也约不上，
            # 该说的是「先去办手续」，而不是让他继续在时间上打转。
            kind = ctx.get("blocker_kind")
            checks = ctx.get("checks") or []
            hard = [c for c in checks if not c.passed and c.name in HARD_CONSTRAINTS]

            if kind == "no_date":
                return (
                    "还没确定是哪一天，我算不了可用时段。"
                    "告诉我日期就行，比如「明天下午两点到四点」。"
                )
            if kind == "no_target":
                return (
                    "没有找到匹配的设备。换个设备名或类别试试，"
                    "比如「荧光光谱仪」「细胞培养箱」「离心机」。"
                )
            if hard:
                reasons = "；".join(c.detail for c in hard[:2])
                return (
                    f"这个需求不是换个时间能解决的：{reasons}。"
                    "等这条满足了我再帮你排时段。"
                )
            head = "没找到可用时段。"
            if blockers:
                head += "原因：" + "；".join(blockers[:3]) + "。"
            return head + "要不要换个日期、缩短时长，或改用同类设备？"

        # 这里**只给结论和原因，不再逐条复述方案**。
        #
        # 2026-10-09 改：控制台会把 proposals 渲染成结构化的卡片（设备 / 时段 /
        # 放宽了什么 / 选这个按钮），而这段话同时把它们列一遍 —— 同一件事说了两遍，
        # 且要横向比较"这几条各自放宽了什么"时，文字版远不如卡片。
        # 分工变成：**代码给事实与结构，文字只给结论**。
        # 逐条文字在纯文本通道（CLI / 脚本调接口）仍然有用，见下面 detail 参数。
        head = (
            "找到完全满足你要求的时段，请选择一条下单："
            if satisfied
            else "原条件约不到。下面是放宽后可行的方案，每条都标注了放宽什么："
        )
        if not satisfied and blockers:
            head += "\n原因：" + "；".join(blockers[:2]) + "。"
        lines = [head]
        # 编号仍然报出来：用户的"第 2 个"要能对上号，而这段文字是
        # 会话历史的一部分（模型与状态机都从这里读上一轮给了什么）。
        for idx, item in enumerate(proposals, start=1):
            lines.append(f"{idx}. {item.label()}")
        lines.append("回复序号我帮你下单。")
        return "\n".join(lines)


# ==========================================================================
# 真实模型实现（OpenAI 兼容接口，直连 httpx，不依赖 langchain-openai）
# ==========================================================================
def _is_rate_limited(exc: BaseException) -> bool:
    """判断一次失败是不是「被限流」（429）。

    只看状态码本身，不看文案：不同供应商的报错文案各不相同，
    而 429 的处置（按配额周期退避）与超时/5xx（小步重试）是两套策略。
    拿不到状态码时保守返回 False —— 那样走的是原来的小步退避，
    而不是让所有失败都排到 15 秒这一档上。
    """
    status = getattr(exc, "response", None)
    code = getattr(status, "status_code", None) if status is not None else None
    if code is None:
        # httpx.HTTPStatusError 的字符串里带状态码，回退到文本匹配
        return "429" in str(exc) or "Too Many Requests" in str(exc)
    return code == 429


class RealLLMClient:
    name = "live"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not settings.llm_api_key:
            raise LLMError("live 模式需要配置 LAB_LLM_API_KEY")

    async def _json(self, system: str, user: str, op: str) -> dict:
        """发一次要 JSON 的调用，失败重试。

        ``op`` 是必须的（没有默认值）：重试次数要归到**具体哪个操作**上 ——
        「compose 在重试」和「classify_intent 在重试」指向完全不同的原因
        （前者多半是输出太长/被截断，后者多半是并发打满）。给个默认值的话，
        新增调用点时忘了传也不会报错，只会让那个操作的重试永远算到别人头上。
        """
        import httpx

        # 量的是**逻辑调用**（含内部重试的总耗时），所以包在最外层；
        # 重试次数单独记（见下面 record_llm_retry 的说明）。
        async with track_llm_call(op):
            url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
            payload = {
                "model": self.settings.llm_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": _temperature(self.settings, 0),
                "response_format": {"type": "json_object"},
            }
            last: Exception | None = None
            for attempt in range(self.settings.llm_max_retry + 1):
                try:
                    async with httpx.AsyncClient(timeout=self.settings.llm_timeout) as client:
                        resp = await client.post(
                            url,
                            json=payload,
                            headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
                        )
                        resp.raise_for_status()
                        raw = resp.json()["choices"][0]["message"]["content"] or ""
                        return json.loads(_strip_json_fence(raw))
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    # 记「额外的 HTTP 次数」而不是"失败了的调用"：一个 op 重试两次
                    # 最终成功，与它直接失败，在成功率上是两件事，在"模型端有多不稳"
                    # 上却是同一件事 —— 后者才是要提前看到的信号。
                    if attempt < self.settings.llm_max_retry:
                        record_llm_retry(op)
                        # 退避分两种，因为 429 和超时的性质完全不同：
                        #   429 = 配额没耗尽、只是这一刻太快 —— 等一会儿就好，
                        #         但按**配额周期**等（起步 15s），否则 0.5s/1s
                        #         这种小步退避只是把同一次 429 再撞一遍。
                        #   其他（超时/5xx）= 可能是偶发抖动，小步退避即可。
                        # 实测：Moonshot 免费/低档位 RPM 很低，eval 连跑 14 个
                        # 用例时 0.5s 退避在第 2 个用例就撞死在 429 上；
                        # 改成按 429 单独退避后才跑得完。
                        # 另：429 应当尊重服务端给的 Retry-After —— 但 httpx 的
                        # 异常对象拿不到响应头，这里退而用固定起步值，
                        # 并在最后一次仍失败时把 429 明确写进错误消息里。
                        if _is_rate_limited(exc):
                            await asyncio.sleep(15 * (attempt + 1))
                        else:
                            await asyncio.sleep(0.5 * (attempt + 1))
            raise LLMError(f"模型调用失败：{last}")

    async def _text(self, system: str, user: str, op: str) -> str:
        """发一次要自由文本的调用（失败重试，与 _json 同一策略）。

        ``op`` 同样必须显式传：它决定这次耗时算在 ``ask_missing`` 还是 ``compose``
        头上，而这两个操作该看的量级完全不同。

        ★ 这里原本是**不重试**的，理由是「自由文本不像 JSON 那样会因截断而失败」。
        但实测换到 Moonshot 之后它成了第一个被 429 打死的地方：
        eval 跑到 ``ask_missing`` / ``compose`` 就崩，而这两步走的正是 _text。
        也就是说「不重试」这个决定把限流风险**全部**推给了这两个节点 ——
        而它们是流程里最靠后、最像「系统坏了」的两步。
        现在与 _json 一致：429 按配额周期退避，其他小步退避。
        """
        import httpx

        async with track_llm_call(op):
            url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
            payload = {
                "model": self.settings.llm_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": _temperature(self.settings, 0.2),
            }
            last: Exception | None = None
            for attempt in range(self.settings.llm_max_retry + 1):
                try:
                    async with httpx.AsyncClient(timeout=self.settings.llm_timeout) as client:
                        resp = await client.post(
                            url,
                            json=payload,
                            headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
                        )
                        resp.raise_for_status()
                        return resp.json()["choices"][0]["message"]["content"]
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    if attempt < self.settings.llm_max_retry:
                        record_llm_retry(op)
                        if _is_rate_limited(exc):
                            await asyncio.sleep(15 * (attempt + 1))
                        else:
                            await asyncio.sleep(0.5 * (attempt + 1))
            raise LLMError(f"模型调用失败：{last}") from last

    async def chat_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> TurnResult:
        """真实的 function calling：把 tools 交给模型，由它决定调哪个。

        三个协议细节在这一层抹平，别处不必再关心：

        1. ``arguments`` 在 wire format 里是 **JSON 字符串**，这里解析成 dict ——
           运行时只处理结构化之后的形状，否则每个分支都要判「字符串还是字典」；
        2. 模型偶尔给出**非法 JSON** 的 arguments。这时不抛错、给空 dict：
           空 dict 会在参数校验那一关被拒，并以「缺哪个字段」回喂给它重试，
           比在协议层直接失败多一次自愈机会；
        3. ``tool_choice="auto"``：允许模型不调工具直接回答（闲聊、追问都走这条）。
           写死 ``required`` 会逼它为了调工具而调工具。
        """
        import httpx

        async with track_llm_call("chat_tools"):
            url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
            payload = {
                "model": self.settings.llm_model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                "temperature": _temperature(self.settings, 0),
            }
            last: Exception | None = None
            for attempt in range(self.settings.llm_max_retry + 1):
                try:
                    async with httpx.AsyncClient(timeout=self.settings.llm_timeout) as client:
                        resp = await client.post(
                            url,
                            json=payload,
                            headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
                        )
                        resp.raise_for_status()
                        message = resp.json()["choices"][0]["message"]
                        return TurnResult(
                            content=message.get("content"),
                            tool_calls=[
                                _parse_tool_call(raw)
                                for raw in (message.get("tool_calls") or [])
                            ],
                        )
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    if attempt < self.settings.llm_max_retry:
                        record_llm_retry("chat_tools")
                        await asyncio.sleep(0.5 * (attempt + 1))
            raise LLMError(f"模型调用失败：{last}")

    async def classify_intent(self, message: str) -> IntentResult:
        today = now_local().date().isoformat()
        data = await self._json(
            "你是实验室预约系统的意图分类器。只输出 JSON，字段："
            "intent（query_availability/create_reservation/cancel_reservation/"
            "check_admission/smalltalk 之一）、confidence（0-1）、reason。",
            f"今天是 {today}。用户说：{message}",
            op="classify_intent",
        )
        try:
            return IntentResult(**data)
        except Exception as exc:
            raise LLMError(f"意图输出无法解析：{data}") from exc

    async def extract_requirement(self, message: str, history: str = "") -> Requirement:
        today = now_local().date().isoformat()
        catalog = "、".join(f"{n}({c})" for n, c in _default_catalog()) or "（未知）"
        data = await self._json(
            "你是实验室预约系统的槽位抽取器。只输出 JSON，字段：date（YYYY-MM-DD 或 null）、"
            "start（HH:MM 或 null）、end（HH:MM 或 null）、duration_hours（数字或 null）、"
            "equipment_name、category、capacity（整数或 null）、purpose。"
            "不知道的字段给 null，不要编造。",
            f"今天是 {today}。可选设备目录：{catalog}。\n历史：{history}\n用户现在说：{message}",
            op="extract_requirement",
        )
        # 三种"没给"都要滤掉：空串、字面量 "null"（文本模型）、JSON null
        # （None —— kimi-k2.6 会把"不知道"显式输出为 null，而 Requirement.purpose
        # 是 str，None 直传就是一条 ValidationError。此 bug 在 DeepSeek 上潜伏，
        # 换一家输出习惯不同的模型立刻现形 —— 模型一换，边界全换）。
        cleaned = {k: v for k, v in data.items() if v not in ("", "null", "未知", None)}
        try:
            return Requirement(**cleaned).normalized()
        except Exception as exc:
            raise LLMError(f"槽位输出无法解析：{data}") from exc

    async def ask_missing(
        self, missing: list[tuple[str, str]], requirement: Requirement
    ) -> str:
        fields = "；".join(f"{f}（参考问法：{q}）" for f, q in missing)
        return await self._text(
            "你是实验室预约助手。用一句中文向用户追问缺失信息，语气自然、口语化，不要罗列字段名。",
            f"已抽取到的信息：{requirement.summary() or '无'}。缺失字段：{fields}。",
            op="ask_missing",
        )

    async def compose(self, ctx: dict[str, Any]) -> str:
        """真实模型只负责「措辞」；事实（备选、约束、引用）由代码提供，不允许它编。"""
        payload = {
            "kind": ctx.get("kind"),
            "satisfied": ctx.get("satisfied"),
            # 未通过的约束按「硬/软」分好类再交给模型：
            # 硬约束（缺资质、设备停用）不该被写成「换个时间再试」。
            "blocker_kind": ctx.get("blocker_kind"),
            "hard_blockers": [
                c.detail
                for c in (ctx.get("checks") or [])
                if not c.passed and c.name in HARD_CONSTRAINTS
            ],
            "blockers": ctx.get("blockers", [])[:3],
            "proposals": [
                {
                    "label": p.label(),
                    "hours": p.hours,
                    "relaxations": p.relaxations,
                }
                for p in (ctx.get("proposals") or [])
            ],
            "booking_message": getattr(ctx.get("booking"), "message", None),
            "citations": [
                {"heading": h.heading, "text": h.text[:200]}
                for h in (ctx.get("citations") or [])[:3]
            ],
        }
        return await self._text(
            "你是实验室预约助手。根据给定的结构化事实，用简洁的中文回复用户。"
            "严禁添加事实里没有的时段、设备或规范条文；"
            "如果是备选方案，务必说明每条放宽了什么；"
            "如果 hard_blockers 非空，说明这是资质或设备状态问题，"
            "不要建议用户更换时段。"
            # 语言约束单独强调，是因为它真的漏过：
            # 实测拿到过「您 requested 的 2026-10-10 14:00–16:00 荧光光谱仪 已被占用」——
            # 事实全对、措辞里却夹了一个英文词。payload 里混着英文键名
            # （kind/satisfied/proposals/hard_blockers），模型会顺着上下文
            # 把英文带进正文。这类瑕疵不影响判分逻辑，但**一眼就能看出是机器写的**，
            # 而"回复像是助手说的"恰恰是这个项目对外展示的核心。
            "**全文必须使用中文**：不得出现英文单词、英文短语或中英混写，"
            "设备型号（如 F-7000）与时间（如 14:00）除外。",
            json.dumps(payload, ensure_ascii=False),
            op="compose",
        )


# ==========================================================================
# function calling 的协议适配与 Mock 辅助
# ==========================================================================
def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return str(msg.get("content") or "")
    return ""


def _query_arguments(requirement: Requirement) -> dict[str, Any]:
    """把已抽取的诉求转成 query_availability 的工具参数。

    只带非空字段：多传一个 ``null`` 对模型没坏处，但会让「参数是谁给的」
    这件事变模糊 —— 而这一层恰恰是要保持「参数来自抽取结果」这一点清楚。
    """
    args: dict[str, Any] = {"date": requirement.date.isoformat() if requirement.date else ""}
    if requirement.start:
        args["start"] = requirement.start.strftime("%H:%M")
    if requirement.end:
        args["end"] = requirement.end.strftime("%H:%M")
    if requirement.duration_hours:
        args["duration_hours"] = requirement.duration_hours
    if requirement.equipment_name:
        args["equipment_name"] = requirement.equipment_name
    if requirement.category:
        args["category"] = requirement.category
    return args


def _answer_from_tool_result(text: str) -> str:
    """Mock 的「读完工具结果就作答」。

    刻意不做真实摘要：Mock 的职责是让链路可跑、评测可复现，不是假装会写
    自然语言。所以它如实呈现工具结果，并标明这来自确定性假模型 ——
    真实模型在这一步会把这些结构化事实措辞成人话（见 compose 的约束）。
    """
    head = text.strip()
    if len(head) > 400:
        head = head[:400] + "…"
    return f"（mock 模型基于工具结果的答复）\n{head}"


def _parse_tool_call(raw: dict[str, Any]) -> ToolCall:
    """把 wire format 的 tool_call 解析成结构化形状。

    ``arguments`` 兼容两种形态：标准协议给 JSON 字符串，部分兼容端点直接给对象。
    非法 JSON 归成空 dict（原因见 RealLLMClient.chat_tools 的说明）。
    """
    function = raw.get("function") or {}
    raw_args = function.get("arguments")
    arguments: dict[str, Any] = {}
    if isinstance(raw_args, dict):
        arguments = raw_args
    elif isinstance(raw_args, str) and raw_args.strip():
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            arguments = parsed
    return ToolCall(
        id=str(raw.get("id") or ""),
        name=str(function.get("name") or ""),
        arguments=arguments,
    )


def _default_catalog() -> list[tuple[str, str]]:
    """兜底目录：真实模型在没有数据库时也要知道有哪些设备类别可选。"""
    return [
        ("荧光光谱仪", "光谱"),
        ("紫外可见分光光度计", "光谱"),
        ("CO2 培养箱", "细胞培养"),
        ("生物安全柜", "细胞培养"),
        ("高速离心机", "离心"),
        ("高效液相色谱仪", "色谱"),
    ]


# ==========================================================================
# 工厂：模型不可用时返回 None，交给上层走降级
# ==========================================================================
def build_client(settings: Settings, catalog: list[tuple[str, str]] | None = None) -> LLMClient | None:
    if settings.app_mode == "degraded":
        return None
    if settings.app_mode == "mock":
        return MockLLMClient(catalog or _default_catalog())
    try:
        return RealLLMClient(settings)
    except LLMError:
        # 配了 live 但没给 key —— 不静默退回 mock（那会让人以为在用真模型），
        # 而是返回 None，让整体进入「引导式表单」降级态，界面上能看见。
        return None
