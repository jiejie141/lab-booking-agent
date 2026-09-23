"""上下文组装：分层 + token 预算 + 确定的超限裁剪策略。

这是 JD 里「上下文管理」那个词落到代码上的地方，也是本项目在抽出 harness
之前**完全没有**的能力：那时把历史拼成一个字符串直接塞进 prompt，从不计算
它有多大，也从不决定「装不下时该牺牲什么」。

为什么不引 tiktoken，改用自己估：
    预算只要**保守且确定**就够用，而 tiktoken 会引入一个带本地二进制资源的
    重依赖，且不同模型的编码还不一样。估小一点（宁可多裁）的代价是多截几段
    文本，估大一点的代价是把请求打爆 —— 两者不对称，所以往保守估。

裁剪优先级按「丢掉它的代价」排，填充顺序就是这个顺序：

    1. **system、当前用户消息、这一轮刚拿到的工具结果：永不丢弃。**
       前两者是规则与本次任务；最后一个是模型**此刻唯一的观察** ——
       丢了它 ReAct 循环必然不收敛：模型看不到答案，只会把同一个工具再调一遍，
       直到步数耗尽。这个坑在把预算调小后立刻显形（详见
       ``test_live_tool_result_survives_a_starving_budget``）。
       另外 ``tool`` 消息必须与携带 ``tool_call_id`` 的 assistant 父消息成对出现，
       这是 OpenAI 协议要求，所以要**按「轮」整组保留**，不能逐条挑。
    2. **对话历史**：保留最新的、丢最旧的。会损失指代能力（"那它呢？"），
       但比丢任务本身轻。
    3. **跨轮摘要（memory）**：放不下就截尾。砍了只是"记得少一点"。
    4. **较早的工具轮次：最先被牺牲。** 它是原料，结论通常已经进了上一轮
       模型自己的判断；而且这些轮次越旧越无关。

预算窄到连第 1 档都装不下时，如实标注 ``over_budget`` 而不是偷偷丢一条继续跑 ——
该被看见的问题是「预算配小了」，不是「模型看到了残缺的提示词」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# CJK 统一表意文字、CJK 标点、全角字符 —— 这些字符的 token 密度远高于 ASCII。
_CJK_RANGES = (
    ("\u3000", "\u303f"),  # CJK 标点
    ("\u4e00", "\u9fff"),  # CJK 统一表意文字
    ("\uff00", "\uffef"),  # 全角字符
)


def estimate_tokens(text: str) -> int:
    """粗估 token 数：CJK 字符计 1，其余字符计 0.25（即 4 字符 1 token）。

    这个比例不是精确值，而是**偏保守**的经验值：中文在多数 tokenizer 下
    接近 1 字 1 token，英文约 4 字符 1 token。偏保守意味着宁可多裁一点。
    """
    cjk = 0
    other = 0
    for ch in text:
        if any(lo <= ch <= hi for lo, hi in _CJK_RANGES):
            cjk += 1
        else:
            other += 1
    return cjk + (other + 3) // 4


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """估算一组 chat 消息的总 token。

    每条消息加 4 的固定开销，近似 role 标记等结构性 token ——
    忽略它会让「很多条短消息」的估算系统性偏低。
    """
    total = 0
    for msg in messages:
        total += 4
        content = msg.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            total += estimate_tokens(str(tool_calls))
    return total


@dataclass
class ContextBuild:
    """一次组装的产物 + **裁剪记录**。

    裁剪记录不是调试信息，而是对外可解释性的一部分：被问「超限时你丢什么」，
    答案就是这个对象的字段，而不是一段回忆。
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    estimated_tokens: int = 0
    dropped_tool_results: int = 0
    dropped_recent: int = 0
    memory_truncated: bool = False
    over_budget: bool = False

    @property
    def trimmed(self) -> bool:
        return bool(
            self.dropped_tool_results or self.dropped_recent or self.memory_truncated
        )

    def describe(self) -> str:
        """给 trace span 用的一句话摘要。"""
        bits = [f"~{self.estimated_tokens} token"]
        if self.dropped_tool_results:
            bits.append(f"丢工具结果 {self.dropped_tool_results}")
        if self.memory_truncated:
            bits.append("截断摘要")
        if self.dropped_recent:
            bits.append(f"丢历史 {self.dropped_recent} 条")
        if self.over_budget:
            bits.append("超预算")
        return " · ".join(bits)


class ContextBuilder:
    """把各层内容按预算组装成 chat messages。

    刻意保持**无状态**：每次调用重新算，不在实例里缓存上一轮结果。
    缓存会让「同一输入必得同一输出」这条测试前提失效，而裁剪逻辑恰恰是
    最需要被测试钉死的地方。
    """

    def __init__(self, *, max_tokens: int = 3000) -> None:
        self.max_tokens = max_tokens

    def build(
        self,
        *,
        system: str,
        memory: str = "",
        recent: list[dict[str, Any]] | None = None,
        tool_results: list[dict[str, Any]] | None = None,
    ) -> ContextBuild:
        recent = list(recent or [])
        # 先按「轮」分组：assistant(tool_calls) + 它引发的那批 tool 结果是一体的。
        rounds = _group_rounds(list(tool_results or []))
        result = ContextBuild()
        system_msg: dict[str, Any] = {"role": "system", "content": system}

        # ---- 优先级 1：永不丢弃的部分 ----
        # 当前用户消息要先从 recent 里**摘出来**，否则它会既算进 must、
        # 又被 _take_newest 当成历史再取一次（重复计费且 prompt 里出现两遍）。
        current = _last_user_message(recent)
        history = [msg for msg in recent if msg is not current]
        must = [system_msg] + ([current] if current is not None else [])

        # 这一轮刚拿到的工具结果同样属于「永不丢弃」，理由见模块 docstring。
        live: list[dict[str, Any]] = []
        if rounds:
            live = rounds.pop()

        budget = self.max_tokens - estimate_messages_tokens(must) - estimate_messages_tokens(live)
        if budget < 0:
            # 连必需项都装不下：如实上报，不偷偷丢。装配顺序仍要正确。
            result.messages = must + live
            result.estimated_tokens = estimate_messages_tokens(result.messages)
            result.over_budget = True
            result.dropped_tool_results = _count_messages(rounds)
            return result

        # ---- 优先级 2：对话历史（保留最新的，丢最旧的）----
        kept_history, result.dropped_recent, budget = _take_newest(history, budget)

        # ---- 优先级 3：跨轮摘要（放不下就截尾）----
        kept_memory = ""
        if memory:
            cost = estimate_tokens(memory) + 4
            if cost > budget:
                kept_memory = _truncate_to(memory, max(0, budget - 4))
                result.memory_truncated = True
                budget -= estimate_tokens(kept_memory) + 4
            else:
                kept_memory = memory
                budget -= cost

        # ---- 优先级 4：较早的工具轮次（最先被牺牲，且整轮取舍）----
        kept_older, result.dropped_tool_results, budget = _take_newest_rounds(rounds, budget)

        # ---- 装配。顺序必须符合 chat 语义：工具结果跟在当前消息之后 ----
        messages: list[dict[str, Any]] = [system_msg]
        if kept_memory:
            messages.append({"role": "system", "content": f"[已知诉求] {kept_memory}"})
        messages.extend(kept_history)
        if current is not None:
            messages.append(current)
        messages.extend(kept_older)
        messages.extend(live)

        result.messages = messages
        result.estimated_tokens = estimate_messages_tokens(messages)
        return result


# --------------------------------------------------------------------------
def _last_user_message(recent: list[dict[str, Any]]) -> dict[str, Any] | None:
    for msg in reversed(recent):
        if msg.get("role") == "user":
            return msg
    return None


def _group_rounds(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """把工具层的消息按「轮」分组。

    一轮 = 一条请求工具调用的 assistant 消息 + 紧随其后的若干 ``tool`` 结果。

    关键在于**轮是由 assistant 消息开启的**，不是由 tool 消息开启的：
    没有父消息的 ``tool`` 消息各自成一轮，而不是互相合并。
    合并是会被踩到的 —— 调用方只传一串裸结果时（早期测试就是这么写的），
    它们会被看成同一轮，于是「最新一轮永不丢弃」变成了「整串永不丢弃」，
    预算直接失效。

    分组本身是必需的，不是整理癖：OpenAI 协议要求每条 ``tool`` 消息前面必须有
    一条带对应 ``tool_call_id`` 的 assistant 消息。逐条裁剪很容易留下孤儿，
    真实 API 会直接 400；整组取舍则天然不会破坏这个配对。
    """
    rounds: list[list[dict[str, Any]]] = []
    open_round: list[dict[str, Any]] | None = None
    for msg in items:
        if msg.get("role") == "tool" and open_round is not None:
            open_round.append(msg)
            continue
        open_round = None
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            open_round = [msg]
            rounds.append(open_round)
            continue
        rounds.append([msg])
    return rounds


def _count_messages(rounds: list[list[dict[str, Any]]]) -> int:
    return sum(len(r) for r in rounds)


def _take_newest(
    items: list[dict[str, Any]], budget: int
) -> tuple[list[dict[str, Any]], int, int]:
    """从最新往回取，直到装不下。返回 (保留的, 丢掉的条数, 剩余预算)。

    从最新往回取、但**按原顺序返回**：模型看到的时间顺序必须是对的，
    否则「他说的那个」会指向错误的一轮。
    """
    kept_reversed: list[dict[str, Any]] = []
    remaining = budget
    for item in reversed(items):
        cost = estimate_messages_tokens([item])
        if cost > remaining:
            break
        kept_reversed.append(item)
        remaining -= cost
    kept = list(reversed(kept_reversed))
    return kept, len(items) - len(kept), remaining


def _take_newest_rounds(
    rounds: list[list[dict[str, Any]]], budget: int
) -> tuple[list[dict[str, Any]], int, int]:
    """整轮地从最新往回取。返回 (保留的消息, 丢掉的消息数, 剩余预算)。

    与 :func:`_take_newest` 的差别只在「以轮为单位取舍」——
    一轮要么整体在内、要么整体在外，理由见 :func:`_group_rounds`。

    取的是**连续后缀**：一旦某轮装不下就停止，不再回头去捡更旧的。
    保留零散的一堆旧轮次只会让模型读到有缺口的对话，不如给它一段连续的近期历史。
    """
    kept_rounds: list[list[dict[str, Any]]] = []
    remaining = budget
    cut = len(rounds)
    for idx in range(len(rounds) - 1, -1, -1):
        group = rounds[idx]
        cost = estimate_messages_tokens(group)
        if cost > remaining:
            # 从 idx 起（含 idx 自己）全部丢弃 —— 这里写成 idx + 1 会漏掉
            # 那条恰好装不下的、也是最新被牺牲的一轮，裁剪计数随之偏小。
            cut = idx
            break
        kept_rounds.append(group)
        remaining -= cost
    kept = [msg for group in reversed(kept_rounds) for msg in group]
    return kept, _count_messages(rounds[cut:]), remaining


def _truncate_to(text: str, token_budget: int) -> str:
    """按 token 预算截断文本（保留头部）。

    保留头部而不是尾部：摘要类文本的主体在开头（日期、时间、设备），
    尾部通常越说越细（补充条件）。截尾的损失比截头小。
    """
    if token_budget <= 0:
        return ""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= token_budget:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + ("…" if lo < len(text) else "")
