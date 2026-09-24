"""数据模型：用户 / 实验室 / 设备 / 预约 / 时段占用。

本文件承载整个项目最核心的工程不变式 —— 它被刻意**下沉到数据库层**，
而不是靠应用层「先查有没有冲突，再写入」的自觉：

不变式 1（同坑不重占）
    同一设备、同一天、同一开始时间的「有效预约」最多一条。
    由部分唯一索引 ``uq_res_active_slot`` 保证，条件是
    ``status IN ('pending','confirmed')``。
    之所以是「部分」索引：已取消 / 已过期的记录不该继续占着坑位，
    否则取消之后这个时段就永远订不回来了。

不变式 2（区间不重叠）★
    同一设备的任意两条有效预约，时间区间不得相交。
    **这条不变式不能靠 (equipment_id, date, start_time) 唯一索引实现** ——
    ``13:00-15:00`` 与 ``14:00-16:00`` 开始时间不同，索引看不见它们重叠。
    实测过：20 并发下这两条会双双写入（超卖）。

    正确做法是把「区间」拆成「不可再分的格」，让重叠变成**同一格被占两次**：
    预约按 ``slot_granularity_minutes``（默认 30 分钟）展开成若干条
    ``ReservationSlot``，唯一索引打在 ``(equipment_id, date, slot_index)`` 上。
    这样任何形式的相交（部分重叠 / 包含 / 完全相同）都必然撞唯一索引，
    且**与数据库种类无关** —— SQLite 与 PostgreSQL 行为一致。

    为什么不用 PostgreSQL 的 ``EXCLUDE USING gist (..., tsrange(...) WITH &&)``：
    那是 PG 上更优雅的解法，但 SQLite 没有等价物，本地开发就跑不起来。
    占用表是「一次实现，两种库都对」的取舍，代价是多一张表。
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .clock import now_local

# 预约状态机：待确认 → 已确认 → 已完成
#                    ↘ 已取消
#                    ↘ 已过期（到时间未使用）
STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_CANCELLED = "cancelled"
STATUS_COMPLETED = "completed"
STATUS_EXPIRED = "expired"

# 「有效」= 占据资源的状态。只有这两种状态参与冲突判定。
ACTIVE_STATUSES: tuple[str, ...] = (STATUS_PENDING, STATUS_CONFIRMED)

# 取值域写成类型而不是散落的字符串字面量（P0-4）：
# 后台接口要**接受**客户端给的状态与角色，如果不把取值域钉住，
# 「状态拼错成 'Normal'」会被静默写进库，设备从此既不是 normal 也不是
# maintenance —— 于是它会被"设备不可预约"这条规则**永久**拦下，
# 而界面上看起来一切正常。这类"写错了但没人报错"是运维最难查的一类故障。
#
# 写法上的弯弯绕绕是 mypy 逼出来的：它不接受 ``Literal[某 str 常量]``，
# 所以字面量只能直接写在这两行里；而常量要继续能给 pydantic 当默认值用，
# 就反过来把常量**标注成**上面这两个类型 —— 于是"常量与取值域不一致"
# 会变成类型错误，而不是等到运行时才发现。
EquipmentStatus = Literal["normal", "maintenance", "scrapped"]
UserRole = Literal["user", "admin", "sysadmin"]

EQUIPMENT_NORMAL: EquipmentStatus = "normal"
EQUIPMENT_MAINTENANCE: EquipmentStatus = "maintenance"
EQUIPMENT_SCRAPPED: EquipmentStatus = "scrapped"

ROLE_USER: UserRole = "user"
ROLE_ADMIN: UserRole = "admin"
ROLE_SYSADMIN: UserRole = "sysadmin"

EQUIPMENT_STATUSES: tuple[str, ...] = (
    EQUIPMENT_NORMAL,
    EQUIPMENT_MAINTENANCE,
    EQUIPMENT_SCRAPPED,
)
USER_ROLES: tuple[str, ...] = (ROLE_USER, ROLE_ADMIN, ROLE_SYSADMIN)

# ---------------------------------------------------------------------------
# 人员准入（门禁）：让「进实验室」这件事从"没人管"变成可判定、可追溯
#
# 与设备预约的关系：设备预约回答「这台仪器这个时段归谁」，
# 准入回答「这个人此刻能不能进这个房间」。两者是不同的资源 ——
# 10 个人可以各自约到 10 台不同设备，但房间同时塞不下 10 个人。
# 所以房间需要**自己的一条不变式**（见 LabOccupancy），不能蹭设备的。
#
# 主路径是「一张设备预约派生一张入室凭证」：用户只需要做一件事，
# 准入能力是他已有的预约换来的，而不是多填一张表。
# ---------------------------------------------------------------------------

# 凭证状态机：已签发 → 已入场 → 已使用（出门后不可再用）
#                     ↘ 已过期（到 valid_to 仍未入场）
#                     ↘ 已撤销（管理员 / 事故停权）
PERMIT_ISSUED = "issued"
PERMIT_CHECKED_IN = "checked_in"
PERMIT_USED = "used"
PERMIT_EXPIRED = "expired"
PERMIT_REVOKED = "revoked"

# 凭证来源：设备预约派生 / 独立申请（只进房间不占设备）/ 管理员代发（访客、陪同）
PERMIT_SOURCE_RESERVATION = "reservation"
PERMIT_SOURCE_STANDALONE = "standalone"
PERMIT_SOURCE_ADMIN = "admin_grant"

DIRECTION_IN = "in"
DIRECTION_OUT = "out"

# 进入**任何**实验室都需要的资质。它与设备类别资质是两件事：
# 设备资质回答"你会不会操作这台仪器"，这一项回答"你知不知道这间屋子的规矩"。
LAB_BASIC_CERT = "实验室安全"

ACCESS_GRANTED = "granted"
ACCESS_DENIED = "denied"

# 拒绝原因码。**每一次拒绝都必须落到一个码上** ——
# 门禁屏要显示"为什么不开门"，事后也要能统计"哪种拒绝最多"。
# 只回一句"验证失败"的门禁，值班人员只能站在门口猜。
DENY_NO_PERMIT = "no_permit"
DENY_PERMIT_USED = "permit_used"
DENY_PERMIT_REVOKED = "permit_revoked"
DENY_PERMIT_EXPIRED = "permit_expired"
DENY_NOT_YET = "not_yet_valid"
DENY_WRONG_LAB = "wrong_lab"
DENY_IDENTITY_MISMATCH = "identity_mismatch"
DENY_CERT_MISSING = "cert_missing"
DENY_CERT_EXPIRED = "cert_expired"
DENY_CERT_REVOKED = "cert_revoked"
DENY_LAB_FULL = "lab_full"
DENY_ALREADY_INSIDE = "already_inside"
DENY_NO_SUCH_USER = "no_such_user"
DENY_CONFLICT = "concurrent_conflict"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(128), unique=True)
    role: Mapped[str] = mapped_column(String(16), default=ROLE_USER)
    # 口令哈希（格式 scrypt$n$r$p$salt$hash，见 security.py）。
    # 默认空串是有意的：历史数据/未初始化账号的哈希为空，
    # verify_password 对空串一律返回 False，因此**默认不可登录**（fail-closed）。
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    # 准入资质：拥有哪些设备类别的操作资格（如 ["光谱", "细胞培养"]）。
    # 注意这是「代码层判定」的输入，绝不允许让大模型自己决定用户有没有资质。
    certs: Mapped[list] = mapped_column(JSON, default=list)

    reservations: Mapped[list[Reservation]] = relationship(back_populates="user")


class Laboratory(Base):
    __tablename__ = "laboratories"

    id: Mapped[int] = mapped_column(primary_key=True)
    building: Mapped[str] = mapped_column(String(32))
    floor: Mapped[int] = mapped_column(Integer)
    room: Mapped[str] = mapped_column(String(32))
    capacity: Mapped[int] = mapped_column(Integer, default=1)
    # {"weekday": ["08:00", "22:00"], "weekend": ["09:00", "18:00"]}
    open_hours: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(Text, default="")

    equipment: Mapped[list[Equipment]] = relationship(back_populates="lab")

    @property
    def label(self) -> str:
        return f"{self.building}{self.floor}楼{self.room}"


class Equipment(Base):
    __tablename__ = "equipment"

    id: Mapped[int] = mapped_column(primary_key=True)
    lab_id: Mapped[int] = mapped_column(ForeignKey("laboratories.id"), index=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(64), default="")
    code: Mapped[str] = mapped_column(String(32), unique=True)
    # 设备类别，与 User.certs 的取值域一致，用于资质校验
    category: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), default=EQUIPMENT_NORMAL)
    # 单次最长可约时长（小时）
    max_hours: Mapped[int] = mapped_column(Integer, default=4)
    requires_training: Mapped[bool] = mapped_column(Boolean, default=False)
    # 这台设备的预约是否需要管理员审批（P1-5）。
    # 默认 False：审批是**按设备开通**的能力 —— 否则这一版上线后所有设备
    # 一夜之间都变成"要等管理员点一下"，而院系并没有为此配人。
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)

    lab: Mapped[Laboratory] = relationship(back_populates="equipment")
    reservations: Mapped[list[Reservation]] = relationship(back_populates="equipment")


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        # 不变式 1：同设备 + 同日期 + 同开始时间，「有效」预约唯一。
        # 用部分索引而不是普通唯一约束，是为了让「取消后能重新预约」成立。
        Index(
            "uq_res_active_slot",
            "equipment_id",
            "date",
            "start_time",
            unique=True,
            sqlite_where=text("status IN ('pending','confirmed')"),
            postgresql_where=text("status IN ('pending','confirmed')"),
        ),
        # 冲突查询的走查索引：按设备 + 日期捞出当天所有预约。
        Index("ix_res_equipment_date", "equipment_id", "date"),
        # 按用户查自己的预约
        Index("ix_res_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    equipment_id: Mapped[int] = mapped_column(ForeignKey("equipment.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date)
    start_time: Mapped[dt.time] = mapped_column(Time)
    end_time: Mapped[dt.time] = mapped_column(Time)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_CONFIRMED)
    purpose: Mapped[str] = mapped_column(Text, default="")
    cancel_reason: Mapped[str] = mapped_column(Text, default="")
    # 乐观锁版本号：取消 / 改期时带上读到的版本，避免覆盖别人的并发修改。
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now_local)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=now_local, onupdate=now_local
    )

    user: Mapped[User] = relationship(back_populates="reservations")
    equipment: Mapped[Equipment] = relationship(back_populates="reservations")
    # 占位格：取消时删除，改期时重建。cascade 保证删预约不留孤儿格。
    slots: Mapped[list[ReservationSlot]] = relationship(
        back_populates="reservation", cascade="all, delete-orphan"
    )

    @property
    def slot_label(self) -> str:
        return (
            f"{self.date.isoformat()} "
            f"{self.start_time.strftime('%H:%M')}-{self.end_time.strftime('%H:%M')}"
        )

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES


# --------------------------------------------------------------------------
# 时段占用格：把「区间不重叠」变成「同一格不被占两次」
# --------------------------------------------------------------------------
# 最小预约粒度（分钟）。必须与 config.slot_granularity_minutes 的默认值一致；
# 单独在这里定义一份是因为 models 不该反向依赖 config（config 会被测试替换）。
DEFAULT_SLOT_GRANULARITY_MINUTES = 30


def slot_index_of(value: dt.time, granularity: int = DEFAULT_SLOT_GRANULARITY_MINUTES) -> int:
    """把时间点映射成「当天第几格」，以 00:00 为原点。

    ⚠️ 只有**对齐**的时间点（分钟数是 granularity 的整数倍）才是精确的。
    未对齐的时间点会被向下取整，导致区间漏保护 ——
    所以调用方必须先做对齐校验，见 domain/booking.py 的 ``_ensure_aligned``。
    """
    return (value.hour * 60 + value.minute) // granularity


def is_aligned(value: dt.time, granularity: int = DEFAULT_SLOT_GRANULARITY_MINUTES) -> bool:
    return (value.hour * 60 + value.minute) % granularity == 0


def slot_indexes_for(
    start: dt.time,
    end: dt.time,
    granularity: int = DEFAULT_SLOT_GRANULARITY_MINUTES,
) -> list[int]:
    """区间 [start, end) 覆盖的格索引。

    刻意用**左闭右开**：14:00-16:00 与 16:00-18:00 首尾相接但不相交，
    不该互相冲突。这是预约系统里最容易写错的一处边界。
    """
    return list(range(slot_index_of(start, granularity), slot_index_of(end, granularity)))


class ReservationSlot(Base):
    """一条预约所占的 30 分钟格。唯一索引是「区间不重叠」的**唯一**保证。

    它是一张纯保护性的表：不参与业务展示，只为让数据库能独立判定重叠。
    预约创建时按区间写入若干行，取消时删除；改期 = 先删旧格再写新格。

    设计取舍见本模块 docstring「不变式 2」。
    """

    __tablename__ = "reservation_slots"
    __table_args__ = (
        # ★ 核心不变式：同一设备、同一天、同一格，全局只能被占一次。
        # 任何形式的区间相交都会在这里撞车，与数据库种类无关。
        Index("uq_equipment_slot", "equipment_id", "date", "slot_index", unique=True),
        Index("ix_res_slot_reservation", "reservation_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int] = mapped_column(
        ForeignKey("reservations.id", ondelete="CASCADE"), index=True
    )
    equipment_id: Mapped[int] = mapped_column(ForeignKey("equipment.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date)
    slot_index: Mapped[int] = mapped_column(Integer)

    reservation: Mapped[Reservation] = relationship(back_populates="slots")


# --------------------------------------------------------------------------
# 审计日志：只追加，不修改、不删除
# --------------------------------------------------------------------------
# 动作名用常量而不是散落的字符串字面量：审计的可查询性完全取决于
# 取值是否收敛，一个拼错的 "login_fialed" 会让统计口径永远对不上。
ACTION_LOGIN = "auth.login"
ACTION_LOGIN_FAILED = "auth.login_failed"
ACTION_BOOK = "reservation.create"
ACTION_CANCEL = "reservation.cancel"
# P1-5：管理员通过/驳回一条申请
ACTION_REVIEW = "reservation.review"
ACTION_ADMIN_READ = "admin.read"
# P0-4：后台维护的**写**操作。与 admin.read 分开是必要的 ——
# "谁看了花名册"和"谁把某人的管理员权限改了"是完全不同的两件事，
# 混在一个动作里就只能在出事后把所有读过的人都排查一遍。
ACTION_ADMIN_WRITE = "admin.write"
# 后台清扫的动作名。它们**必须**与用户动作分开命名 ——
# 「谁改了什么业务数据」这条审计里，值班人员最想一眼分清
# 「是用户自己出的门」还是「是系统到点替他收的尾」。
# 混用同一个动作名会让这条区分永远丢失。
ACTION_SWEEP_RESERVATION_EXPIRED = "sweep.reservation_expired"
ACTION_SWEEP_FORCE_CHECKOUT = "sweep.force_checkout"
ACTION_SWEEP_ARCHIVED = "sweep.archived"

OUTCOME_OK = "ok"
OUTCOME_DENIED = "denied"
OUTCOME_FAILED = "failed"


class AuditLog(Base):
    """谁、何时、对什么、做了什么、结果如何。

    刻意保留**失败与拒绝**的记录：只记成功的话，「有人一直在试别人的预约号」
    这种最该被发现的模式恰好什么都看不到。
    也刻意不存口令、token 原文 —— 审计表本身不该成为新的泄露面。
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        # 常见查询：按时间倒序翻页 / 按人筛选 / 按动作统计
        Index("ix_audit_created", "created_at"),
        Index("ix_audit_actor", "actor_id", "created_at"),
        Index("ix_audit_action", "action", "created_at"),
        # 「这一个请求到底做了什么」——排障时最常用的一次查询
        Index("ix_audit_request", "request_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now_local)
    # 贯穿一条链路的关联 id（P1-3）。来自 obs.current_request_id()：
    # HTTP 请求由中间件绑定，后台清扫每轮绑定一个，命令行/播种留空。
    # 用空串而不是 NULL —— 空串是"没有请求上下文"的确定表示，
    # 而 NULL 会让 `WHERE request_id = ''` 查不到这些行。
    request_id: Mapped[str] = mapped_column(String(36), default="", server_default="")
    # 允许为空：登录失败时还没有身份（这正是要记下来的一类事件）
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_name: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(48))
    target_type: Mapped[str] = mapped_column(String(32), default="")
    target_id: Mapped[str] = mapped_column(String(64), default="")
    outcome: Mapped[str] = mapped_column(String(16), default=OUTCOME_OK)
    detail: Mapped[str] = mapped_column(Text, default="")
    # 来源 IP。反向代理后面需要在网关上把真实来源写进 X-Forwarded-For 才有意义，
    # 这里明确只记后端看到的地址，不假装它是客户端真实 IP。
    client_host: Mapped[str] = mapped_column(String(64), default="")


# ===========================================================================
# 人员准入：资质（带有效期） / 入室凭证 / 房间在场占位 / 通行事件
# ===========================================================================
class CertGrant(Base):
    """一张**带有效期**的资质授权。

    为什么不能继续用 ``User.certs`` 那个裸字符串列表：

    1. 它回答不了「这张培训证**还有效**吗」。高校实验室准入里培训有效期是硬门槛 ——
       三年前考过的离心机操作证不等于今天还能用，而这正是事故的常见来源；
    2. 它没法吊销。违纪、出事故之后要停权，裸列表只能整条删掉，
       连「曾经授过、后因何事撤销」都留不下来；
    3. 它没法追溯。没有授予日期与依据，出了事无法回答「他是凭什么进来的」。

    ``expires_at`` 与 ``revoked_at`` 是两条独立的失效路径，判定时必须都看：
    过期是「到点了」，撤销是「被停了」，给用户的提示语完全不同（该复训 / 该找管理员）。
    """

    __tablename__ = "cert_grants"
    __table_args__ = (
        # 同一人、同一类别、同一天只应有一条授权记录；复训换证是新的 granted_at。
        Index("uq_cert_user_category_grant", "user_id", "category", "granted_at", unique=True),
        # 进场核验的走查索引：按人 + 类别捞有效授权。
        Index("ix_cert_user_category", "user_id", "category"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)
    granted_at: Mapped[dt.date] = mapped_column(Date)
    expires_at: Mapped[dt.date] = mapped_column(Date)
    # 培训考核单 / 证书编号，出事后可回溯到纸面依据
    evidence: Mapped[str] = mapped_column(String(128), default="")
    # 非空 = 该授权已被撤销（值即撤销时间）。保留记录而不是删除，为了可追溯。
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")


class EntryPermit(Base):
    """入室凭证：允许某人在某个时间窗内进入某个实验室。

    **单次核销是这张表的核心语义。** 凭证不是「身份标识」，而是「一次性通行权」：
    如果允许重复使用，一张二维码截图转发给十个人就能让十个人进门 ——
    这是门禁系统最经典也最容易被忽略的漏洞。
    所以状态机是 ``issued → checked_in → used``，``checked_in_at`` 一旦写上就不再回头。

    凭证串本身**只存哈希**（``credential_hash``）。理由和口令一样：
    库被拖走时明文凭证等于给所有人发了通行证。核验时比对哈希，不需要原文。
    """

    __tablename__ = "entry_permits"
    __table_args__ = (
        # 凭证串全局唯一：同一个凭证不可能同时属于两张凭证
        Index("uq_permit_credential", "credential_hash", unique=True),
        # ★ 同一个人同一时刻只能「在馆中」一条。
        # 用部分唯一索引表达「只在 checked_in 这个状态下成立」——
        # 没有它，一个人可以凭两条凭证同时出现在两个房间，
        # 在馆人数统计就会把他算两遍，容量约束随之失真。
        Index(
            "uq_permit_one_inside",
            "user_id",
            unique=True,
            sqlite_where=text("status = 'checked_in'"),
            postgresql_where=text("status = 'checked_in'"),
        ),
        Index("ix_permit_user_date", "user_id", "date"),
        Index("ix_permit_lab_date_status", "lab_id", "date", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    lab_id: Mapped[int] = mapped_column(ForeignKey("laboratories.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date)
    # 有效时间窗。核验时允许提前一点入场（宽限），但绝不允许超时入场。
    valid_from: Mapped[dt.time] = mapped_column(Time)
    valid_to: Mapped[dt.time] = mapped_column(Time)
    status: Mapped[str] = mapped_column(String(16), default=PERMIT_ISSUED, index=True)
    source: Mapped[str] = mapped_column(String(24), default=PERMIT_SOURCE_RESERVATION)
    # 派生自哪条设备预约（独立申请 / 管理员代发时为空）
    reservation_id: Mapped[int | None] = mapped_column(
        ForeignKey("reservations.id"), nullable=True, index=True
    )
    credential_hash: Mapped[str] = mapped_column(String(64))
    # 入场所需的资质类别，**签发时冻结**。
    # 为什么冻结而不是进门时再算：要求是"当时被批准的条件"，
    # 之后设备改了 requires_training、或房间换了规则，不该追溯性地改变
    # 一张已发出凭证的合法条件。而**有效性**（是否过期/被撤销）必须在
    # 进门那一刻重新算 —— 所以冻结的是"要哪些类别"，不是"资质本身有没有效"。
    required_certs: Mapped[list] = mapped_column(JSON, default=list)
    issued_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now_local)
    checked_in_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    checked_out_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # 从哪台门禁进出的。多门实验室里，事后能回答「他从哪个门进来的」。
    gate_in: Mapped[str] = mapped_column(String(32), default="")
    gate_out: Mapped[str] = mapped_column(String(32), default="")


class LabOccupancy(Base):
    """房间在场占位：把「容量」这条不变式下沉到数据库。★

    **为什么不能只用一条「当前人数」计数列**：那需要「读出来 → 加一 → 写回去」，
    两个并发请求会读到同一个旧值、各自加一，最终人数少算 ——
    这正是本项目在设备预约上踩过的同一个坑（``check-then-act`` 在 SQLite 上
    退化为无锁）。计数列在并发下**必然**失真，房间会被塞爆。

    正确做法是把「同时最多 N 人」翻译成「N 个座位」：
    房间的每个时间格有 capacity 个座位，每人入场时占一个座位，
    唯一索引打在 ``(lab_id, date, slot_index, seat)`` 上。
    这样「超容量」就变成了「同一个座位被占两次」—— 由数据库判定；
    应用层只负责试着占座、撞了就换一个座位，换不到就是满了。

    与 ``ReservationSlot`` 是同一套手法，只是多了一维 ``seat``：
    设备是「一个坑一个人」，房间是「N 个坑各一个人」。
    """

    __tablename__ = "lab_occupancy"
    __table_args__ = (
        # ★ 核心不变式：同一实验室、同一天、同一格、同一座位，全局只能被占一次。
        Index("uq_lab_slot_seat", "lab_id", "date", "slot_index", "seat", unique=True),
        Index("ix_lab_occ_permit", "permit_id"),
        Index("ix_lab_occ_lab_date", "lab_id", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    lab_id: Mapped[int] = mapped_column(ForeignKey("laboratories.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date)
    slot_index: Mapped[int] = mapped_column(Integer)
    # 座位号，取值 0 .. capacity-1。它不代表物理座位，只是把容量离散化的槽口。
    seat: Mapped[int] = mapped_column(Integer)
    permit_id: Mapped[int] = mapped_column(
        ForeignKey("entry_permits.id", ondelete="CASCADE"), index=True
    )


class AccessEvent(Base):
    """通行事件流水：**只追加**，进门、出门、被拒都记。

    与 ``AuditLog`` 分开的理由是频率与用途不同：审计记的是「谁改了什么业务数据」
    （低频、面向合规），这里记的是「谁什么时候站在哪个门口、开没开门」
    （高频、面向安全）。混在一张表里，翻审计时会被门禁流水淹掉。

    被拒事件尤其重要：它是「未预约者试图进入」的**唯一证据**。
    没有这张表，「限制未预约者进入」就只是一个说法，拿不出任何数据。
    """

    __tablename__ = "access_events"
    __table_args__ = (
        Index("ix_access_occurred", "occurred_at"),
        Index("ix_access_user_time", "user_id", "occurred_at"),
        Index("ix_access_lab_time", "lab_id", "occurred_at"),
        Index("ix_access_result", "result", "occurred_at"),
        # 与 audit_logs 同一个用途：拿一个 request_id 把门禁流水和审计串起来
        Index("ix_access_request", "request_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now_local, index=True)
    # 与 AuditLog.request_id 同义：能回答「这次刷卡是哪个请求触发的」
    request_id: Mapped[str] = mapped_column(String(36), default="", server_default="")
    # 允许为空：凭证无效时可能连「是谁」都还没识别出来（例如读卡失败）
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lab_id: Mapped[int] = mapped_column(ForeignKey("laboratories.id"), index=True)
    gate_id: Mapped[str] = mapped_column(String(32), default="")
    direction: Mapped[str] = mapped_column(String(8))
    result: Mapped[str] = mapped_column(String(16))
    # 拒绝原因码，见本模块顶部的 DENY_* 常量
    reason_code: Mapped[str] = mapped_column(String(32), default="")
    permit_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 不记明文凭证，只记它的短指纹；用于排查「是不是同一张截图被反复刷」
    credential_fingerprint: Mapped[str] = mapped_column(String(16), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
