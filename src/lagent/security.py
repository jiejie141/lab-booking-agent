"""认证与授权的原语：密码哈希（scrypt）+ HS256 JWT + RBAC 判定。

设计取舍 —— 为什么这里**没有**引入 ``pyjwt`` / ``passlib[bcrypt]``：

1. **零依赖是刻意的。** 一个 HS256 JWT 的全部内容就是
   ``base64url(header).base64url(payload).base64url(HMAC-SHA256(secret, 前两段))``，
   自己写几十行就把每个字节的来源讲清楚了；换成 pyjwt 也只是同一件事加一层封装。
   本项目在检索层（手写 BM25）、前端（零构建单文件）做了同样的取舍。
2. **密码哈希用标准库 ``hashlib.scrypt``。** 它是*内存硬* KDF（默认 16MB 工作区），
   抗 GPU 暴力破解的能力不低于 bcrypt，且不需要编译扩展 ——
   Windows 上装 bcrypt 经常要 MSVC 工具链，对「开箱即跑」是实打实的摩擦。
3. **接口刻意与 pyjwt 对齐**（``create_access_token`` / ``decode_access_token``），
   要换成真库只需改这两个函数体，调用方一行不动。
4. **KDF 参数写进哈希串**（``scrypt$n$r$p$salt$hash``），
   所以以后提高成本参数不会让存量密码失效 —— 这是自己写格式时必须做对的一点。

安全要点（每条都有对应测试，见 ``tests/test_auth.py``）：

* 签名比较用 ``hmac.compare_digest``，**不用 ``==``**（防时序侧信道）
* header 的 ``alg`` 必须**严格等于** ``HS256`` —— 否则攻击者可以把算法改成 ``none`` 绕过签名
* 必须校验 ``exp``，且**缺 exp 的 token 一律拒绝**（否则等于签发了永久凭证）
* 密码比对同样走 ``compare_digest``
* 密码哈希解析失败一律返回 ``False``，绝不抛异常（不把内部格式泄露成 500）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from pydantic import BaseModel

from .config import get_settings
from .models import ROLE_ADMIN, ROLE_SYSADMIN, ROLE_USER

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
ALGORITHM = "HS256"
_KDF_NAME = "scrypt"
# 成本参数默认值（可在 config 里覆盖）。n 必须是 2 的幂。
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_DKLEN = 32


class TokenError(Exception):
    """token 无效（格式错 / 签名错 / 已过期）。

    调用方一律翻译成 401 且**不回传具体原因** —— 告诉攻击者是"签名错"还是
    "过期"没有业务价值，只是免费情报。
    """


# ---------------------------------------------------------------------------
# base64url（JWT 用的是去掉 '=' 填充的变体）
# ---------------------------------------------------------------------------
def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    # 补回被去掉的 '=' 填充，否则 base64 解码会直接报错
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------------------
# 密码哈希（scrypt）
# ---------------------------------------------------------------------------
def _maxmem(n: int, r: int) -> int:
    """scrypt 的内存上限参数，必须 >= 128*n*r（再留一倍余量）。

    不显式传时 OpenSSL 默认只给 32MB，参数一调大就会抛
    ``ValueError: memory limit exceeded`` —— 这是个很容易踩的坑。
    """
    return 128 * n * r * 2


def hash_password(password: str, *, n: int | None = None) -> str:
    """把明文密码转成可入库的哈希串：``scrypt$n$r$p$salt$hash``。"""
    if not password:
        raise ValueError("密码不能为空")
    cost = n or get_settings().password_kdf_n
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=cost,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_DKLEN,
        maxmem=_maxmem(cost, _SCRYPT_R),
    )
    return "$".join(
        (_KDF_NAME, str(cost), str(_SCRYPT_R), str(_SCRYPT_P),
         _b64u_encode(salt), _b64u_encode(digest))
    )


def verify_password(password: str, stored: str) -> bool:
    """校验明文密码与库里的哈希是否匹配。

    任何解析异常都吞掉并返回 ``False``：把格式错误暴露成异常，
    等于给攻击者一个"这个账号的哈希串坏了"的旁路信号。
    """
    if not password or not stored:
        return False
    try:
        name, cost_s, r_s, p_s, salt_s, digest_s = stored.split("$")
        if name != _KDF_NAME:
            return False
        cost, r, p = int(cost_s), int(r_s), int(p_s)
        salt = _b64u_decode(salt_s)
        expected = _b64u_decode(digest_s)
    except (ValueError, TypeError):
        return False

    try:
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt, n=cost, r=r, p=p, dklen=len(expected),
            maxmem=_maxmem(cost, r),
        )
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------------------
# JWT（HS256）
# ---------------------------------------------------------------------------
def create_access_token(
    *,
    user_id: int,
    username: str,
    role: str,
    ttl_seconds: int | None = None,
    now_ts: int | None = None,
) -> str:
    """签发一个 HS256 访问令牌。"""
    settings = get_settings()
    issued = int(time.time() if now_ts is None else now_ts)
    ttl = settings.jwt_ttl_minutes * 60 if ttl_seconds is None else ttl_seconds

    header = {"alg": ALGORITHM, "typ": "JWT"}
    payload = {
        # sub 按 RFC 7519 必须是字符串，这里存 str(user_id)
        "sub": str(user_id),
        "name": username,
        "role": role,
        "iat": issued,
        "exp": issued + ttl,
    }
    # 紧凑序列化：不带空格，与 JWT 惯例一致（也避免 base64 里出现意外字符）
    dumping = {"ensure_ascii": False, "separators": (",", ":"), "sort_keys": True}
    signing_input = ".".join((
        _b64u_encode(json.dumps(header, **dumping).encode("utf-8")),
        _b64u_encode(json.dumps(payload, **dumping).encode("utf-8")),
    ))
    signature = hmac.new(
        settings.jwt_secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{signing_input}.{_b64u_encode(signature)}"


def decode_access_token(token: str, *, now_ts: int | None = None) -> dict:
    """校验并解出 token 载荷。任何问题都抛 ``TokenError``。"""
    if not token or token.count(".") != 2:
        raise TokenError("token 格式不正确")

    header_seg, payload_seg, signature_seg = token.split(".")
    try:
        header = json.loads(_b64u_decode(header_seg))
        payload = json.loads(_b64u_decode(payload_seg))
        provided = _b64u_decode(signature_seg)
    except (ValueError, TypeError) as exc:
        raise TokenError("token 无法解析") from exc

    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise TokenError("token 载荷结构不正确")

    # ★ 关键：算法必须严格匹配。若不校验，攻击者可把 header.alg 改成 "none"
    #   并去掉签名，服务端若"按 header 指定的算法验签"就会直接放行。
    if header.get("alg") != ALGORITHM:
        raise TokenError("不支持的签名算法")

    settings = get_settings()
    expected = hmac.new(
        settings.jwt_secret.encode("utf-8"),
        f"{header_seg}.{payload_seg}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    # compare_digest 而非 ==：避免通过比较耗时逐字节猜出正确签名
    if not hmac.compare_digest(expected, provided):
        raise TokenError("签名校验失败")

    exp = payload.get("exp")
    # 缺 exp 视为无效：否则一枚泄露的 token 就是永久凭证
    if not isinstance(exp, int):
        raise TokenError("token 缺少过期时间")
    current = int(time.time() if now_ts is None else now_ts)
    if exp <= current:
        raise TokenError("token 已过期")

    if payload.get("sub") is None:
        raise TokenError("token 缺少主体")
    return payload


# ---------------------------------------------------------------------------
# 身份主体
# ---------------------------------------------------------------------------
class Principal(BaseModel):
    """从 token 解出的调用方身份。

    **所有鉴权判断只认它**，绝不认请求体里的任何 ``user_id`` 字段 ——
    这正是本次升级要修掉的那个洞：身份曾经是请求体里的一个整数。
    """

    user_id: int
    username: str = ""
    role: str = ROLE_USER
    expires_at: int = 0

    @property
    def is_admin(self) -> bool:
        return self.role in (ROLE_ADMIN, ROLE_SYSADMIN)


def principal_from_token(token: str, *, now_ts: int | None = None) -> Principal:
    payload = decode_access_token(token, now_ts=now_ts)
    try:
        user_id = int(payload["sub"])
    except (TypeError, ValueError) as exc:
        raise TokenError("token 主体不是合法用户标识") from exc
    return Principal(
        user_id=user_id,
        username=str(payload.get("name", "")),
        role=str(payload.get("role", ROLE_USER)),
        expires_at=int(payload["exp"]),
    )


# ---------------------------------------------------------------------------
# 配置检查
# ---------------------------------------------------------------------------
DEFAULT_JWT_SECRET = "dev-insecure-secret-change-me"


def uses_default_secret() -> bool:
    """是否仍在用仓库里公开的默认密钥 —— 生产环境必须具备此告警。"""
    return get_settings().jwt_secret == DEFAULT_JWT_SECRET
