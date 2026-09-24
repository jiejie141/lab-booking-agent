"""initial schema

初始结构：全部 10 张表（用户 / 实验室 / 设备 / 预约 / 占用格 / 审计 /
资质 / 凭证 / 房间占位 / 通行流水）。

本文件由 ``alembic revision --autogenerate`` 生成后**人工复核**过，
复核的重点是三处「靠唯一索引兜住并发」的部分索引，它们最容易在生成时丢条件：

  * ``uq_res_active_slot``   —— 仅在 ``status IN ('pending','confirmed')`` 上唯一，
    所以取消之后那个时段还能被订回来；
  * ``uq_permit_one_inside`` —— 仅在 ``status = 'checked_in'`` 上唯一，
    所以「同一人同一时刻只能在一个房间」；
  * ``uq_equipment_slot`` / ``uq_lab_slot_seat`` —— 无条件的唯一索引，
    它们是「区间不重叠」「容量不超额」两条不变式**唯一**的保证。

这几条一旦丢了 WHERE 子句，功能测试仍然会过（单线程下察觉不到），
只有并发压测才会暴露 —— 所以 tests/test_migrations.py 直接断言 DDL 文本。

Revision ID: 0001
Revises:
Create Date: 2026-09-24

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("actor_name", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("client_host", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.create_index("ix_audit_action", ["action", "created_at"], unique=False)
        batch_op.create_index("ix_audit_actor", ["actor_id", "created_at"], unique=False)
        batch_op.create_index("ix_audit_created", ["created_at"], unique=False)

    op.create_table(
        "laboratories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("building", sa.String(length=32), nullable=False),
        sa.Column("floor", sa.Integer(), nullable=False),
        sa.Column("room", sa.String(length=32), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("open_hours", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("certs", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_users_username"), ["username"], unique=True)

    op.create_table(
        "access_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("lab_id", sa.Integer(), nullable=False),
        sa.Column("gate_id", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=32), nullable=False),
        sa.Column("permit_id", sa.Integer(), nullable=True),
        sa.Column("credential_fingerprint", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["lab_id"], ["laboratories.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("access_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_access_events_lab_id"), ["lab_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_access_events_occurred_at"), ["occurred_at"], unique=False
        )
        batch_op.create_index("ix_access_lab_time", ["lab_id", "occurred_at"], unique=False)
        batch_op.create_index("ix_access_occurred", ["occurred_at"], unique=False)
        batch_op.create_index("ix_access_result", ["result", "occurred_at"], unique=False)
        batch_op.create_index("ix_access_user_time", ["user_id", "occurred_at"], unique=False)

    op.create_table(
        "cert_grants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("granted_at", sa.Date(), nullable=False),
        sa.Column("expires_at", sa.Date(), nullable=False),
        sa.Column("evidence", sa.String(length=128), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("cert_grants", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_cert_grants_category"), ["category"], unique=False)
        batch_op.create_index(batch_op.f("ix_cert_grants_user_id"), ["user_id"], unique=False)
        batch_op.create_index("ix_cert_user_category", ["user_id", "category"], unique=False)
        batch_op.create_index(
            "uq_cert_user_category_grant",
            ["user_id", "category", "granted_at"],
            unique=True,
        )

    op.create_table(
        "equipment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lab_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("max_hours", sa.Integer(), nullable=False),
        sa.Column("requires_training", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["lab_id"], ["laboratories.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    with op.batch_alter_table("equipment", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_equipment_category"), ["category"], unique=False)
        batch_op.create_index(batch_op.f("ix_equipment_lab_id"), ["lab_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_equipment_name"), ["name"], unique=False)

    op.create_table(
        "reservations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("equipment_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("cancel_reason", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["equipment_id"], ["equipment.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("reservations", schema=None) as batch_op:
        batch_op.create_index("ix_res_equipment_date", ["equipment_id", "date"], unique=False)
        batch_op.create_index("ix_res_user_status", ["user_id", "status"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_reservations_equipment_id"), ["equipment_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_reservations_user_id"), ["user_id"], unique=False)
        # ★ 不变式 1：仅「有效」预约参与唯一性，取消后时段要能订回来
        batch_op.create_index(
            "uq_res_active_slot",
            ["equipment_id", "date", "start_time"],
            unique=True,
            sqlite_where=sa.text("status IN ('pending','confirmed')"),
            postgresql_where=sa.text("status IN ('pending','confirmed')"),
        )

    op.create_table(
        "entry_permits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("lab_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("valid_from", sa.Time(), nullable=False),
        sa.Column("valid_to", sa.Time(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("reservation_id", sa.Integer(), nullable=True),
        sa.Column("credential_hash", sa.String(length=64), nullable=False),
        sa.Column("required_certs", sa.JSON(), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("checked_in_at", sa.DateTime(), nullable=True),
        sa.Column("checked_out_at", sa.DateTime(), nullable=True),
        sa.Column("gate_in", sa.String(length=32), nullable=False),
        sa.Column("gate_out", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["lab_id"], ["laboratories.id"]),
        sa.ForeignKeyConstraint(["reservation_id"], ["reservations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("entry_permits", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_entry_permits_lab_id"), ["lab_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_entry_permits_reservation_id"), ["reservation_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_entry_permits_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_entry_permits_user_id"), ["user_id"], unique=False)
        batch_op.create_index("ix_permit_lab_date_status", ["lab_id", "date", "status"], unique=False)
        batch_op.create_index("ix_permit_user_date", ["user_id", "date"], unique=False)
        batch_op.create_index("uq_permit_credential", ["credential_hash"], unique=True)
        # ★ 同一人同一时刻只能「在馆中」一条
        batch_op.create_index(
            "uq_permit_one_inside",
            ["user_id"],
            unique=True,
            sqlite_where=sa.text("status = 'checked_in'"),
            postgresql_where=sa.text("status = 'checked_in'"),
        )

    op.create_table(
        "reservation_slots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reservation_id", sa.Integer(), nullable=False),
        sa.Column("equipment_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("slot_index", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["equipment_id"], ["equipment.id"]),
        sa.ForeignKeyConstraint(["reservation_id"], ["reservations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("reservation_slots", schema=None) as batch_op:
        batch_op.create_index("ix_res_slot_reservation", ["reservation_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_reservation_slots_equipment_id"), ["equipment_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_reservation_slots_reservation_id"), ["reservation_id"], unique=False
        )
        # ★ 不变式 2：区间重叠在数据库层被拆成「同一格占两次」
        batch_op.create_index(
            "uq_equipment_slot", ["equipment_id", "date", "slot_index"], unique=True
        )

    op.create_table(
        "lab_occupancy",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lab_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("slot_index", sa.Integer(), nullable=False),
        sa.Column("seat", sa.Integer(), nullable=False),
        sa.Column("permit_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["lab_id"], ["laboratories.id"]),
        sa.ForeignKeyConstraint(["permit_id"], ["entry_permits.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("lab_occupancy", schema=None) as batch_op:
        batch_op.create_index("ix_lab_occ_lab_date", ["lab_id", "date"], unique=False)
        batch_op.create_index("ix_lab_occ_permit", ["permit_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_lab_occupancy_lab_id"), ["lab_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_lab_occupancy_permit_id"), ["permit_id"], unique=False)
        # ★ 不变式 3：房间容量被翻译成「N 个座位」，超员 = 同一座位被占两次
        batch_op.create_index(
            "uq_lab_slot_seat", ["lab_id", "date", "slot_index", "seat"], unique=True
        )


def downgrade() -> None:
    with op.batch_alter_table("lab_occupancy", schema=None) as batch_op:
        batch_op.drop_index("uq_lab_slot_seat")
        batch_op.drop_index(batch_op.f("ix_lab_occupancy_permit_id"))
        batch_op.drop_index(batch_op.f("ix_lab_occupancy_lab_id"))
        batch_op.drop_index("ix_lab_occ_permit")
        batch_op.drop_index("ix_lab_occ_lab_date")
    op.drop_table("lab_occupancy")

    with op.batch_alter_table("reservation_slots", schema=None) as batch_op:
        batch_op.drop_index("uq_equipment_slot")
        batch_op.drop_index(batch_op.f("ix_reservation_slots_reservation_id"))
        batch_op.drop_index(batch_op.f("ix_reservation_slots_equipment_id"))
        batch_op.drop_index("ix_res_slot_reservation")
    op.drop_table("reservation_slots")

    with op.batch_alter_table("entry_permits", schema=None) as batch_op:
        batch_op.drop_index("uq_permit_one_inside")
        batch_op.drop_index("uq_permit_credential")
        batch_op.drop_index("ix_permit_user_date")
        batch_op.drop_index("ix_permit_lab_date_status")
        batch_op.drop_index(batch_op.f("ix_entry_permits_user_id"))
        batch_op.drop_index(batch_op.f("ix_entry_permits_status"))
        batch_op.drop_index(batch_op.f("ix_entry_permits_reservation_id"))
        batch_op.drop_index(batch_op.f("ix_entry_permits_lab_id"))
    op.drop_table("entry_permits")

    with op.batch_alter_table("reservations", schema=None) as batch_op:
        batch_op.drop_index("uq_res_active_slot")
        batch_op.drop_index(batch_op.f("ix_reservations_user_id"))
        batch_op.drop_index(batch_op.f("ix_reservations_equipment_id"))
        batch_op.drop_index("ix_res_user_status")
        batch_op.drop_index("ix_res_equipment_date")
    op.drop_table("reservations")

    with op.batch_alter_table("equipment", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_equipment_name"))
        batch_op.drop_index(batch_op.f("ix_equipment_lab_id"))
        batch_op.drop_index(batch_op.f("ix_equipment_category"))
    op.drop_table("equipment")

    with op.batch_alter_table("cert_grants", schema=None) as batch_op:
        batch_op.drop_index("uq_cert_user_category_grant")
        batch_op.drop_index("ix_cert_user_category")
        batch_op.drop_index(batch_op.f("ix_cert_grants_user_id"))
        batch_op.drop_index(batch_op.f("ix_cert_grants_category"))
    op.drop_table("cert_grants")

    with op.batch_alter_table("access_events", schema=None) as batch_op:
        batch_op.drop_index("ix_access_user_time")
        batch_op.drop_index("ix_access_result")
        batch_op.drop_index("ix_access_occurred")
        batch_op.drop_index("ix_access_lab_time")
        batch_op.drop_index(batch_op.f("ix_access_events_occurred_at"))
        batch_op.drop_index(batch_op.f("ix_access_events_lab_id"))
    op.drop_table("access_events")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_users_username"))
    op.drop_table("users")

    op.drop_table("laboratories")

    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.drop_index("ix_audit_created")
        batch_op.drop_index("ix_audit_actor")
        batch_op.drop_index("ix_audit_action")
    op.drop_table("audit_logs")
