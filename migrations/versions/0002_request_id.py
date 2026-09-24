"""给 audit_logs / access_events 加 request_id（P1-3）

Revision ID: 0002
Revises: 0001

## 为什么需要这一列

P1-3 的目标是「一条日志能串起一次请求」。日志文件里有了 request_id 之后，
下一个问题是：**库里的记录怎么和那条日志对上**？

一次 ``POST /api/agent/chat`` 通常会做三件事：写一条审计（谁下了单）、
可能写一条门禁流水（如果他顺手验证了凭证）、打若干行日志。
出问题时想回答的是「**这一个请求**到底做了什么」——没有这一列，
就只能靠时间戳去猜，而同一秒里可能有十几个请求。

有了它，一条 SQL 就能把链路捞出来：

    SELECT * FROM audit_logs   WHERE request_id = 'a1b2c3d4e5f6a7b8';
    SELECT * FROM access_events WHERE request_id = 'a1b2c3d4e5f6a7b8';

## 两个实现细节

**① ``server_default=''`` 不能省。** 这两张表在线上已经有数据，
新增一个 ``NOT NULL`` 列必须给老行一个确定的值，否则迁移会在
"给已有行补 NULL" 这一步失败。用空串而不是 NULL 也是刻意的：
空串是「没有请求上下文」（命令行、播种、定时任务）的确定表示，
而 NULL 会让 ``WHERE request_id = ''`` 这类查询漏掉它们。

**② 用 ``batch_alter_table``。** SQLite 没有 ``ALTER TABLE ... ADD COLUMN``
之外的列变更能力，batch 模式会在后台「建新表 → 拷数据 → 换名」。
这里加的是一列 + 一个索引，两种数据库都能走同一份代码 ——
所以没有写 ``if dialect == ...`` 的分支。

## 这条 revision 的意义

``0001`` 是从空库一次性建起的初始版本，真正的考验是**第二条**：
它要在**有数据的表**上做结构变更。这也是 ``migrations/README.md``
「已知限制」里点名没验证过的那一格。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMN = sa.Column(
    "request_id", sa.String(length=36), server_default="", nullable=False
)


def upgrade() -> None:
    with op.batch_alter_table("access_events", schema=None) as batch_op:
        batch_op.add_column(_COLUMN.copy())
        batch_op.create_index("ix_access_request", ["request_id"], unique=False)

    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.add_column(_COLUMN.copy())
        batch_op.create_index("ix_audit_request", ["request_id"], unique=False)


def downgrade() -> None:
    # 顺序与 upgrade 相反。删列会丢掉 request_id —— 那是**关联信息**，
    # 不是业务数据，所以这条回滚是安全的（不会答不出"谁下了这单"）。
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.drop_index("ix_audit_request")
        batch_op.drop_column("request_id")

    with op.batch_alter_table("access_events", schema=None) as batch_op:
        batch_op.drop_index("ix_access_request")
        batch_op.drop_column("request_id")
