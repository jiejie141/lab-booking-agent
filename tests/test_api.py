"""HTTP 层：健康检查、认证、对话、资源查询、取消与检索。

P0-2 之后这里多了一层前提：**除存活探针与登录外，所有端点都要令牌**。
因此本文件的重点之一是「未认证一律 401」，见 TestAuthRequired。
"""

from __future__ import annotations

import datetime as dt

import pytest

TOMORROW = dt.date.today() + dt.timedelta(days=1)


class TestPublicEndpoints:
    """无需令牌的端点白名单。它短且集中，是本次鉴权设计的核心约束。"""

    async def test_health(self, http):
        resp = await http.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["app_mode"] == "mock"
        assert body["agent_available"] is True
        assert body["counts"]["labs"] == 3
        assert body["counts"]["equipment"] == 6

    async def test_health_never_leaks_secrets(self, http):
        body = (await http.get("/api/health")).json()
        assert "llm_api_key" not in body
        assert "jwt_secret" not in body
        assert "api_key" not in str(body).lower()

    async def test_index_served(self, http):
        resp = await http.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


class TestAuthRequired:
    """默认拒绝：拿掉令牌，业务端点必须一律 401。"""

    PROTECTED = [
        ("GET", "/api/tools"),
        ("GET", "/api/labs"),
        ("GET", "/api/users"),
        ("GET", "/api/today"),
        ("GET", "/api/reservations"),
        ("GET", "/api/retrieve?q=离心机"),
        ("GET", "/api/auth/me"),
    ]

    @pytest.mark.parametrize("method,path", PROTECTED)
    async def test_without_token_is_401(self, http, method, path):
        resp = await http.request(method, path)
        assert resp.status_code == 401, f"{path} 未受保护"
        assert resp.headers.get("www-authenticate") == "Bearer"

    async def test_chat_without_token_is_401(self, http):
        resp = await http.post("/api/agent/chat", json={"message": "你好"})
        assert resp.status_code == 401

    async def test_cancel_without_token_is_401(self, http):
        resp = await http.post("/api/reservations/cancel", json={"reservation_id": 1})
        assert resp.status_code == 401


class TestResources:
    async def test_tools_listed(self, http, as_user):
        resp = await http.get("/api/tools", headers=await as_user("李娜"))
        names = {item["name"] for item in resp.json()["tools"]}
        assert {"query_availability", "create_reservation", "cancel_reservation",
                "check_admission"} <= names

    async def test_today(self, http, as_user):
        body = (await http.get("/api/today", headers=await as_user("李娜"))).json()
        assert "date" in body and "weekday" in body

    async def test_labs_include_equipment(self, http, as_user):
        labs = (await http.get("/api/labs", headers=await as_user("李娜"))).json()
        assert len(labs) == 3
        assert all("equipment" in lab for lab in labs)
        codes = {item["code"] for lab in labs for item in lab["equipment"]}
        assert "SPEC-F7000" in codes


class TestUserDirectory:
    """花名册是管理端点：普通用户 403，管理员 200。"""

    async def test_normal_user_gets_403(self, http, as_user):
        resp = await http.get("/api/users", headers=await as_user("张伟"))
        assert resp.status_code == 403
        assert "管理员" in resp.json()["detail"]

    async def test_admin_lists_users(self, http, as_user):
        users = (await http.get("/api/users", headers=await as_user("管理员"))).json()
        assert len(users) == 3
        zhang = next(u for u in users if u["username"] == "张伟")
        assert zhang["certs"] == ["光谱"]

    async def test_directory_omits_credentials(self, http, as_user):
        users = (await http.get("/api/users", headers=await as_user("管理员"))).json()
        for user in users:
            assert "password_hash" not in user
            assert "email" not in user


class TestReservationScoping:
    """越权读取：非管理员看不到别人的预约，且传 user_id 也无法绕过。"""

    async def test_normal_user_sees_only_own(self, http, as_user):
        rows = (await http.get("/api/reservations", headers=await as_user("张伟"))).json()
        assert len(rows) == 1

    async def test_query_param_cannot_escape_scope(self, http, as_user):
        """张伟显式请求 user_id=2（李娜）——必须仍只拿到自己的。"""
        headers = await as_user("张伟")
        rows = (await http.get("/api/reservations", params={"user_id": 2}, headers=headers)).json()
        assert len(rows) == 1, "越权读取没被拦住"

    async def test_admin_sees_all(self, http, as_user):
        rows = (await http.get("/api/reservations", headers=await as_user("管理员"))).json()
        assert len(rows) == 2

    async def test_admin_can_filter_by_user(self, http, as_user):
        headers = await as_user("管理员")
        rows = (await http.get("/api/reservations", params={"user_id": 1}, headers=headers)).json()
        assert len(rows) == 1


class TestChat:
    async def test_chat_returns_proposals(self, http, as_user):
        resp = await http.post(
            "/api/agent/chat",
            json={"message": "明天下午两点想用荧光光谱仪两小时", "session_id": "api"},
            headers=await as_user("李娜"),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["intent"] == "create_reservation"
        assert body["proposals"]
        assert body["trace"]

    async def test_identity_comes_from_token_not_body(self, http, as_user):
        """★ P0-2 的核心断言：请求体里的 user_id 必须被丢弃。

        张伟（只有光谱资质）冒充李娜（资质齐全）去约离心机。
        若身份来自请求体，这单会成功；来自令牌则会被资质约束拦下。
        """
        headers = await as_user("张伟")
        resp = await http.post(
            "/api/agent/chat",
            json={
                "message": "明天上午十点到十一点，高速离心机",
                "user_id": 2,          # ← 冒充李娜，必须无效
                "session_id": "spoof",
            },
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["booking"] is None, "冒充身份竟然下单成功了"
        assert body["proposals"] == []

    async def test_booking_is_attributed_to_token_holder(self, http, as_user):
        """下单归属：张伟下的单不能出现在李娜的列表里。"""
        zhang = await as_user("张伟")
        booked = await http.post(
            "/api/agent/chat",
            json={
                "message": "明天上午十点到十一点，紫外可见分光光度计",
                "user_id": 2,          # 伪造归属，必须无效
                "session_id": "attr",
            },
            headers=zhang,
        )
        body = booked.json()
        assert body["booking"]["ok"] is True
        reservation_id = body["booking"]["reservation"]["id"]

        zhang_rows = (await http.get("/api/reservations", headers=zhang)).json()
        assert reservation_id in {r["id"] for r in zhang_rows}

        lina_rows = (await http.get("/api/reservations", headers=await as_user("李娜"))).json()
        assert reservation_id not in {r["id"] for r in lina_rows}, "预约错误地归属到了李娜名下"

    async def test_chat_books_and_can_be_cancelled(self, http, as_user):
        headers = await as_user("李娜")
        booked = await http.post(
            "/api/agent/chat",
            json={
                "message": "明天上午十点到十一点，紫外可见分光光度计",
                "session_id": "api2",
            },
            headers=headers,
        )
        body = booked.json()
        assert body["booking"]["ok"] is True
        reservation_id = body["booking"]["reservation"]["id"]

        cancelled = await http.post(
            "/api/reservations/cancel",
            json={"reservation_id": reservation_id, "reason": "接口测试"},
            headers=headers,
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["ok"] is True

    async def test_chat_empty_message_rejected(self, http, as_user):
        resp = await http.post(
            "/api/agent/chat", json={"message": ""}, headers=await as_user("李娜")
        )
        assert resp.status_code == 422

    async def test_degraded_mode_reports_flag(self, http, as_user):
        original = http.app.state.agent
        try:
            from lagent.agent.graph import LabBookingAgent

            http.app.state.agent = LabBookingAgent(None)
            resp = await http.post(
                "/api/agent/chat", json={"message": "你好"}, headers=await as_user("李娜")
            )
            assert resp.json()["degraded"] is True
        finally:
            http.app.state.agent = original


class TestRetrievalEndpoint:
    async def test_retrieve_hits(self, http, as_user):
        resp = await http.get(
            "/api/retrieve", params={"q": "离心机 配平", "k": 3}, headers=await as_user("李娜")
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["backend"] == "bm25"
        assert body["hits"]
        assert body["hits"][0]["heading"] == "离心类设备使用规范"
        assert body["hits"][0]["score"] > 0

    async def test_retrieve_requires_query(self, http, as_user):
        resp = await http.get("/api/retrieve", headers=await as_user("李娜"))
        assert resp.status_code == 422


class TestCancelAuthorization:
    async def test_cancel_unknown_returns_409(self, http, as_user):
        resp = await http.post(
            "/api/reservations/cancel",
            json={"reservation_id": 999999},
            headers=await as_user("李娜"),
        )
        assert resp.status_code == 409

    async def test_cannot_cancel_others_reservation(self, http, as_user):
        """李娜（user 2）去取消张伟（user 1）的预约：409，且预约仍在。"""
        admin = await as_user("管理员")
        zhang_rows = (
            await http.get("/api/reservations", params={"user_id": 1}, headers=admin)
        ).json()
        target = zhang_rows[0]["id"]

        resp = await http.post(
            "/api/reservations/cancel",
            json={"reservation_id": target, "reason": "越权尝试"},
            headers=await as_user("李娜"),
        )
        assert resp.status_code == 409
        assert "自己" in resp.json()["detail"]

        still = (await http.get("/api/reservations", params={"user_id": 1}, headers=admin)).json()
        assert [r["status"] for r in still if r["id"] == target] == ["confirmed"]

    async def test_as_user_id_rejected_for_normal_user(self, http, as_user):
        """非管理员用 as_user_id 代他人取消：显式 403（不是静默忽略）。"""
        admin = await as_user("管理员")
        zhang_rows = (
            await http.get("/api/reservations", params={"user_id": 1}, headers=admin)
        ).json()
        resp = await http.post(
            "/api/reservations/cancel",
            json={"reservation_id": zhang_rows[0]["id"], "as_user_id": 1},
            headers=await as_user("李娜"),
        )
        assert resp.status_code == 403

    async def test_admin_can_cancel_on_behalf(self, http, as_user):
        admin = await as_user("管理员")
        zhang_rows = (
            await http.get("/api/reservations", params={"user_id": 1}, headers=admin)
        ).json()
        target = zhang_rows[0]["id"]
        resp = await http.post(
            "/api/reservations/cancel",
            json={"reservation_id": target, "as_user_id": 1, "reason": "管理员代为取消"},
            headers=admin,
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
