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

# 设备状态
EQUIPMENT_NORMAL = "normal"
EQUIPMENT_MAINTENANCE = "maintenance"
EQUIPMENT_SCRAPPED = "scrapped"

ROLE_USER = "user"
ROLE_ADMIN = "admin"
ROLE_SYSADMIN = "sysadmin"


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

    reservations: Mapped[list["Reservation"]] = relationship(back_populates="user")


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

    equipment: Mapped[list["Equipment"]] = relationship(back_populates="lab")

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

    lab: Mapped[Laboratory] = relationship(back_populates="equipment")
    reservations: Mapped[list["Reservation"]] = relationship(back_populates="equipment")


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
    slots: Mapped[list["ReservationSlot"]] = relationship(
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

    reservation: Mapped["Reservation"] = relationship(back_populates="slots")


# --------------------------------------------------------------------------
# 审计日志：只追加，不修改、不删除
# --------------------------------------------------------------------------
# 动作名用常量而不是散落的字符串字面量：审计的可查询性完全取决于
# 取值是否收敛，一个拼错的 "login_fialed" 会让统计口径永远对不上。
ACTION_LOGIN = "auth.login"
ACTION_LOGIN_FAILED = "auth.login_failed"
ACTION_BOOK = "reservation.create"
ACTION_CANCEL = "reservation.cancel"
ACTION_ADMIN_READ = "admin.read"

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
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now_local)
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
