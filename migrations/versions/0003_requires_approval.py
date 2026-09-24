"""给 equipment 加 requires_approval（P1-5 审批）

Revision ID: 0003
Revises: 0002

## 为什么是一列，而不是一张审批表

评估里给的建议就是「不要做工作流引擎」：加一个 ``requires_approval`` 标记，
预约落到 ``pending``，管理员点"通过/驳回"两下即可。

于是真正需要存的东西只有两样：

  * **这台设备要不要审批** → 设备上的一列；
  * **这条申请被怎么处理了** → 预约自己的状态 + 审计记录。

中间不需要"审批单"这种第三张表：它没有自己的生命周期，
一旦引入就要回答"审批单和预约状态不一致时以谁为准"，
而这个问题在本项目的规模下没有好答案。

## 三个已经存在、所以这里能这么简单的前提

1. ``STATUS_PENDING`` 早就在 ``ACTIVE_STATUSES`` 里 —— **待审批的申请占着坑**。
   这是对的：如果申请不占坑，那么"提交申请"到"审批通过"之间
   别人可以再约同一时段，审批通过时才发现冲突，用户白等一场。
2. 驳回走的是 ``cancelled`` + ``release_slots``，与"用户自己取消"是同一条路径 ——
   释放占用格这件事只有一处实现，不会出现"驳回了但格子还占着"。
3. 状态迁移带 ``version`` 乐观锁，两个管理员同时点通过/驳回不会都生效。

## 默认值为什么是 False

老设备默认不审批 —— 否则这一版上线之后，所有设备一夜之间都变成
"要等管理员点一下"，而院系并没有为此配人。审批是**按设备开通**的能力。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # server_default 不能省：这张表在线上已经有数据，新增 NOT NULL 列
    # 必须给老行一个确定的值（详见 0002 的说明）。
    with op.batch_alter_table("equipment", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "requires_approval",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )


def downgrade() -> None:
    # 删列是安全的：它只是"这台设备走不走审批"的开关，
    # 已经产生的 pending 预约仍会以 pending 停在库里（需要人工处理），
    # 但不会变成答不出"这单是怎么来的"。
    with op.batch_alter_table("equipment", schema=None) as batch_op:
        batch_op.drop_column("requires_approval")
