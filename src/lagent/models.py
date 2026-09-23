"""数据模型：用户 / 实验室 / 设备 / 预约。

本文件承载整个项目最核心的工程不变式 —— 它被刻意**下沉到数据库层**，
而不是靠应用层「先查有没有冲突，再写入」的自觉：

不变式 1（同坑不重占）
    同一设备、同一天、同一开始时间的「有效预约」最多一条。
    由部分唯一索引 ``uq_res_active_slot`` 保证，条件是
    ``status IN ('pending','confirmed')``。
    之所以是「部分」索引：已取消 / 已过期的记录不该继续占着坑位，
    否则取消之后这个时段就永远订不回来了。

不变式 2（区间不重叠）
    时间区间重叠只能在事务里串行判定。READ COMMITTED 隔离级别下，
    两个并发事务可以各自查到「没冲突」然后双双写入 —— 这就是典型的
    check-then-act 漏判。因此下单前必须先取设备级锁，见 domain/booking.py。

两条不变式分别用「索引」与「锁」实现，都用并发测试真实打过。
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

    @property
    def slot_label(self) -> str:
        return (
            f"{self.date.isoformat()} "
            f"{self.start_time.strftime('%H:%M')}-{self.end_time.strftime('%H:%M')}"
        )

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES
