"""下单幂等键：reservations.idempotency_key（P2）

Revision ID: 0006
Revises: 0005

## 它挡住的是哪一种失败

最容易想到的失败形态是"同一时段重复下单" —— 那个其实已经被
``uq_res_active_slot`` 挡住了（会明确回 409 冲突）。

★ 真正会漏的是**换一个时段的重试**：网络超时后客户端重发，
而这次请求里的时段与上一条不同（比如用户改了主意、或客户端重算了默认时段），
于是系统老老实实地又建了一条 —— 一条用户从没打算下的预约。
用户看到的则是"我只是点了一下，怎么有两单"。

## 为什么键由客户端生成

幂等的语义是"**同一个意图**只生效一次"，而"是不是同一个意图"只有发起方知道。
服务端自己算哈希（比如把参数拼起来 hash）在重试时会算出**同一个**值 ——
那正好是我们要的……但一旦参数里带了时间戳之类会变的东西就不成立了，
而且服务端算键会让"重试"和"再来一单"变得无法区分。
让客户端生成并显式带上，语义最干净：重试就带同一个键，新单就换一个。

## 为什么是部分唯一索引

``idempotency_key`` 可空：老预约没有它，不关心幂等的调用方也可以不传。
用 ``WHERE idempotency_key IS NOT NULL`` 的部分索引，
只对真的传了键的行施加唯一约束 —— 否则"所有没传键的预约"会因为
NULL 而各自独立（多数数据库里 NULL 不相等），语义上倒是也成立，
但把索引写成部分索引更直白，也和项目里另外三条部分索引保持一致。

## 索引

``uq_res_idempotency``：(user_id, idempotency_key) 唯一。
按用户分开是必须的 —— 两个不同用户用同一个键不应当互相影响，
否则客户端用一个固定字符串当键时，全系统就只有一个人能下单了。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "reservations",
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "uq_res_idempotency",
        "reservations",
        ["user_id", "idempotency_key"],
        unique=True,
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_res_idempotency", table_name="reservations")
    op.drop_column("reservations", "idempotency_key")
