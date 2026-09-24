"""通知：写下来，再发出去（P1-6）。

这一组要证明的**不是**"邮件发出去了"（那要真 SMTP，测试里发不了），
而是四件决定它有没有用的事：

1. **业务动作真的留下了通知** —— 下单/取消/通过/驳回各一条，
   而且收件人是**申请人**（管理员代操作时不能通知错人）；
2. **没配 SMTP 时不假装成功**：一条都发不出去，但都还是 ``pending``，
   任务报告里写明"跳过 N 条：SMTP 未配置"。
   "我们根本没配邮件"与"配了但发失败"必须是两种看得出来的状态；
3. **配了之后能真的发出去**（用假的 SMTP 客户端验证），失败则留在
   ``failed`` 并**记下原因** —— 否则"用户说没收到"只能靠猜；
4. **通知崩了不能把业务带崩**：写通知失败时下单仍然要成功。

第 4 条是最容易被忽略的：通知是锦上添花，邮件服务器挂了不该导致
实验室约不上。
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from lagent import notify
from lagent.db import session_scope
from lagent.models import Notification, User

ZHANGWEI, LINA, ADMIN = 1, 2, 3
UV = 2
CENTRIFUGE = 6

START = dt.time(10, 0)
END = dt.time(12, 0)


def free_day() -> dt.date:
    return now_local_date() + dt.timedelta(days=2)


def now_local_date() -> dt.date:
    from lagent.clock import now_local

    return now_local().date()


def booking_body(equipment_id: int, **overrides) -> dict:
    body = {
        "equipment_id": equipment_id,
        "date": free_day().isoformat(),
        "start": START.strftime("%H:%M"),
        "end": END.strftime("%H:%M"),
        "purpose": "通知测试",
    }
    body.update(overrides)
    return body


async def rows_for(user_id: int) -> list[Notification]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Notification).where(Notification.user_id == user_id)
            )
        ).scalars().all()
        return list(rows)


# ===========================================================================
# 一、业务动作留下证据
# ===========================================================================
class TestBusinessEventsProduceNotifications:
    async def test_a_successful_booking_notifies_the_user(self, http, as_user):
        headers = await as_user("李娜")
        resp = await http.post("/api/reservations", json=booking_body(UV), headers=headers)
        assert resp.status_code == 201, resp.text

        rows = await rows_for(LINA)
        assert len(rows) == 1
        assert rows[0].kind == notify.KIND_CREATED
        assert rows[0].status == "pending"
        assert rows[0].reservation_id == resp.json()["reservation"]["id"]

    async def test_a_pending_application_says_it_is_waiting(self, http, as_user):
        """待审批的通知要说清"在等什么"，否则用户以为约上了。"""
        admin = await as_user("管理员")
        assert (
            await http.patch(
                f"/api/equipment/{UV}", json={"requires_approval": True}, headers=admin
            )
        ).status_code == 200

        lina = await as_user("李娜")
        assert (await http.post("/api/reservations", json=booking_body(UV), headers=lina)).status_code == 201
        rows = await rows_for(LINA)
        assert rows[0].kind == notify.KIND_PENDING
        assert "审批" in rows[0].body

    async def test_cancelling_notifies_the_user(self, http, as_user):
        headers = await as_user("李娜")
        created = await http.post("/api/reservations", json=booking_body(UV), headers=headers)
        assert (await http.post(
            "/api/reservations/cancel",
            json={"reservation_id": created.json()["reservation"]["id"]},
            headers=headers,
        )).status_code == 200

        kinds = {row.kind for row in await rows_for(LINA)}
        assert notify.KIND_CANCELLED in kinds

    async def test_rejection_notifies_the_applicant_with_the_reason(self, http, as_user):
        """★ 驳回要通知到**申请人**并带上原因。

        否则用户只会看到"我的预约没了"，不知道为什么，也不知道该找谁。
        """
        admin = await as_user("管理员")
        assert (
            await http.patch(
                f"/api/equipment/{UV}", json={"requires_approval": True}, headers=admin
            )
        ).status_code == 200

        lina = await as_user("李娜")
        created = await http.post("/api/reservations", json=booking_body(UV), headers=lina)
        reservation_id = created.json()["reservation"]["id"]

        rejected = await http.post(
            f"/api/reservations/{reservation_id}/reject",
            json={"reason": "该时段已安排教学"},
            headers=admin,
        )
        assert rejected.status_code == 200, rejected.text

        rows = await rows_for(LINA)
        rejected_rows = [r for r in rows if r.kind == notify.KIND_REJECTED]
        assert len(rejected_rows) == 1
        assert "已安排教学" in rejected_rows[0].body

    async def test_approval_notifies_the_applicant(self, http, as_user):
        admin = await as_user("管理员")
        assert (
            await http.patch(
                f"/api/equipment/{UV}", json={"requires_approval": True}, headers=admin
            )
        ).status_code == 200
        lina = await as_user("李娜")
        reservation_id = (
            await http.post("/api/reservations", json=booking_body(UV), headers=lina)
        ).json()["reservation"]["id"]
        assert (
            await http.post(f"/api/reservations/{reservation_id}/approve", headers=admin)
        ).status_code == 200

        kinds = {row.kind for row in await rows_for(LINA)}
        assert notify.KIND_APPROVED in kinds

    async def test_an_admin_booking_for_someone_notifies_that_someone(self, http, as_user):
        """★ 管理员代下单，通知要发给**被代的那个人**，不是管理员。"""
        admin = await as_user("管理员")
        created = await http.post(
            "/api/reservations",
            json=booking_body(UV, as_user_id=ZHANGWEI, purpose="代约"),
            headers=admin,
        )
        assert created.status_code == 201, created.text

        assert await rows_for(ZHANGWEI), "通知发给了管理员而不是申请人"
        assert not await rows_for(ADMIN)

    async def test_a_failed_booking_notifies_nobody(self, http, as_user):
        """约不上就不该收到"预约成功" —— 这条看着显然，
        但它验的是"通知挂在成功分支上"而不是挂在"走过下单流程"上。"""
        headers = await as_user("李娜")
        resp = await http.post("/api/reservations", json=booking_body(99999), headers=headers)
        assert resp.status_code == 404
        assert await rows_for(LINA) == []


# ===========================================================================
# 二、没配 SMTP：不假装成功
# ===========================================================================
class TestWithoutSmtp:
    async def test_drain_skips_and_says_why(self, http, as_user):
        """★ 一封都发不出去，但**必须说明是没配**，而不是"发出 0 条"。"""
        headers = await as_user("李娜")
        assert (await http.post("/api/reservations", json=booking_body(UV), headers=headers)).status_code == 201

        result = await notify.drain()
        assert result.sent == 0
        assert result.skipped == 1
        assert "SMTP" in result.reason

    async def test_the_rows_stay_pending(self, http, as_user):
        """没发出去就还是 pending —— 配好 SMTP 之后这一批可以补发。

        如果在这里标成 sent，"用户没收到"就变成了一个永远查不出来的谎言。
        """
        headers = await as_user("李娜")
        assert (await http.post("/api/reservations", json=booking_body(UV), headers=headers)).status_code == 201
        await notify.drain()
        rows = await rows_for(LINA)
        assert all(row.status == "pending" for row in rows)
        assert all(row.sent_at is None for row in rows)

    def test_smtp_problem_names_the_missing_setting(self):
        assert notify.smtp_problem() is not None
        text = notify.smtp_problem() or ""
        assert "SMTP" in text


# ===========================================================================
# 三、配了 SMTP：真的能发，失败要留原因
# ===========================================================================
class _FakeSmtp:
    """假 SMTP：记录发出去的信，可按需抛异常。"""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[dict] = []

    def __call__(self, host: str, port: int, timeout: float = 15):
        fake = self

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def ehlo(self):
                return (250, b"ok")

            def starttls(self):
                return (220, b"ready")

            def login(self, user, password):
                return (235, b"ok")

            def send_message(self, message):
                if fake.fail:
                    raise OSError("550 mailbox unavailable")
                fake.sent.append(
                    {
                        "to": message["To"],
                        "subject": message["Subject"],
                        "body": message.get_content(),
                    }
                )
                return {}

        return _Client()


@pytest.fixture
def smtp(monkeypatch):
    """把 SMTP 配好，并把 smtplib.SMTP 换成假的。"""
    monkeypatch.setenv("LAB_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("LAB_SMTP_FROM", "lab@example.com")
    from lagent.config import reset_settings_cache

    reset_settings_cache()
    yield
    reset_settings_cache()


class TestWithSmtp:
    async def test_drain_sends_the_pending_rows(self, isolated_db, smtp, monkeypatch):
        fake = _FakeSmtp()
        monkeypatch.setattr("smtplib.SMTP", fake)

        async with session_scope() as session:
            await notify.enqueue(
                session, user_id=LINA, kind=notify.KIND_CREATED,
                title="预约成功", body="你已预约 10:00-12:00",
            )
        result = await notify.drain()
        assert result.sent == 1 and result.failed == 0
        assert fake.sent[0]["to"] == "lina@example.com"
        assert fake.sent[0]["subject"] == "预约成功"

    async def test_sent_rows_are_marked_and_stamped(self, isolated_db, smtp, monkeypatch):
        monkeypatch.setattr("smtplib.SMTP", _FakeSmtp())
        async with session_scope() as session:
            await notify.enqueue(
                session, user_id=LINA, kind=notify.KIND_CREATED, title="t", body="b"
            )
        await notify.drain()
        rows = await rows_for(LINA)
        assert rows[0].status == "sent"
        assert rows[0].sent_at is not None

    async def test_a_failure_is_recorded_not_swallowed(self, isolated_db, smtp, monkeypatch):
        """★ 发不出去要留在 failed **并记下原因**。

        吞掉的话，"用户说没收到"就只能靠猜：是没生成？没发？还是进了垃圾箱？
        """
        monkeypatch.setattr("smtplib.SMTP", _FakeSmtp(fail=True))
        async with session_scope() as session:
            await notify.enqueue(
                session, user_id=LINA, kind=notify.KIND_CREATED, title="t", body="b"
            )
        result = await notify.drain()
        assert result.failed == 1
        rows = await rows_for(LINA)
        assert rows[0].status == "failed"
        assert "550" in rows[0].error

    async def test_one_bad_message_does_not_stop_the_batch(self, isolated_db, smtp, monkeypatch):
        """坏邮件不该中断整批 —— 否则一条写错的地址会把所有人的通知卡住。"""
        calls = {"n": 0}

        def flaky(host, port, timeout=15):
            calls["n"] += 1
            return _FakeSmtp(fail=(calls["n"] == 1))(host, port, timeout)

        monkeypatch.setattr("smtplib.SMTP", flaky)
        async with session_scope() as session:
            for _ in range(3):
                await notify.enqueue(
                    session, user_id=LINA, kind=notify.KIND_CREATED, title="t", body="b"
                )
        result = await notify.drain()
        assert result.failed == 1 and result.sent == 2

    async def test_a_user_without_email_is_reported_not_silently_dropped(
        self, isolated_db, smtp, monkeypatch
    ):
        monkeypatch.setattr("smtplib.SMTP", _FakeSmtp())
        async with session_scope() as session:
            user = User(username="没邮箱", email="", role="user", certs=[])
            session.add(user)
            await session.flush()
            await notify.enqueue(
                session, user_id=user.id, kind=notify.KIND_CREATED, title="t", body="b"
            )
        result = await notify.drain()
        assert result.failed == 1
        assert "邮箱" in (result.errors[0] if result.errors else "")


# ===========================================================================
# 四、接口与运维视角
# ===========================================================================
class TestVisibility:
    async def test_a_user_sees_only_their_own(self, http, as_user):
        admin = await as_user("管理员")
        lina = await as_user("李娜")
        assert (await http.post("/api/reservations", json=booking_body(UV), headers=lina)).status_code == 201
        assert (
            await http.post(
                "/api/reservations",
                json=booking_body(CENTRIFUGE, purpose="管理员自己"),
                headers=admin,
            )
        ).status_code == 201

        mine = (await http.get("/api/notifications", headers=lina)).json()
        assert mine and all(row["id"] for row in mine)
        assert len(mine) == 1

    async def test_anonymous_cannot_read_notifications(self, http):
        assert (await http.get("/api/notifications")).status_code == 401

    async def test_an_admin_can_inspect_someone_else(self, http, as_user):
        admin = await as_user("管理员")
        lina = await as_user("李娜")
        assert (await http.post("/api/reservations", json=booking_body(UV), headers=lina)).status_code == 201

        resp = await http.get(f"/api/notifications?user_id={LINA}", headers=admin)
        assert resp.status_code == 200, resp.text
        assert len(resp.json()) == 1

    async def test_a_plain_user_cannot_read_others_even_by_id(self, http, as_user):
        """非管理员传 user_id 要被无视（与预约列表同一策略）。"""
        admin = await as_user("管理员")
        lina = await as_user("李娜")
        assert (
            await http.post(
                "/api/reservations",
                json=booking_body(CENTRIFUGE, purpose="管理员的"),
                headers=admin,
            )
        ).status_code == 201

        resp = await http.get(f"/api/notifications?user_id={ADMIN}", headers=lina)
        assert resp.status_code == 200, resp.text
        assert resp.json() == [], "普通用户读到了别人的通知"

    async def test_backlog_counts_are_reportable(self, isolated_db):
        async with session_scope() as session:
            await notify.enqueue(
                session, user_id=LINA, kind=notify.KIND_CREATED, title="t", body="b"
            )
        counts = await notify.backlog()
        assert counts["pending"] == 1
        assert counts["sent"] == 0


# ===========================================================================
# 五、★ 通知不能把业务带崩
# ===========================================================================
class TestNotificationNeverBreaksTheBusiness:
    async def test_a_broken_notify_still_lets_the_booking_succeed(
        self, http, as_user, monkeypatch
    ):
        """★ 邮件服务器挂了不该导致实验室约不上。

        通知是锦上添花。这里把入队直接打成抛异常，
        下单必须照样 201 —— 而失败要被记下来（不是静默吞掉）。
        """
        import lagent.api as api_module

        async def boom(*args, **kwargs):
            raise RuntimeError("通知表炸了（测试注入）")

        monkeypatch.setattr(api_module.notify, "enqueue", boom)

        headers = await as_user("李娜")
        resp = await http.post("/api/reservations", json=booking_body(UV), headers=headers)
        assert resp.status_code == 201, resp.text

    async def test_the_failure_is_logged_not_swallowed(self, http, as_user, monkeypatch, caplog):
        """失败要留下痕迹 —— 静默吞掉的话"为什么没人收到通知"永远查不出。"""
        import logging

        import lagent.api as api_module

        async def boom(*args, **kwargs):
            raise RuntimeError("通知表炸了（测试注入）")

        monkeypatch.setattr(api_module.notify, "enqueue", boom)
        with caplog.at_level(logging.WARNING):
            headers = await as_user("李娜")
            await http.post("/api/reservations", json=booking_body(UV), headers=headers)
        assert any("通知入队失败" in record.message % record.args
                   for record in caplog.records)
