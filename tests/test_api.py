"""HTTP 层：健康检查、对话、资源查询、取消与检索。"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from lagent.api import app

TOMORROW = dt.date.today() + dt.timedelta(days=1)


@pytest.fixture
async def client(isolated_db):
    # 手动进入 lifespan，让 app.state 里的 agent 与目录被正确装配
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


class TestMeta:
    async def test_health(self, client):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["app_mode"] == "mock"
        assert body["agent_available"] is True
        assert body["counts"]["labs"] == 3
        assert body["counts"]["equipment"] == 6

    async def test_health_never_leaks_secrets(self, client):
        body = (await client.get("/api/health")).json()
        assert "llm_api_key" not in body
        assert "api_key" not in str(body).lower()

    async def test_tools_listed(self, client):
        resp = await client.get("/api/tools")
        names = {item["name"] for item in resp.json()["tools"]}
        assert {"query_availability", "create_reservation", "cancel_reservation",
                "check_admission"} <= names

    async def test_today(self, client):
        body = (await client.get("/api/today")).json()
        assert "date" in body and "weekday" in body


class TestResources:
    async def test_labs_include_equipment(self, client):
        labs = (await client.get("/api/labs")).json()
        assert len(labs) == 3
        assert all("equipment" in lab for lab in labs)
        codes = {item["code"] for lab in labs for item in lab["equipment"]}
        assert "SPEC-F7000" in codes

    async def test_users_expose_certs(self, client):
        users = (await client.get("/api/users")).json()
        assert len(users) == 3
        zhang = next(u for u in users if u["username"] == "张伟")
        assert zhang["certs"] == ["光谱"]

    async def test_reservations_seeded(self, client):
        rows = (await client.get("/api/reservations")).json()
        assert len(rows) == 2
        assert all(row["equipment_name"] for row in rows)

    async def test_reservations_filtered_by_user(self, client):
        rows = (await client.get("/api/reservations", params={"user_id": 1})).json()
        assert len(rows) == 1


class TestChat:
    async def test_chat_returns_proposals(self, client):
        resp = await client.post(
            "/api/agent/chat",
            json={"message": "明天下午两点想用荧光光谱仪两小时", "user_id": 2, "session_id": "api"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["intent"] == "create_reservation"
        assert body["proposals"]
        assert body["trace"]

    async def test_chat_books_and_can_be_cancelled(self, client):
        booked = await client.post(
            "/api/agent/chat",
            json={
                "message": "明天上午十点到十一点，紫外可见分光光度计",
                "user_id": 2,
                "session_id": "api2",
            },
        )
        body = booked.json()
        assert body["booking"]["ok"] is True
        reservation_id = body["booking"]["reservation"]["id"]

        cancelled = await client.post(
            "/api/reservations/cancel",
            json={"reservation_id": reservation_id, "user_id": 2, "reason": "接口测试"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["ok"] is True

    async def test_chat_empty_message_rejected(self, client):
        resp = await client.post("/api/agent/chat", json={"message": "", "user_id": 2})
        assert resp.status_code == 422

    async def test_chat_unknown_user_is_handled(self, client):
        resp = await client.post(
            "/api/agent/chat",
            json={"message": "我想约个设备", "user_id": 9999, "session_id": "api3"},
        )
        assert resp.status_code == 200

    async def test_degraded_mode_reports_flag(self, client):
        client_app = app
        original = client_app.state.agent
        try:
            from lagent.agent.graph import LabBookingAgent

            client_app.state.agent = LabBookingAgent(None)
            resp = await client.post(
                "/api/agent/chat", json={"message": "你好", "user_id": 2}
            )
            body = resp.json()
            assert body["degraded"] is True
        finally:
            client_app.state.agent = original


class TestRetrievalEndpoint:
    async def test_retrieve_hits(self, client):
        resp = await client.get("/api/retrieve", params={"q": "离心机 配平", "k": 3})
        assert resp.status_code == 200
        body = resp.json()
        assert body["backend"] == "bm25"
        assert body["hits"]
        assert body["hits"][0]["heading"] == "离心类设备使用规范"
        assert body["hits"][0]["score"] > 0

    async def test_retrieve_requires_query(self, client):
        assert (await client.get("/api/retrieve")).status_code == 422


class TestCancelErrors:
    async def test_cancel_unknown_returns_409(self, client):
        resp = await client.post(
            "/api/reservations/cancel", json={"reservation_id": 999999, "user_id": 2}
        )
        assert resp.status_code == 409

    async def test_cancel_others_returns_409(self, client):
        rows = (await client.get("/api/reservations", params={"user_id": 1})).json()
        resp = await client.post(
            "/api/reservations/cancel", json={"reservation_id": rows[0]["id"], "user_id": 2}
        )
        assert resp.status_code == 409


class TestConsolePage:
    async def test_index_served(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
