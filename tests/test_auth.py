"""认证与授权：密码哈希、JWT 签名/过期/篡改、RBAC、身份不可伪造。

这里的每一条都在防一个具体的攻击，而不是在测「函数返回了东西」：

* ``TestPasswordHashing``  —— 同一口令两次哈希必须不同（有盐）；坏哈希串不能抛异常
* ``TestTokenIntegrity``   —— 改载荷、改算法、去签名、过期，**四种伪造都必须被拒**
* ``TestLogin``            —— 账号不存在与口令错误返回**完全相同**的响应（防账号枚举）
* ``TestAuthorization``    —— 401（没认证）与 403（认证了但权限不够）语义不能混
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from lagent.security import (
    ALGORITHM,
    DEFAULT_JWT_SECRET,
    TokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    principal_from_token,
    uses_default_secret,
    verify_password,
)

# 测试里把 KDF 调低（见 conftest），显式传参以免依赖环境变量顺序
FAST_N = 2 ** 10


# ==========================================================================
# 密码哈希
# ==========================================================================
class TestPasswordHashing:
    def test_roundtrip(self):
        stored = hash_password("lina@123", n=FAST_N)
        assert verify_password("lina@123", stored) is True
        assert verify_password("lina@124", stored) is False

    def test_never_stores_plaintext(self):
        stored = hash_password("s3cret-pw", n=FAST_N)
        assert "s3cret-pw" not in stored

    def test_salted_so_same_password_differs(self):
        """两次哈希同一口令必须不同 —— 否则等于「相同口令的账号可被一眼看穿」。"""
        a = hash_password("same-password", n=FAST_N)
        b = hash_password("same-password", n=FAST_N)
        assert a != b
        assert verify_password("same-password", a)
        assert verify_password("same-password", b)

    def test_params_are_embedded_so_cost_can_be_raised_later(self):
        """成本参数写进哈希串，改默认值不会让存量密码失效。"""
        stored = hash_password("pw", n=FAST_N)
        head = stored.split("$")
        assert head[0] == "scrypt"
        assert int(head[1]) == FAST_N
        # 用一个不同的默认成本也能校验通过（用的是串里的 n）
        assert verify_password("pw", stored) is True

    def test_supports_non_ascii_password(self):
        stored = hash_password("口令-中文-🔐", n=FAST_N)
        assert verify_password("口令-中文-🔐", stored) is True
        assert verify_password("口令-中文", stored) is False

    @pytest.mark.parametrize("garbage", [
        "", "not-a-hash", "scrypt$bad$1$1$aa$bb", "bcrypt$1024$8$1$aa$bb",
        "scrypt$1024$8$1$!!!$!!!", "scrypt$1024$8$1$aa",
    ])
    def test_malformed_hash_returns_false_not_exception(self, garbage):
        """坏哈希串一律 False —— 不能把内部格式错误变成 500 或异常。"""
        assert verify_password("anything", garbage) is False

    def test_empty_password_rejected(self):
        with pytest.raises(ValueError):
            hash_password("")


# ==========================================================================
# JWT
# ==========================================================================
class TestTokenIntegrity:
    def test_roundtrip_carries_identity(self):
        token = create_access_token(user_id=42, username="李娜", role="user", ttl_seconds=60)
        principal = principal_from_token(token)
        assert principal.user_id == 42
        assert principal.username == "李娜"
        assert principal.is_admin is False

    def test_admin_flag(self):
        token = create_access_token(user_id=3, username="管理员", role="admin", ttl_seconds=60)
        assert principal_from_token(token).is_admin is True

    def test_expired_token_rejected(self):
        token = create_access_token(user_id=1, username="a", role="user", ttl_seconds=10, now_ts=1000)
        assert decode_access_token(token, now_ts=1005)  # 未过期
        with pytest.raises(TokenError, match="过期"):
            decode_access_token(token, now_ts=1011)

    def test_tampered_payload_rejected(self):
        """把 user_id 从 1 改成 9999 并保持原签名 —— 签名校验必须发现。"""
        token = create_access_token(user_id=1, username="a", role="user", ttl_seconds=600)
        head, _, signature = token.split(".")
        forged = _b64u({"sub": "9999", "name": "a", "role": "admin",
                        "iat": int(time.time()), "exp": int(time.time()) + 600})
        with pytest.raises(TokenError, match="签名"):
            decode_access_token(f"{head}.{forged}.{signature}")

    def test_role_escalation_rejected(self):
        """普通用户把 role 改成 admin 也必须失败（这是本项目最要紧的一条）。"""
        token = create_access_token(user_id=2, username="李娜", role="user", ttl_seconds=600)
        head, payload, signature = token.split(".")
        original = json.loads(_b64u_decode(payload))
        original["role"] = "admin"
        with pytest.raises(TokenError):
            decode_access_token(f"{head}.{_b64u(original)}.{signature}")

    def test_alg_none_rejected(self):
        """★ 经典 alg 混淆攻击：把算法改成 none、去掉签名。

        服务端若「按 header 里声明的算法验签」，就会直接放行。
        """
        _, payload, _ = create_access_token(
            user_id=1, username="a", role="admin", ttl_seconds=600
        ).split(".")
        forged_header = _b64u({"alg": "none", "typ": "JWT"})
        with pytest.raises(TokenError, match="算法"):
            decode_access_token(f"{forged_header}.{payload}.")

    def test_unsigned_token_rejected(self):
        token = create_access_token(user_id=1, username="a", role="user", ttl_seconds=600)
        head, payload, _ = token.split(".")
        with pytest.raises(TokenError):
            decode_access_token(f"{head}.{payload}.")

    def test_missing_exp_rejected(self):
        """没有 exp 的 token 等于永久凭证，必须拒绝。"""
        header = _b64u({"alg": ALGORITHM, "typ": "JWT"})
        payload = _b64u({"sub": "1", "role": "admin"})   # 无 exp
        token = _sign(f"{header}.{payload}")
        with pytest.raises(TokenError, match="过期时间"):
            decode_access_token(token)

    def test_missing_sub_rejected(self):
        header = _b64u({"alg": ALGORITHM, "typ": "JWT"})
        payload = _b64u({"role": "admin", "exp": int(time.time()) + 600})
        with pytest.raises(TokenError, match="主体"):
            decode_access_token(_sign(f"{header}.{payload}"))

    def test_non_int_sub_rejected(self):
        header = _b64u({"alg": ALGORITHM, "typ": "JWT"})
        payload = _b64u({"sub": "abc", "exp": int(time.time()) + 600})
        with pytest.raises(TokenError):
            principal_from_token(_sign(f"{header}.{payload}"))

    @pytest.mark.parametrize("bad", ["", "abc", "a.b", "a.b.c.d", "....", "👻.👻.👻"])
    def test_malformed_token_rejected(self, bad):
        with pytest.raises(TokenError):
            decode_access_token(bad)

    def test_wrong_secret_rejected(self, monkeypatch):
        token = create_access_token(user_id=1, username="a", role="user", ttl_seconds=600)
        from lagent.config import reset_settings_cache

        monkeypatch.setenv("LAB_JWT_SECRET", "another-secret")
        reset_settings_cache()
        try:
            with pytest.raises(TokenError, match="签名"):
                decode_access_token(token)
        finally:
            reset_settings_cache()

    def test_default_secret_is_flagged(self):
        assert uses_default_secret() is True
        assert DEFAULT_JWT_SECRET  # 常量必须真的有值，否则告警逻辑形同虚设


# ==========================================================================
# 登录端点
# ==========================================================================
class TestLogin:
    async def test_login_success(self, http):
        resp = await http.post(
            "/api/auth/login", json={"username": "李娜", "password": "lina@123"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0
        assert body["user"]["username"] == "李娜"
        assert len(body["access_token"].split(".")) == 3

    async def test_login_response_omits_hash(self, http):
        body = (
            await http.post("/api/auth/login", json={"username": "张伟", "password": "zhangwei@123"})
        ).json()
        assert "password_hash" not in json.dumps(body)
        assert body["user"]["certs"] == ["光谱"]

    async def test_wrong_password_is_401(self, http):
        resp = await http.post(
            "/api/auth/login", json={"username": "李娜", "password": "wrong"}
        )
        assert resp.status_code == 401

    async def test_unknown_user_same_response_as_wrong_password(self, http):
        """账号是否存在**不能**从响应里分辨出来，否则等于一个账号枚举接口。"""
        unknown = await http.post(
            "/api/auth/login", json={"username": "查无此人", "password": "whatever"}
        )
        wrong = await http.post(
            "/api/auth/login", json={"username": "李娜", "password": "wrong"}
        )
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["detail"] == wrong.json()["detail"]

    async def test_login_requires_both_fields(self, http):
        assert (await http.post("/api/auth/login", json={"username": "李娜"})).status_code == 422
        assert (await http.post("/api/auth/login", json={})).status_code == 422

    async def test_token_works_on_me(self, http, as_user):
        headers = await as_user("张伟")
        body = (await http.get("/api/auth/me", headers=headers)).json()
        assert body["username"] == "张伟"
        assert body["certs"] == ["光谱"]

    async def test_issued_token_is_accepted(self, http):
        token = (
            await http.post("/api/auth/login", json={"username": "管理员", "password": "admin@123"})
        ).json()["access_token"]
        resp = await http.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"


# ==========================================================================
# 授权的两种失败语义
# ==========================================================================
class TestAuthorization:
    async def test_missing_header_is_401(self, http):
        resp = await http.get("/api/auth/me")
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"

    async def test_garbage_token_is_401(self, http):
        resp = await http.get("/api/auth/me", headers={"Authorization": "Bearer not.a.token"})
        assert resp.status_code == 401

    async def test_wrong_scheme_is_401(self, http):
        resp = await http.get("/api/auth/me", headers={"Authorization": "Basic abc123"})
        assert resp.status_code == 401

    async def test_expired_token_is_401_over_http(self, http):
        expired = create_access_token(
            user_id=1, username="张伟", role="user", ttl_seconds=1, now_ts=1000
        )
        resp = await http.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
        assert resp.status_code == 401

    async def test_authenticated_but_forbidden_is_403_not_401(self, http, as_user):
        """403 与 401 必须分开：前端据此决定「去登录」还是「找管理员」。"""
        resp = await http.get("/api/users", headers=await as_user("张伟"))
        assert resp.status_code == 403

    async def test_tampered_role_cannot_reach_admin_endpoint(self, http, as_user):
        """普通用户把令牌载荷里的 role 改成 admin，直接打管理端点 —— 必须 401。

        这是「越权」最直接的形态：不改密码、不猜口令，只改一个字段。
        之所以拦得住，是因为签名把整个载荷绑在了一起。
        """
        real = await as_user("张伟")
        _, payload, signature = real["Authorization"].split(" ")[1].split(".")
        assert (await http.get("/api/users", headers=real)).status_code == 403

        tampered = json.loads(_b64u_decode(payload))
        tampered["role"] = "admin"
        header = _b64u({"alg": ALGORITHM, "typ": "JWT"})
        bad = f"{header}.{_b64u(tampered)}.{signature}"
        resp = await http.get("/api/users", headers={"Authorization": f"Bearer {bad}"})
        assert resp.status_code == 401, "改了 role 的令牌竟然通过了签名校验"


# ==========================================================================
# 工具
# ==========================================================================
def _b64u(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> str:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("utf-8")


def _sign(signing_input: str) -> str:
    """用配置里的密钥给任意签名输入补一个合法签名（用于构造"合法但语义非法"的令牌）。"""
    import hashlib
    import hmac

    from lagent.config import get_settings

    digest = hmac.new(
        get_settings().jwt_secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{signing_input}.{base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')}"
