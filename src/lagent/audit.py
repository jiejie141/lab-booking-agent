"""审计日志：只追加，且**用独立事务写**。

两个刻意的设计：

1. **独立事务。** 审计记录不能跟着业务事务一起回滚 ——
   「越权尝试被拒绝」这件事恰恰发生在业务事务失败的那条路径上。
   如果把审计写进同一个 session，一次回滚会把证据一起抹掉，
   留下的永远是「一片祥和」的日志。所以这里自己开 ``session_scope()``。

2. **失败与拒绝也要记。** 只记成功操作，就答不出「谁在反复试别人的预约号」
   这种最该被发现的问题。

写入失败（例如库锁超时）只吞掉并打印，**绝不让审计把主流程搞挂** ——
要求"审计必须成功否则业务失败"对预约系统来说代价过大，
正确做法是让它失败可见（打印）而不是静默，再由告警去兜。
"""

from __future__ import annotations

from sqlalchemy import select

from .config import get_settings
from .db import session_scope
from .models import (
    ACTION_ADMIN_READ,
    ACTION_BOOK,
    ACTION_CANCEL,
    ACTION_LOGIN,
    ACTION_LOGIN_FAILED,
    OUTCOME_DENIED,
    OUTCOME_FAILED,
    OUTCOME_OK,
    AuditLog,
)

__all__ = [
    "ACTION_ADMIN_READ",
    "ACTION_BOOK",
    "ACTION_CANCEL",
    "ACTION_LOGIN",
    "ACTION_LOGIN_FAILED",
    "OUTCOME_DENIED",
    "OUTCOME_FAILED",
    "OUTCOME_OK",
    "recent_logs",
    "record",
]


async def record(
    *,
    action: str,
    outcome: str = OUTCOME_OK,
    actor_id: int | None = None,
    actor_name: str = "",
    target_type: str = "",
    target_id: str | int = "",
    detail: str = "",
    client_host: str = "",
) -> None:
    """追加一条审计记录。永远不抛异常给调用方。"""
    if not get_settings().audit_enabled:
        return
    try:
        async with session_scope() as session:
            session.add(
                AuditLog(
                    actor_id=actor_id,
                    actor_name=actor_name[:64],
                    action=action,
                    target_type=target_type[:32],
                    target_id=str(target_id)[:64],
                    outcome=outcome,
                    # 截断：detail 可能来自用户输入，别让它撑爆审计表
                    detail=detail[:500],
                    client_host=client_host[:64],
                )
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[audit] ⚠ 审计写入失败（不影响主流程）：{type(exc).__name__}: {exc}")


async def recent_logs(limit: int = 100, *, action: str | None = None) -> list[AuditLog]:
    """读最近的审计记录（管理员端点用）。"""
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(max(1, min(limit, 500)))
    if action:
        stmt = stmt.where(AuditLog.action == action)
    async with session_scope() as session:
        return list((await session.execute(stmt)).scalars().all())
