"""预约违约字段：no_show_at / pardoned_at（P1-8 黑名单与违约规则）

Revision ID: 0005
Revises: 0004

## 为什么加在 reservations 上，而不是另建一张 violations 表

违约是**一条预约的属性**，不是一个独立对象：它回答的是"这条款约兑现了吗"，
脱离了预约本身就没有意义。另建一张表会带来两个真实麻烦 ——

  * 一条预约可能被判两次（重复行要靠唯一约束兜，而唯一约束正是我本来
    想靠它保证的东西）；
  * "豁免"要跨表更新，事务边界一下子变复杂。

两个时间戳放在预约行上，判定天然幂等（`WHERE no_show_at IS NULL`），
豁免也不过是把另一个字段填上。

## 为什么是"两个可空时间戳"而不是一个布尔 + 一个布尔

`no_show_at` 记**判定时刻**，`pardoned_at` 记**豁免时刻**。
一个布尔只能回答"算不算违约"，回答不了"这条是什么时候判的、
是不是被推翻过"。而误判恰恰是最需要追溯的场景 ——
学生说"那天门禁坏了"，管理员要能看出系统是当天判的还是上周补判的、
有没有被人推翻过。

## 为什么不加 `violations_count` 冗余字段

违约次数**从数据推导**（窗口内 `no_show_at IS NOT NULL AND pardoned_at IS NULL`
的行数），不存计数。存了就要保证它和明细永远一致，
而"豁免一条之后计数要减一"这种逻辑迟早会在某条路径上漏掉。
能推导的东西就别存 —— 这条与"容量不靠应用层数"是同一个立场。

## 索引

`ix_res_noshow`：违约统计要按"用户 + 判定时刻"扫窗口，
没有它每次下单前的检查都是一次全表扫 —— 而它恰好在**预约写入的关键路径**上。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "reservations",
        sa.Column("no_show_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "reservations",
        sa.Column("pardoned_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_res_noshow", "reservations", ["user_id", "no_show_at"])


def downgrade() -> None:
    op.drop_index("ix_res_noshow", table_name="reservations")
    op.drop_column("reservations", "pardoned_at")
    op.drop_column("reservations", "no_show_at")
