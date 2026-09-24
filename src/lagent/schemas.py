"""对外数据契约（Pydantic v2）。

这里最值得看的是 ``Proposal``：它把「约束冲突」表达成**一组逐级放宽的备选**，
而不是一个布尔值。教学版预约系统在冲突时只会回「该时段不可预约」，
用户得自己换条件反复试；本项目要求 Agent 把「放宽了哪一条约束」
明说给用户，让权衡可见、可比较、可拒绝。
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .clock import minutes_between, parse_date, parse_time

# --------------------------------------------------------------------------
# 意图
# --------------------------------------------------------------------------
IntentKind = Literal[
    "query_availability",  # 查空闲
    "create_reservation",  # 下单
    "cancel_reservation",  # 取消
    "check_admission",     # 查准入资质 / 安全规范
    "smalltalk",           # 闲聊兜底
]


class IntentResult(BaseModel):
    intent: IntentKind
    confidence: float = Field(ge=0.0, le=1.0, default=0.6)
    reason: str = ""


# --------------------------------------------------------------------------
# 诉求（槽位）
# --------------------------------------------------------------------------
class Requirement(BaseModel):
    """从自然语言里抽出的预约诉求。

    所有字段可空：空即「还缺信息」，交给 Agent 决定是追问还是先试算。
    """

    model_config = ConfigDict(extra="ignore")

    date: dt.date | None = None
    start: dt.time | None = None
    end: dt.time | None = None
    duration_hours: float | None = None
    equipment_name: str | None = None
    category: str | None = None
    lab_hint: str | None = None
    capacity: int | None = None
    purpose: str = ""

    @field_validator("date", mode="before")
    @classmethod
    def _coerce_date(cls, v):
        if isinstance(v, str):
            return parse_date(v)
        return v

    @field_validator("start", "end", mode="before")
    @classmethod
    def _coerce_time(cls, v):
        if isinstance(v, str):
            return parse_time(v)
        return v

    # ---- 归一化与缺失判定 ------------------------------------------------

    def normalized(self) -> Requirement:
        """把「时长」与「起止时间」补齐成一致形态。"""
        start, end, duration = self.start, self.end, self.duration_hours
        if start and not end and duration:
            total = start.hour * 60 + start.minute + int(duration * 60)
            end = dt.time(hour=(total // 60) % 24, minute=total % 60)
        if start and end and not duration:
            duration = minutes_between(start, end) / 60 if end > start else None
        return self.model_copy(update={"start": start, "end": end, "duration_hours": duration})

    def missing_slots(self) -> list[tuple[str, str]]:
        """返回 [(字段名, 该问用户的话)]。

        注意设备/类别只需要**有一个**，因为「随便哪台光谱仪都行」是合理诉求；
        而日期与时间窗是硬必需的，缺了没法算任何候选。
        """
        miss: list[tuple[str, str]] = []
        cur = self.normalized()
        if cur.date is None:
            miss.append(("date", "你想约哪一天？"))
        if cur.start is None or cur.end is None:
            miss.append(("time_window", "大概几点到几点？（只说起始时间也行，我按设备的单次最长时间补足）"))
        if not cur.equipment_name and not cur.category:
            miss.append(("equipment", "有指定的设备或设备类别吗？（比如「光谱仪」「细胞培养箱」，也可以说不限）"))
        return miss

    def is_complete(self) -> bool:
        return not self.missing_slots()

    def summary(self) -> str:
        cur = self.normalized()
        bits: list[str] = []
        if cur.date:
            bits.append(cur.date.isoformat())
        if cur.start and cur.end:
            bits.append(f"{cur.start.strftime('%H:%M')}-{cur.end.strftime('%H:%M')}")
        target = cur.equipment_name or cur.category or "任意设备"
        bits.append(target)
        if cur.capacity:
            bits.append(f"{cur.capacity}人")
        return " · ".join(bits)


# --------------------------------------------------------------------------
# 约束检查
# --------------------------------------------------------------------------
ConstraintName = Literal[
    "equipment_status",  # 设备是否可用（非维护 / 报废）
    "training",          # 用户是否具备该类别准入资质
    "open_hours",        # 是否落在实验室开放时间内
    "max_hours",         # 是否超出单次最长时长
    "capacity",          # 房间容量是否够
    "conflict",          # 是否与已有预约重叠
]


class ConstraintCheck(BaseModel):
    name: ConstraintName
    passed: bool
    detail: str


# --------------------------------------------------------------------------
# 候选与备选方案
# --------------------------------------------------------------------------
class AvailabilityCandidate(BaseModel):
    """一个「完整满足原诉求」的可用时段。"""

    equipment_id: int
    equipment_name: str
    equipment_code: str
    category: str
    lab_label: str
    date: dt.date
    start: dt.time
    end: dt.time
    hours: float


ProposalKind = Literal["exact", "shift", "shorten", "switch_equipment", "switch_lab"]

# 「为什么约不到」的分类：
#   none        没卡住（有精确解）
#   no_date     还没定日期，算不了
#   no_target   没有匹配的设备
#   constraint  被某条约束卡住（细节看 checks）
BlockerKind = Literal["none", "no_date", "no_target", "constraint"]

# 硬约束：换时间、换时长、换同类设备都绕不过去，只能去「办手续」。
# 与它相对的是 conflict / open_hours / max_hours —— 那些换个条件确实能解决。
HARD_CONSTRAINTS = frozenset({"training", "equipment_status"})


class Proposal(BaseModel):
    """一个可用方案。``relaxations`` 是相对原始诉求放宽了哪些约束。"""

    kind: ProposalKind
    equipment_id: int
    equipment_name: str
    lab_label: str
    date: dt.date
    start: dt.time
    end: dt.time
    hours: float
    relaxations: list[str] = Field(default_factory=list)
    score: float = Field(ge=0.0, le=1.0, default=1.0)
    reason: str = ""

    @property
    def is_exact(self) -> bool:
        return not self.relaxations

    def label(self) -> str:
        return (
            f"{self.date.isoformat()} "
            f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')} "
            f"{self.equipment_name}（{self.lab_label}）"
        )


class NegotiationResult(BaseModel):
    """协商结果：能全满足就给候选，满足不了就给取舍后的备选。"""

    satisfied: bool
    checks: list[ConstraintCheck] = Field(default_factory=list)
    proposals: list[Proposal] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    # 「为什么不行」的**分类**，供回复层选对策。
    # 只有把它结构化了，回复才不会千篇一律地劝人「换个时间」——
    # 缺资质这种事，换一百个时间也没用，应该让人先去培训。
    blocker_kind: BlockerKind = "none"

    def primary(self) -> Proposal | None:
        return self.proposals[0] if self.proposals else None


# --------------------------------------------------------------------------
# 检索（安全规范）
# --------------------------------------------------------------------------
class DocHit(BaseModel):
    source: str
    heading: str
    text: str
    score: float
    # 命中来源：bm25 / vector / both —— 融合后能看出是哪几路共同支撑的
    matched_by: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# 预约输出
# --------------------------------------------------------------------------
class ReservationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    equipment_id: int
    equipment_name: str = ""
    lab_label: str = ""
    date: dt.date
    start_time: dt.time
    end_time: dt.time
    status: str
    purpose: str = ""
    slot: str = ""


# 下单/取消的**机器可读**结果分类。刻意只有这七个：
# 指标要按它分组，而"分类太细"和"分类错了"一样会让图表失去意义。
BookingReason = Literal[
    "ok",          # 成功
    "not_found",   # 用户 / 设备 / 预约不存在
    "invalid",     # 参数或业务规则不过（资质、开放时间、粒度不对齐、时间已过去）
    "forbidden",   # 越权（想动别人的预约）
    "state",       # 当前状态不允许该操作（已取消 / 已完成 / 自己已是该状态）
    "conflict",    # 目标时段已被占用（复检时发现的：坑没了）
    "contention",  # 并发争抢，重试到上限仍未成功（系统承压的信号）
]


class BookingOutcome(BaseModel):
    """下单/取消的结果。``retries`` 暴露乐观并发重试了几次，评测与压测都看这个数。

    ``reason`` 是**机器可读**的结果分类（P1-4 加的），与 ``message`` 分工明确：
    message 给人看、可以随便改文案；reason 给指标分组、**不能**靠解析 message 得到。

    为什么值得单开一个字段：抓"从人话里找关键词"当分类依据，是典型的
    「改一句文案，指标就静默错位」—— 而且它不会报错，只会让图上的数字
    慢慢变得没有意义。这种错误发现得极晚，代价却是整条可观测性链路。
    """

    ok: bool
    message: str
    reservation: ReservationOut | None = None
    retries: int = 0
    conflict_with: ReservationOut | None = None
    reason: BookingReason | None = None

    @property
    def outcome_label(self) -> str:
        """给指标用的分组标签。

        ``reason`` 没填时兜底成 ``unknown`` 而**不是** ``ok``：一个漏了分类的
        返回点应该在指标上露出来，而不是混进"成功"里被永久掩盖。
        """
        if self.reason:
            return self.reason
        return "ok" if self.ok else "unknown"


# --------------------------------------------------------------------------
# 认证（P0-2）
# --------------------------------------------------------------------------
class LoginRequest(BaseModel):
    """登录请求。用户名 + 口令，别无其他。"""

    # 严格模式：未知字段直接 422。
    # 默认的 pydantic 行为是**静默忽略**未知字段，那会让拼错的参数
    # （比如 password_hash、role）看起来"请求成功了"，实际没生效。
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    """对外暴露的用户视图。

    **刻意不含 password_hash / email**：身份信息按需最小化，
    列表接口没有任何理由返回口令哈希（哪怕它不可逆）。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: str
    certs: list[str] = Field(default_factory=list)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class AuditLogOut(BaseModel):
    """审计记录视图（管理员可读）。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: dt.datetime
    actor_id: int | None = None
    actor_name: str = ""
    action: str
    target_type: str = ""
    target_id: str = ""
    outcome: str
    detail: str = ""
    client_host: str = ""


# --------------------------------------------------------------------------
# 对话
# --------------------------------------------------------------------------
class ChatRequest(BaseModel):
    # 严格模式：未知字段 422。
    # P0-2 之后前端不再传 user_id，但如果有人还在传，
    # 这里会**明确报错**而不是静默忽略 —— 静默忽略会让人以为"身份传进去了"。
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    # ⚠️ 这里**不是**身份来源。HTTP 层一律用 token 解出的 user_id 覆盖它
    # （见 api.chat），请求体里传什么都不作数。
    # 之所以保留字段：CLI（cli._chat）与离线评测（evaluation.run_eval）
    # 是在进程内直接调 Agent 的，没有 token 可解，必须显式传身份。
    # 缺省 None 而不是 1 —— 曾经默认 1 号用户，等于「不传就是 1 号」，
    # 是个静默的越权入口。
    user_id: int | None = None
    # 会话号进内存字典当 key，必须有长度上限：否则一个超长 session_id
    # 就能在 SessionStore 里塞进一条几 MB 的记录（自己给自己制造内存压力）。
    session_id: str = Field(default="default", min_length=1, max_length=64)
    # 用户点选备选方案时回传，避免让模型重新推断
    accept_equipment_id: int | None = None
    accept_date: dt.date | None = None
    accept_start: dt.time | None = None
    accept_end: dt.time | None = None


class TraceStep(BaseModel):
    """一步可观测记录。控制台直接渲染这个，不靠日志里翻。"""

    node: str
    detail: str
    elapsed_ms: float = 0.0


class ChatResponse(BaseModel):
    reply: str
    intent: IntentKind | None = None
    stage: str = ""
    missing: list[str] = Field(default_factory=list)
    proposals: list[Proposal] = Field(default_factory=list)
    citations: list[DocHit] = Field(default_factory=list)
    booking: BookingOutcome | None = None
    trace: list[TraceStep] = Field(default_factory=list)
    # Agent 被降级（模型不可用 / 关闭）时为 True，前端据此改写提示
    degraded: bool = False


class AccessVerifyRequest(BaseModel):
    """门禁机发起的一次核验请求。

    ``credential``（二维码 / 卡片内的凭证串）与 ``user_id``（刷卡或人脸识别出的
    身份）**至少要有一个**：前者是"你带了什么"，后者是"你是谁"。
    两个都给时会被交叉校验 —— 这正是防"拿别人截图进门"的关键一步。

    严格模式（``extra="forbid"``）：门禁集成最容易出的错是字段名写错却被静默忽略，
    结果门禁以为在传身份、后端根本没收到，于是所有人都被当成无凭证拒掉。
    """

    model_config = ConfigDict(extra="forbid")

    lab_id: int
    credential: str | None = Field(default=None, max_length=512)
    user_id: int | None = None
    gate_id: str = Field(default="", max_length=32)
    direction: str = Field(default="in", pattern="^(in|out)$")
    # 预检：只判定不落库，用于门禁屏上"你随时可以进"的提示
    precheck: bool = False


class AccessVerifyResponse(BaseModel):
    """核验结论。**带原因码**，门禁屏据此显示"为什么不开门"。"""

    granted: bool
    reason_code: str = ""
    message: str = ""
    permit_id: int | None = None
    user_id: int | None = None
    user_name: str = ""
    lab_id: int | None = None
    lab_label: str = ""


class InsideEntry(BaseModel):
    """在馆名单里的一条。"""

    permit_id: int
    user_id: int
    username: str
    lab_id: int
    lab_label: str
    valid_from: str
    valid_to: str
    checked_in_at: str | None = None


class AccessIssueRequest(BaseModel):
    """手工签发一张凭证（访客、临时人员、忘记预约的补救）。

    有副作用的动作，只允许管理员调用，且**必须写明理由** ——
    "谁能绕过预约流程"这件事本身要留痕。
    """

    model_config = ConfigDict(extra="forbid")

    user_id: int
    lab_id: int
    date: dt.date
    valid_from: dt.time
    valid_to: dt.time
    reason: str = Field(default="", max_length=200)


class AccessIssueResponse(BaseModel):
    permit_id: int
    user_id: int
    lab_id: int
    date: dt.date
    valid_from: dt.time
    valid_to: dt.time
    # 明文凭证**只在签发时返回这一次**，系统只留哈希
    credential: str
    required_certs: list[str] = Field(default_factory=list)


class CancelRequest(BaseModel):
    """取消预约。

    ``user_id`` 已从请求体里**删除** —— 取消者身份一律取自 token。
    需要管理员代他人取消时用 ``as_user_id``，且该字段对非管理员返回 403
    （显式拒绝，而不是静默忽略）。
    """

    # 严格模式：还按老接口传 user_id 的人会拿到明确的 422，
    # 而不是"请求成功了但取消的是自己"这种更难排查的结果。
    model_config = ConfigDict(extra="forbid")

    reservation_id: int
    reason: str = Field(default="", max_length=200)
    as_user_id: int | None = None
