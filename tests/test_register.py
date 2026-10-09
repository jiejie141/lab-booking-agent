"""自助注册（P2-2）：拿号、安全边界、审计。

每一条都在钉一个具体的安全约束，而不是在测"接口返回了东西"：

* 注册成功的角色必须是最低级（user）—— 提权只能走管理员的 user.manage；
* 请求里带 role 字段必须 422 —— extra=forbid 不是摆设，静默忽略会让
  "带 role 注册"看起来成功了；
* 用户名/邮箱冲突统一 409、不区分是哪个撞了 —— 与 login 同一条反枚举思路；
* 关闭开关 → 403；同 IP 超配额 → 429；
* 注册进审计 —— 管理员能在 /api/audit 里看见 auth.register。

环境相关用例（关闭开关 / 限流配额）必须让 env 在 **create_app() 之前**生效：
``http`` 夹具的 lifespan 在那一刻读取配置，测试体里再改 env 就晚了。
所以这里不复用 conftest 的 ``http``，而是复制它的组装并留出注入点 ——
两份组装的差异（多两行 setenv）是显式的，好过隐式的全局参数。
"""

from __future__ import annotations

import pytest
from conftest import login

REGISTER_BODY = {
    "username": "newstu",
    "email": "newstu@stu.example.com",
    "password": "passw0rd123",
}


async def _launch(monkeypatch, isolated_db, extra_env: dict[str, str]):
    """isolated_db 之后追加环境变量，再组装一个带 lifespan 的客户端。"""
    for key, value in extra_env.items():
        monkeypatch.setenv(key, value)
    from lagent.config import reset_settings_cache

    reset_settings_cache()

    import httpx

    from lagent.api import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
async def reg_off_http(monkeypatch, isolated_db):
    async for client in _launch(monkeypatch, isolated_db, {"LAB_REGISTRATION_ENABLED": "false"}):
        yield client


@pytest.fixture
async def reg_limited_http(monkeypatch, isolated_db):
    async for client in _launch(monkeypatch, isolated_db, {"LAB_REGISTER_PER_HOUR": "2"}):
        yield client


async def _register(http, **overrides) -> tuple[int, dict]:
    body = {**REGISTER_BODY, **overrides}
    resp = await http.post("/api/auth/register", json=body)
    return resp.status_code, _json(resp)


def _json(resp):
    return resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}


# ==========================================================================
# 正路径
# ==========================================================================
class TestRegister:
    async def test_register_then_token_works(self, http):
        status, body = await _register(http)
        assert status == 200, body
        assert body["access_token"]
        me = await http.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
        )
        assert me.status_code == 200
        assert me.json()["username"] == "newstu"
        assert me.json()["role"] == "user"  # 最低级，没有之一

    async def test_registered_account_can_login(self, http):
        status, _ = await _register(http)
        assert status == 200
        resp = await http.post(
            "/api/auth/login",
            json={"username": "newstu", "password": "passw0rd123"},
        )
        assert resp.status_code == 200

    async def test_register_is_audited(self, http):
        status, _ = await _register(http)
        assert status == 200
        token = await login(http, "管理员")
        resp = await http.get(
            "/api/audit",
            params={"action": "auth.register"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert any(row["actor_name"] == "newstu" for row in rows)


# ==========================================================================
# 安全边界
# ==========================================================================
class TestRegisterBoundaries:
    async def test_role_field_is_rejected(self, http):
        status, _ = await _register(http, role="sysadmin")
        assert status == 422  # extra=forbid：不是忽略，是拒绝

    async def test_certs_field_is_rejected(self, http):
        status, _ = await _register(http, certs=["光谱"])
        assert status == 422  # 资质只能由管理员授予，注册接口不收

    async def test_duplicate_username_is_409(self, http):
        status, _ = await _register(http)
        assert status == 200
        status, _ = await _register(http, email="other@stu.example.com")
        assert status == 409

    async def test_duplicate_email_is_409(self, http):
        status, _ = await _register(http)
        assert status == 200
        status, _ = await _register(http, username="otherstu")
        assert status == 409

    async def test_short_password_rejected(self, http):
        status, _ = await _register(http, password="short")
        assert status == 422

    async def test_bad_username_rejected(self, http):
        # 空格与超长都不行：用户名会进审计与日志，不接受自由文本
        status, _ = await _register(http, username="白 鸽")
        assert status == 422
        status, _ = await _register(http, username="ab")
        assert status == 422

    async def test_bad_email_rejected(self, http):
        status, _ = await _register(http, email="not-an-email")
        assert status == 422

    async def test_disabled_returns_403(self, reg_off_http):
        status, _ = await _register(reg_off_http)
        assert status == 403

    async def test_per_ip_rate_limit(self, reg_limited_http):
        # 配额 2：两个不同用户名用完配额，第三个请求无论用什么身份都 429
        for i in range(2):
            status, _ = await _register(
                reg_limited_http,
                username=f"stu{i}",
                email=f"stu{i}@stu.example.com",
            )
            assert status == 200
        status, _ = await _register(
            reg_limited_http, username="stu9", email="stu9@stu.example.com"
        )
        assert status == 429
