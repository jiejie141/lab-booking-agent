"""通知：写下来，再发出去（P1-6）。

评估里的建议是「最低限度：预约成功 / 取消 / 驳回三种，邮件一种通道，
关键是『有』而不是『全』」。这份实现照着做，但补了一条：

**先落库，再投递。**

直接在业务接口里 ``smtplib.send`` 的下场是 —— 邮件服务器抖一下，
下单接口就跟着 500；或者更糟：通知失败被 try/except 吞掉，
于是"用户说没收到"这件事**永远查不出发生过没有**。
所以流程是：写一条 ``pending`` → 独立的投递任务去发 → 结果写回同一行。

关于"SMTP 没配"这件事的立场：

**不假装成功，也不静默丢弃。** 没配 SMTP 时投递任务照常跑，
一条都发不出去，但每一行都还是 ``pending``，并且任务报告里写明
"跳过 N 条：SMTP 未配置"。这样"我们根本没配邮件"和"配了但发失败"
是两种看得出来的状态 —— 而假装成功（直接标记 sent）会让这件事
一直到用户投诉才暴露。
"""

from __future__ import annotations

import asyncio
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage

from sqlalchemy import select

from .clock import now_local
from .config import get_settings
from .db import session_scope
from .models import Notification, User

# 业务类型。取值域刻意很小 —— 通知做"全"的诱惑很大，
# 而每加一种就要回答一次"它迟到还有意义吗"。
KIND_CREATED = "reservation.created"
KIND_PENDING = "reservation.pending"
KIND_CANCELLED = "reservation.cancelled"
KIND_APPROVED = "reservation.approved"
KIND_REJECTED = "reservation.rejected"

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"


@dataclass
class DrainResult:
    """一次投递的结果。

    ``skipped`` 与 ``failed`` 分开：前者是"我们没配置好"（运维问题），
    后者是"配了但发不出去"（对方/网络问题）。处置完全不同，
    合成一个数字就没法判断该去找谁。
    """

    sent: int = 0
    failed: int = 0
    skipped: int = 0
    reason: str = ""
    errors: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = [f"发出 {self.sent} 条", f"失败 {self.failed} 条"]
        if self.skipped:
            parts.append(f"跳过 {self.skipped} 条")
        text = "，".join(parts)
        if self.reason:
            text += f"（{self.reason}）"
        return text

    @property
    def ok(self) -> bool:
        return self.failed == 0


async def enqueue(
    session,
    *,
    user_id: int,
    kind: str,
    title: str,
    body: str,
    reservation_id: int | None = None,
    channel: str = "email",
) -> Notification:
    """写一条待发通知。**不发送** —— 发送只由 :func:`drain` 负责。"""
    row = Notification(
        user_id=user_id,
        kind=kind,
        title=title,
        body=body,
        channel=channel,
        status=STATUS_PENDING,
        reservation_id=reservation_id,
        created_at=now_local(),
    )
    session.add(row)
    await session.flush()
    return row


def smtp_problem() -> str | None:
    """SMTP 配好了吗？返回一句人话原因；``None`` 表示可以发。

    与 ``security.secret_problem()`` 同一写法：返回**原因**而不是 bool，
    调用方可以直接把它写进日志和任务报告。
    """
    settings = get_settings()
    if not settings.smtp_host:
        return "SMTP 未配置（LAB_SMTP_HOST 为空），通知只记录不投递"
    if not settings.smtp_from:
        return "发件人未配置（LAB_SMTP_FROM 为空）"
    return None


async def drain(*, limit: int | None = None) -> DrainResult:
    """投递一批待发通知。

    返回而不是抛出：投递失败不该让调用它的定时任务整个挂掉，
    而且"几条失败、为什么"要能出现在任务报告里。
    """
    settings = get_settings()
    batch = limit or settings.notify_batch_size
    result = DrainResult()

    problem = smtp_problem()
    if problem is not None:
        async with session_scope() as session:
            pending = (
                await session.execute(
                    select(Notification.id).where(
                        Notification.status == STATUS_PENDING
                    )
                )
            ).scalars().all()
        result.skipped = len(pending)
        result.reason = problem
        return result

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Notification)
                .where(Notification.status == STATUS_PENDING)
                .order_by(Notification.created_at)
                .limit(batch)
            )
        ).scalars().all()
        if not rows:
            return result
        # 先把用户邮箱一次捞齐，避免逐条 get 造成 N+1
        users = {
            row.id: row
            for row in (
                await session.execute(
                    select(User).where(User.id.in_({r.user_id for r in rows}))
                )
            ).scalars().all()
        }
        targets = [(r, users.get(r.user_id)) for r in rows]

    for row, user in targets:
        if user is None or not user.email:
            await _mark(row.id, STATUS_FAILED, "用户没有邮箱地址，无法投递")
            result.failed += 1
            result.errors.append(f"#{row.id} 无邮箱")
            continue
        try:
            await asyncio.to_thread(
                _send_email,
                recipient=user.email,
                subject=row.title,
                body=row.body,
            )
        except Exception as exc:  # noqa: BLE001 —— 见下方注释
            # 宽捕获是**故意**的：SMTP 的失败形态五花八门（连接超时、
            # 认证失败、收件人被拒……），漏掉任何一种都会让整个投递批次
            # 中断在一条坏邮件上。这里要的是"这条失败，继续下一条"。
            await _mark(row.id, STATUS_FAILED, str(exc))
            result.failed += 1
            result.errors.append(f"#{row.id} {exc}")
            continue
        await _mark(row.id, STATUS_SENT, "")
        result.sent += 1

    return result


async def _mark(row_id: int, status: str, error: str) -> None:
    async with session_scope() as session:
        row = await session.get(Notification, row_id)
        if row is None:
            return
        row.status = status
        row.error = error
        if status == STATUS_SENT:
            row.sent_at = now_local()


def _send_email(*, recipient: str, subject: str, body: str) -> None:
    """同步发信（阻塞 IO，调用方负责丢到线程里）。"""
    settings = get_settings()
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from
    message["To"] = recipient
    message.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as client:
        # 没有 EHLO 的话服务器通常会直接 503；smtplib 在 sendmail 前
        # 会自己补一次，这里显式写出来是为了让 "连上了但服务器不理" 暴露得更早
        client.ehlo()
        if settings.smtp_use_tls:
            client.starttls()
            client.ehlo()
        if settings.smtp_user:
            client.login(settings.smtp_user, settings.smtp_password)
        client.send_message(message)


async def sweep_notifications() -> tuple[int, str]:
    """清扫任务入口：配了 SMTP 就自动投递，没配就如实报告"跳过"。"""
    result = await drain()
    return result.sent + result.failed, result.describe()


async def backlog() -> dict[str, int]:
    """待发 / 失败各有多少（运维看一眼就知道有没有积压）。"""
    from sqlalchemy import func

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Notification.status, func.count()).group_by(Notification.status)
            )
        ).all()
    counts = {STATUS_PENDING: 0, STATUS_SENT: 0, STATUS_FAILED: 0}
    for status, total in rows:
        counts[status] = int(total)
    return counts
