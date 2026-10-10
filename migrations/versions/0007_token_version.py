"""令牌世代：users.token_version（P0）

Revision ID: 0007
Revises: 0006

## 它挡住的是哪一种失败

JWT 是**自包含**的：服务端验签通过就认账，不会回查数据库。
于是 ``deactivate_user``（清空口令哈希）只挡住了**新登录** ——
已经发出去的那张令牌，在过期前（默认 2 小时）仍然完全可用。

实测复现过：

    1. 建用户 → 登录拿 token
    2. 管理员停用该账号 → {"active": false}
    3. 用该账号密码重新登录 → 401  ✓ 挡住了
    4. 但拿第 1 步的旧 token：GET /api/auth/me      → 200
                               GET /api/reservations → 200
                               POST /api/agent/chat  → 200   ← 还能下单

降权同理且更危险：把某人从 admin 降成 user，旧令牌 payload 里的
``role`` 仍是 admin，在过期前继续享有管理员权限。

## 为什么是一个整数而不是黑名单

黑名单要存**每一张**被撤销的令牌（jti），还得为它们设过期清理 ——
而这里真正需要表达的是"**该用户的所有旧令牌都作废**"，
那用一个单调递增的整数最省：签发时把当时的世代写进 payload，
鉴权时回查用户当前世代，不相等即拒。

不需要清理任务：过期令牌本来就会被 exp 挡住，世代号只是个比较值。

## 代价

每个需要登录的请求多一次主键查询。这个查询本来就要为审计做，
所以不是新增开销；换来的是一条**可撤销**的鉴权链路。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("users", "token_version")
