"""HTTP 层：健康检查、认证、对话、资源查询、取消与检索。

P0-2 之后这里多了一层前提：**除存活探针与登录外，所有端点都要令牌**。
因此本文件的重点之一是「未认证一律 401」，见 TestAuthRequired。
"""

from __future__ import annotations

import datetime as dt
from typing import ClassVar

import pytest

TOMORROW = dt.date.today() + dt.timedelta(days=1)


class TestPublicEndpoints:
    """无需令牌的端点白名单。它短且集中，是鉴权设计的核心约束。

    P1-4 之后这个白名单**没有变长**，反而更精确了：健康检查拆成三档，
    公开的只有「存活」与「就绪」——它们**必须**公开（编排系统的探针不带凭据，
    要凭据的探针等于没探针），但都只回"能不能用"，不回任何业务数字。
    """

    async def test_health_is_liveness_only(self, http):
        """存活探针只证明「进程还能响应」，**一个业务字段都没有**。"""
        resp = await http.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["app_mode"] == "mock"
        assert isinstance(body["uptime_seconds"], (int, float))
        # 这几个字段是 P1-4 从公开接口上**搬走**的：匿名调用者不该读到
        # "库里有多少用户、多少条预约"。断言"它们不在"才是这条用例的重点。
        assert "counts" not in body
        assert "database" not in body
        assert "retrieval_backend" not in body

    async def test_health_survives_a_dead_database(self, http, monkeypatch):
        """★ 存活探针不许依赖任何下游 —— 这是"健康检查为什么要拆"的全部理由。

        数据库一抖，如果存活探针跟着失败，编排系统就会不停地重启一个
        **完全健康**的进程：重启修不好数据库，却把一次降级放大成"服务一直在重启"。
        所以这里把数据库调用整个打断，存活探针必须仍然是 200。
        """
        import lagent.api as api_module

        def boom():
            raise RuntimeError("数据库不可达（模拟）")

        monkeypatch.setattr(api_module, "session_scope", boom)
        resp = await http.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_ready_reports_each_dependency(self, http):
        resp = await http.get("/api/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        checks = {check["name"]: check for check in body["checks"]}
        assert set(checks) == {"database", "agent", "retrieval", "sweeper"}
        for check in checks.values():
            assert isinstance(check["ok"], bool)
            assert isinstance(check["critical"], bool)
            assert check["detail"], "每一项都要说明白为什么是这个结论"
        # 就绪探针也**不能**泄露业务量 —— 它是公开的
        assert "counts" not in body

    async def test_ready_is_503_when_the_database_is_dead(self, http, monkeypatch):
        """★ 同一件事在就绪探针上必须**相反**：数据库没了，服务就是不能干活。

        两条要求合起来才是完整设计：「能响」与「能干活」是两件事，
        用一个接口回答它们必然是错的（要么探针误杀、要么编排系统永远以为就绪）。
        """
        import lagent.api as api_module

        def boom():
            raise RuntimeError("数据库不可达（模拟）")

        monkeypatch.setattr(api_module, "session_scope", boom)
        resp = await http.get("/api/health/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "not_ready"
        database = next(c for c in body["checks"] if c["name"] == "database")
        assert database["ok"] is False
        assert database["critical"] is True
        assert "数据库不可达（模拟）" in database["detail"]

    async def test_a_designed_degradation_does_not_block_readiness(self, http, monkeypatch):
        """一次**设计内**的降级不该把服务判成"不能用"。

        检索缺可选依赖会回退到内置 BM25 —— 服务照常工作、检索也照常返回结果。
        若把它算成致命项，探针会一直红，然后就没有人再看探针了。
        所以这条钉的是 ``critical=False`` 这个字段真的在起作用。
        """
        import lagent.api as api_module

        monkeypatch.setattr(api_module, "fallback_reason", lambda: "未安装 chromadb")
        resp = await http.get("/api/health/ready")
        assert resp.status_code == 200
        retrieval = next(c for c in resp.json()["checks"] if c["name"] == "retrieval")
        assert retrieval["ok"] is False
        assert retrieval["critical"] is False

    async def test_details_requires_a_token(self, http):
        resp = await http.get("/api/health/details")
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == "Bearer"

    async def test_details_returns_counts_to_a_logged_in_user(self, http, as_user):
        resp = await http.get("/api/health/details", headers=await as_user("张伟"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["agent_available"] is True
        assert body["counts"]["labs"] == 3
        assert body["counts"]["equipment"] == 6

    async def test_health_never_leaks_secrets(self, http, as_user):
        headers = await as_user("张伟")
        for path in ("/api/health", "/api/health/ready", "/api/health/details"):
            body = (await http.get(path, headers=headers)).json()
            assert "llm_api_key" not in body, path
            assert "jwt_secret" not in body, path
            assert "api_key" not in str(body).lower(), path

    async def test_index_served(self, http):
        resp = await http.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


class TestAuthRequired:
    """默认拒绝：拿掉令牌，业务端点必须一律 401。"""

    PROTECTED: ClassVar[list[tuple[str, str]]] = [
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


class TestReservationPagination:
    """分页（P2）：limit/offset 可选，总数走 X-Total-Count。

    刻意**没**把返回形状改成信封 —— 那会让所有既有调用方一起改一遍，
    而分页在「明确可延后」那一组里。所以这里钉住的是"可选的、不破坏现状"。
    """

    async def test_without_limit_nothing_is_dropped(self, http, as_user):
        """不传 limit = 现状：全量返回。老调用方不该因为这次改动而少拿数据。"""
        headers = await as_user("管理员")
        resp = await http.get("/api/reservations", headers=headers)
        assert len(resp.json()) == 2
        assert resp.headers["X-Total-Count"] == "2"

    async def test_limit_slices_and_total_stays_the_same(self, http, as_user):
        """总数不跟着页走 —— 否则前端算不出"还有几页"。"""
        headers = await as_user("管理员")
        page = await http.get("/api/reservations?limit=1", headers=headers)
        assert len(page.json()) == 1
        assert page.headers["X-Total-Count"] == "2"

    async def test_offset_walks_to_the_next_page(self, http, as_user):
        headers = await as_user("管理员")
        first = (await http.get("/api/reservations?limit=1&offset=0", headers=headers)).json()
        second = (await http.get("/api/reservations?limit=1&offset=1", headers=headers)).json()
        assert first[0]["id"] != second[0]["id"]

    async def test_out_of_range_limit_is_rejected(self, http, as_user):
        """上限要挡住 —— 不设上限的话 ?limit=99999999 就是一条免费的 OOM。"""
        headers = await as_user("管理员")
        assert (await http.get("/api/reservations?limit=0", headers=headers)).status_code == 422
        assert (await http.get("/api/reservations?limit=501", headers=headers)).status_code == 422

    async def test_pagination_does_not_widen_the_scope(self, http, as_user):
        """★ 分页不能成为越权的旁道：翻到别人的页上也要被拦。"""
        headers = await as_user("张伟")
        rows = (
            await http.get(
                "/api/reservations?limit=10&offset=0",
                params={"user_id": 2},
                headers=headers,
            )
        ).json()
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
