"""审计日志：关键写操作留痕，且**拒绝也要留痕**。

审计最容易做成"摆设"：接口有了、表也有了，但只记成功操作，
于是「谁在反复试别人的预约号」这种最该被发现的模式恰好一片空白。
所以本文件的重点是**失败路径的断言**。

另一个重点是**独立事务**：审计记录必须能在业务事务回滚之后仍然存在。
"""

from __future__ import annotations

from lagent.models import AuditLog
from lagent.audit import ACTION_LOGIN, ACTION_LOGIN_FAILED, ACTION_CANCEL, ACTION_BOOK


async def _logs(http, admin_headers, **params):
    resp = await http.get("/api/audit", headers=admin_headers, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestAuditEndpointAccess:
    async def test_admin_can_read(self, http, as_user):
        rows = await _logs(http, await as_user("管理员"))
        assert isinstance(rows, list)

    async def test_normal_user_is_403(self, http, as_user):
        resp = await http.get("/api/audit", headers=await as_user("李娜"))
        assert resp.status_code == 403

    async def test_anonymous_is_401(self, http):
        assert (await http.get("/api/audit")).status_code == 401

    async def test_limit_bounds_enforced(self, http, as_user):
        headers = await as_user("管理员")
        assert (await http.get("/api/audit", headers=headers, params={"limit": 0})).status_code == 422
        assert (
            await http.get("/api/audit", headers=headers, params={"limit": 9999})
        ).status_code == 422


class TestLoginAudit:
    async def test_successful_login_recorded(self, http):
        await http.post("/api/auth/login", json={"username": "李娜", "password": "lina@123"})
        rows = await _logs(http, (await self._admin(http)), action=ACTION_LOGIN)
        assert any(r["actor_name"] == "李娜" and r["outcome"] == "ok" for r in rows)

    async def test_failed_login_recorded(self, http):
        """★ 失败必须留痕 —— 否则暴力破解在日志里完全不可见。"""
        await http.post("/api/auth/login", json={"username": "李娜", "password": "wrong"})
        rows = await _logs(http, (await self._admin(http)), action=ACTION_LOGIN_FAILED)
        assert rows, "登录失败竟然没有审计记录"
        assert rows[0]["actor_name"] == "李娜"
        assert rows[0]["outcome"] == "denied"

    async def test_login_audit_never_stores_password(self, http):
        await http.post(
            "/api/auth/login", json={"username": "李娜", "password": "super-secret-pw"}
        )
        rows = await _logs(http, (await self._admin(http)))
        blob = str(rows)
        assert "super-secret-pw" not in blob
        assert "password" not in blob.lower()

    async def test_unknown_account_failure_recorded_without_actor_id(self, http):
        await http.post("/api/auth/login", json={"username": "查无此人", "password": "x"})
        rows = await _logs(http, (await self._admin(http)), action=ACTION_LOGIN_FAILED)
        assert rows[0]["actor_id"] is None
        assert rows[0]["actor_name"] == "查无此人"

    async def _admin(self, http):
        resp = await http.post(
            "/api/auth/login", json={"username": "管理员", "password": "admin@123"}
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}


class TestBusinessAudit:
    async def test_successful_booking_recorded(self, http, as_user):
        booked = await http.post(
            "/api/agent/chat",
            json={"message": "明天上午十点到十一点，紫外可见分光光度计", "session_id": "audit-1"},
            headers=await as_user("李娜"),
        )
        reservation_id = booked.json()["booking"]["reservation"]["id"]
        rows = await _logs(http, await as_user("管理员"), action=ACTION_BOOK)
        assert any(
            r["outcome"] == "ok" and r["target_id"] == str(reservation_id)
            and r["actor_name"] == "李娜"
            for r in rows
        )

    async def test_plain_chat_is_not_audited(self, http, as_user):
        """闲聊不该往审计表里灌水，否则真正的写操作会被淹掉。"""
        await http.post(
            "/api/agent/chat",
            json={"message": "你好", "session_id": "audit-chat"},
            headers=await as_user("李娜"),
        )
        rows = await _logs(http, await as_user("管理员"))
        assert not any(r["action"] == ACTION_BOOK for r in rows)

    async def test_cancel_recorded(self, http, as_user):
        headers = await as_user("李娜")
        booked = await http.post(
            "/api/agent/chat",
            json={"message": "明天上午十点到十一点，紫外可见分光光度计", "session_id": "audit-2"},
            headers=headers,
        )
        reservation_id = booked.json()["booking"]["reservation"]["id"]
        await http.post(
            "/api/reservations/cancel",
            json={"reservation_id": reservation_id, "reason": "审计用例"},
            headers=headers,
        )
        rows = await _logs(http, await as_user("管理员"), action=ACTION_CANCEL)
        assert any(r["target_id"] == str(reservation_id) and r["outcome"] == "ok" for r in rows)

    async def test_denied_cross_user_cancel_is_recorded(self, http, as_user):
        """越权取消被拒 —— 这条记录正是审计存在的意义。"""
        admin = await as_user("管理员")
        target = (await http.get("/api/reservations", params={"user_id": 1}, headers=admin)).json()[
            0
        ]["id"]
        resp = await http.post(
            "/api/reservations/cancel",
            json={"reservation_id": target, "reason": "越权"},
            headers=await as_user("李娜"),
        )
        assert resp.status_code == 409
        rows = await _logs(http, admin, action=ACTION_CANCEL)
        assert any(
            r["target_id"] == str(target)
            and r["outcome"] == "denied"
            and r["actor_name"] == "李娜"
            for r in rows
        ), "越权取消没留下记录"

    async def test_as_user_id_abuse_is_recorded_as_denied(self, http, as_user):
        """非管理员尝试用 as_user_id 代他人取消：403 且必须留痕。"""
        resp = await http.post(
            "/api/reservations/cancel",
            json={"reservation_id": 1, "as_user_id": 2},
            headers=await as_user("张伟"),
        )
        assert resp.status_code == 403
        rows = await _logs(http, await as_user("管理员"), action=ACTION_CANCEL)
        assert any(r["outcome"] == "denied" and "as_user_id" in r["detail"] for r in rows)


class TestAuditDurability:
    async def test_audit_survives_business_rollback(self, isolated_db, http, as_user):
        """★ 审计写在**独立事务**里，业务回滚不能把它一起抹掉。

        这条断言对应一个真实的设计决策：如果把审计写进业务的 session，
        "被拒绝的越权尝试"（恰恰发生在失败路径上）会随回滚一起消失，
        日志会永远显得一片祥和。
        """
        before = await _count()

        # 触发一条 409：取消不存在的预约（业务失败）
        resp = await http.post(
            "/api/reservations/cancel",
            json={"reservation_id": 987654},
            headers=await as_user("李娜"),
        )
        assert resp.status_code == 409

        after = await _count()
        assert after > before, "业务失败路径上的审计记录被回滚掉了"

    async def test_admin_reads_are_recorded(self, http, as_user):
        """读花名册也留痕：审计要能回答「谁看过什么」，不只是「谁改了什么」。"""
        await http.get("/api/users", headers=await as_user("管理员"))
        rows = await _logs(http, await as_user("管理员"))
        assert any(r["action"] == "admin.read" for r in rows)

    async def test_audit_table_is_append_only(self):
        """没有任何接口能改或删审计记录 —— 只暴露 GET。

        用 ``openapi()`` 而不是遍历 ``app.routes``：FastAPI 新版把
        ``include_router`` 的结果包成 ``_IncludedRouter``，直接遍历
        ``routes`` 拿不到 ``.path``（会静默得到一个空集合，测试形同虚设）。
        """
        from lagent.api import create_app

        paths = create_app().openapi()["paths"]
        audit_paths = {p for p in paths if p.startswith("/api/audit")}
        assert audit_paths == {"/api/audit"}, f"审计端点多出意料之外的路由：{audit_paths}"
        assert set(paths["/api/audit"]) == {"get"}, "审计端点只允许读取"


async def _count() -> int:
    from sqlalchemy import func, select

    from lagent.db import session_scope

    async with session_scope() as session:
        return int((await session.execute(select(func.count()).select_from(AuditLog))).scalar() or 0)
