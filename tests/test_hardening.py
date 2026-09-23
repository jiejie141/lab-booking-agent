"""P0-3 边界加固：请求体上限、CORS 白名单、限流、输入白名单。

对应 ``docs/ENTERPRISE-UPGRADE.md`` §P0-3 的验收要求 ——
一份最小越权/滥用清单：无 token 写、越权读改、超大 body、高频刷接口。

这里刻意**不**只测"正常路径也能过"：每条用例都在断言**攻击/滥用路径被挡住**，
因为加固代码最容易出现的问题不是"没写"，而是"写了但没生效"
（中间件挂错位置、白名单是空的、限流器是另一个实例……）。
"""

from __future__ import annotations

import contextlib
import json
from typing import Any, cast

import httpx
from conftest import auth_header, login


@contextlib.asynccontextmanager
async def app_client(monkeypatch, **env):
    """按环境变量造一个**独立**的应用实例。

    必须这样测：CORS 白名单和体积上限都是 ``create_app()`` 构造时读进来的，
    对着模块级单例测等于只测了"生产那一份配置"。
    """
    for key, value in env.items():
        monkeypatch.setenv(f"LAB_{key.upper()}", str(value))

    from lagent.api import create_app
    from lagent.config import reset_settings_cache

    reset_settings_cache()
    application = create_app()
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            cast(Any, client).app = application
            yield client
    reset_settings_cache()


# ==========================================================================
# 请求体大小上限
# ==========================================================================
class TestBodySizeLimit:
    async def test_oversized_body_is_413(self, http, as_user):
        """超大 body 必须在**校验之前**就被挡住（413 而不是 422）。"""
        resp = await http.post(
            "/api/agent/chat",
            content=json.dumps({"message": "x" * 200_000}).encode("utf-8"),
            headers={"Content-Type": "application/json", **(await as_user("李娜"))},
        )
        assert resp.status_code == 413
        assert "上限" in resp.json()["detail"]

    async def test_normal_body_passes(self, http, as_user):
        resp = await http.post(
            "/api/agent/chat",
            json={"message": "你好", "session_id": "size-ok"},
            headers=await as_user("李娜"),
        )
        assert resp.status_code == 200

    async def test_limit_is_configurable(self, isolated_db, monkeypatch):
        """把上限调到 200 字节，一个正常请求就该被拒 —— 证明读的是配置。"""
        async with app_client(monkeypatch, max_body_bytes=200) as client:
            headers = auth_header(await login(client, "李娜"))
            resp = await client.post(
                "/api/agent/chat",
                json={"message": "x" * 300},
                headers=headers,
            )
            assert resp.status_code == 413

    async def test_get_is_not_blocked_by_missing_length(self, http, as_user):
        """GET 没有 body，不该被 411 拦下。"""
        resp = await http.get("/api/health")
        assert resp.status_code == 200


class TestBodySizeLimitMiddlewareDirect:
    """直接驱动 ASGI 中间件，覆盖 httpx 造不出来的场景（分块传输）。"""

    async def _run(self, method: str, headers: list[tuple[bytes, bytes]], max_bytes: int):
        from lagent.api import BodySizeLimitMiddleware

        reached = {"inner": False}

        async def inner(scope, receive, send):
            reached["inner"] = True

        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        middleware = BodySizeLimitMiddleware(inner, max_bytes=max_bytes)
        await middleware({"type": "http", "method": method, "headers": headers}, receive, send)
        return reached["inner"], sent

    async def test_missing_content_length_is_411(self):
        """分块传输（不声明长度）无法在读取前度量，直接要求声明长度。"""
        reached, sent = await self._run("POST", [], 1024)
        assert reached is False, "不该把请求放行到下游"
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        assert status == 411

    async def test_bad_content_length_is_400(self):
        reached, sent = await self._run(
            "POST", [(b"content-length", b"not-a-number")], 1024
        )
        assert reached is False
        assert next(m["status"] for m in sent if m["type"] == "http.response.start") == 400

    async def test_within_limit_reaches_downstream(self):
        reached, sent = await self._run(
            "POST", [(b"content-length", b"512")], 1024
        )
        assert reached is True
        assert sent == []


# ==========================================================================
# CORS 白名单
# ==========================================================================
class TestCorsAllowlist:
    async def test_no_allowlist_means_no_cors_headers(self, http):
        """默认空白名单 = 不发 CORS 头（浏览器只允许同源）。"""
        resp = await http.get("/api/health", headers={"Origin": "http://evil.example.com"})
        assert "access-control-allow-origin" not in resp.headers

    async def test_allowed_origin_gets_header(self, isolated_db, monkeypatch):
        async with app_client(monkeypatch, cors_origins="http://console.example.com") as client:
            resp = await client.get(
                "/api/health", headers={"Origin": "http://console.example.com"}
            )
            assert resp.headers.get("access-control-allow-origin") == "http://console.example.com"

    async def test_unlisted_origin_gets_nothing(self, isolated_db, monkeypatch):
        """白名单是白名单：不在名单上的源拿不到 CORS 头。"""
        async with app_client(monkeypatch, cors_origins="http://console.example.com") as client:
            resp = await client.get("/api/health", headers={"Origin": "http://evil.example.com"})
            assert "access-control-allow-origin" not in resp.headers

    async def test_multiple_origins_parsed(self, isolated_db, monkeypatch):
        async with app_client(
            monkeypatch, cors_origins="http://a.example.com, http://b.example.com"
        ) as client:
            for origin in ("http://a.example.com", "http://b.example.com"):
                resp = await client.get("/api/health", headers={"Origin": origin})
                assert resp.headers.get("access-control-allow-origin") == origin


# ==========================================================================
# 限流
# ==========================================================================
class TestRateLimit:
    async def test_chat_is_rate_limited_per_user(self, isolated_db, monkeypatch):
        async with app_client(monkeypatch, rate_limit_per_minute=3) as client:
            headers = auth_header(await login(client, "李娜"))
            codes = []
            for i in range(5):
                resp = await client.post(
                    "/api/agent/chat",
                    json={"message": "你好", "session_id": f"rl-{i}"},
                    headers=headers,
                )
                codes.append(resp.status_code)
            assert codes[:3] == [200, 200, 200], codes
            assert codes[3] == 429, codes

    async def test_429_carries_retry_after(self, isolated_db, monkeypatch):
        async with app_client(monkeypatch, rate_limit_per_minute=1) as client:
            headers = auth_header(await login(client, "李娜"))
            await client.post("/api/agent/chat", json={"message": "你好"}, headers=headers)
            resp = await client.post("/api/agent/chat", json={"message": "你好"}, headers=headers)
            assert resp.status_code == 429
            assert int(resp.headers["retry-after"]) >= 1

    async def test_quota_is_per_user_not_global(self, isolated_db, monkeypatch):
        """按用户限流：一个人打满不该影响另一个人。

        按 IP 限流在这里会误伤 —— 同一个实验室出口 IP 后面站着几十个人。
        """
        async with app_client(monkeypatch, rate_limit_per_minute=1) as client:
            lina = auth_header(await login(client, "李娜"))
            zhang = auth_header(await login(client, "张伟"))
            assert (
                await client.post("/api/agent/chat", json={"message": "你好"}, headers=lina)
            ).status_code == 200
            assert (
                await client.post("/api/agent/chat", json={"message": "你好"}, headers=lina)
            ).status_code == 429
            assert (
                await client.post("/api/agent/chat", json={"message": "你好"}, headers=zhang)
            ).status_code == 200

    async def test_zero_disables_limit(self, isolated_db, monkeypatch):
        async with app_client(monkeypatch, rate_limit_per_minute=0) as client:
            headers = auth_header(await login(client, "李娜"))
            for _ in range(5):
                resp = await client.post(
                    "/api/agent/chat", json={"message": "你好"}, headers=headers
                )
                assert resp.status_code == 200

    async def test_limiter_does_not_grow_unbounded(self):
        """窗口过期后必须回收 key，否则按用户限流就是内存泄漏。"""
        from lagent.ratelimit import SlidingWindowLimiter

        limiter = SlidingWindowLimiter(limit=5, window_seconds=0.01)
        for i in range(200):
            limiter.hit(f"user:{i}")
        assert limiter.tracked_keys() == 200
        # 让窗口过去，再触发一次 sweep（超过 max_keys 才会清理）
        import time

        time.sleep(0.03)
        limiter.max_keys = 10
        limiter.hit("user:new")
        assert limiter.tracked_keys() <= 12


# ==========================================================================
# 输入白名单
# ==========================================================================
class TestInputValidation:
    async def test_oversized_session_id_rejected(self, http, as_user):
        resp = await http.post(
            "/api/agent/chat",
            json={"message": "你好", "session_id": "s" * 500},
            headers=await as_user("李娜"),
        )
        assert resp.status_code == 422

    async def test_oversized_reason_rejected(self, http, as_user):
        resp = await http.post(
            "/api/reservations/cancel",
            json={"reservation_id": 1, "reason": "r" * 500},
            headers=await as_user("李娜"),
        )
        assert resp.status_code == 422

    async def test_unknown_field_rejected_on_login(self, http):
        """未知字段 422 而不是静默忽略 —— 静默忽略会让拼错的参数看起来生效了。"""
        resp = await http.post(
            "/api/auth/login",
            json={"username": "李娜", "password": "lina@123", "role": "admin"},
        )
        assert resp.status_code == 422

    async def test_unknown_field_rejected_on_cancel(self, http, as_user):
        """老接口把 user_id 写在 body 里 —— 现在明确报错，而不是"取消了自己"。"""
        resp = await http.post(
            "/api/reservations/cancel",
            json={"reservation_id": 1, "user_id": 2},
            headers=await as_user("李娜"),
        )
        assert resp.status_code == 422

    async def test_empty_message_still_rejected(self, http, as_user):
        resp = await http.post(
            "/api/agent/chat", json={"message": ""}, headers=await as_user("李娜")
        )
        assert resp.status_code == 422

    async def test_negative_reservation_id_allowed_to_reach_business_layer(
        self, http, as_user
    ):
        """负数 ID 不是注入，交给业务层回 409（"不存在"），不必在 schema 层拦。"""
        resp = await http.post(
            "/api/reservations/cancel",
            json={"reservation_id": -1},
            headers=await as_user("李娜"),
        )
        assert resp.status_code == 409
