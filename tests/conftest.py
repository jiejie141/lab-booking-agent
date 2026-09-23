"""测试夹具：每个用例一套独立的 SQLite 库。

要点是**每例换一个库文件**并重置引擎单例。db.py 里的引擎是模块级单例，
如果测试之间共用，前一个用例写进去的预约会在后一个用例里变成「冲突」，
于是测试时灵时不灵 —— 而且这种失败看起来像业务 bug。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# 演示口令固定（seed.py），测试里会反复登录。
# scrypt 默认 n=2**14 ≈ 140ms/次，几十次登录就是十几秒纯等待 ——
# 这里调到 2**10 把等待压到 ~1ms。**成本参数本身不是被测对象**
# （它是 KDF 的实现细节，且 n 已写进哈希串，不影响校验正确性），
# 所以测试里降低它不损失覆盖面，只省时间。
TEST_KDF_N = "1024"

# 演示账号（与 seed.USERS 同一份，README 也列了）
DEMO_PASSWORD = {"张伟": "zhangwei@123", "李娜": "lina@123", "管理员": "admin@123"}


@pytest.fixture
async def isolated_db(tmp_path, monkeypatch):
    """干净的库 + 种子数据。"""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("LAB_DATABASE_URL", url)
    monkeypatch.setenv("LAB_APP_MODE", "mock")
    monkeypatch.setenv("LAB_PASSWORD_KDF_N", TEST_KDF_N)

    from lagent import db as db_module
    from lagent.config import reset_settings_cache

    reset_settings_cache()
    await db_module.dispose_engine()
    await db_module.init_db()

    from lagent.seed import seed

    await seed(force=True)

    yield db_module

    await db_module.dispose_engine()
    reset_settings_cache()


@pytest.fixture
async def catalog(isolated_db):
    """设备目录 (名称, 类别)，供 Mock 模型识别设备。"""
    from lagent.agent.tools import load_catalog

    return await load_catalog()


@pytest.fixture
def mock_client(catalog):
    from lagent.agent.llm import MockLLMClient

    return MockLLMClient(catalog)


@pytest.fixture
def agent(mock_client):
    from lagent.agent.graph import LabBookingAgent
    from lagent.agent.state import SessionStore

    return LabBookingAgent(mock_client, store=SessionStore())


# --------------------------------------------------------------------------
# HTTP 层夹具（P0-2 之后所有业务端点都要令牌）
# --------------------------------------------------------------------------
@pytest.fixture
async def http(isolated_db):
    """已进入 lifespan 的 ASGI 客户端（未登录）。

    刻意用 ``create_app()`` 而不是模块级的 ``app`` 单例：
    中间件（CORS 白名单、请求体上限）的配置是在 ``create_app()`` 里读进来的，
    用同一个单例就没法验证"换了配置真的会变"。
    """
    import httpx

    from lagent.api import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            client.app = application  # 便于测试直接改 app.state
            yield client


def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def login(http, username: str, password: str | None = None) -> str:
    """登录并返回访问令牌。"""
    resp = await http.post(
        "/api/auth/login",
        json={"username": username, "password": password or DEMO_PASSWORD[username]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest.fixture
def as_user(http):
    """``as_user("张伟")`` → (client 不变, 该身份的请求头)。

    用法::

        headers = await as_user("管理员")
        r = await http.get("/api/users", headers=headers)
    """

    async def _login(username: str, password: str | None = None) -> dict:
        return auth_header(await login(http, username, password))

    return _login
