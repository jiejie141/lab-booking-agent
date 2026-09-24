"""加 notifications 表（P1-6 通知）

Revision ID: 0004
Revises: 0003

## 为什么先做"记录"，再谈"发信"

评估里给的建议是「最低限度通知：只做成功/取消/驳回三种，邮件一种通道，
关键是'有'而不是'全'」。但"有"的第一层含义其实是**可查**：
用户说"我没收到"时，系统要能回答三件事中的哪一件发生了 ——

  1. 根本没生成（代码没走到）；
  2. 生成了但没发出去（SMTP 没配 / 挂了）；
  3. 发出去了（那就是邮件服务商或垃圾箱的事）。

没有这张表，这三种情况在运维眼里长得一模一样，只能靠猜。

## 为什么 failed 不自动重发

一封**迟到**的"预约成功"比没有更糟：用户照着它去了实验室，
而那条预约可能早就被取消了。所以失败就停在 failed，
重发由人确认后手动触发（``main.py notify`` 会列出待发与失败的数量）。

## 默认通道只有 email

学校场景里邮箱是唯一人人都有、且不需要接第三方平台的通道。
短信/微信要接服务商、要报备，属于"把通知做全"而不是"先有通知"。
```

## 索引

``ix_notify_status``：投递任务是"捞一批 pending 然后逐条发"，
走查条件就是 status，没有这个索引每次投递都全表扫。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        # ⚠️ nullable 必须与模型**逐列一致**：schema 比对（tests/test_migrations.py）
        # 会把 "迁移产物里可空、模型里不可空" 直接判成不一致。
        # 这类差异在 SQLite 上跑得好好的，只有到 PostgreSQL 上才会变成
        # "插入了没有 user_id 的通知"，所以必须在迁移里就写死。
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"),
                  index=True, nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("channel", sa.String(length=16), nullable=False, server_default="email"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("reservation_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_notify_status", "notifications", ["status", "created_at"])
    op.create_index("ix_notify_user", "notifications", ["user_id", "created_at"])


def downgrade() -> None:
    # 删表会丢掉"发过什么、有没有发出去"的记录 —— 那是**投递证据**，
    # 不是业务数据，回滚到这个版本时它本来也不再被使用。
    op.drop_index("ix_notify_user", table_name="notifications")
    op.drop_index("ix_notify_status", table_name="notifications")
    op.drop_table("notifications")
