"""HTTP 冒烟 / 越权清单：对着**真实的 uvicorn 进程**跑一遍。

为什么不只用 pytest 里的 ``httpx.ASGITransport``：
进程内 ASGI 测试绕过了真实网络栈、中间件链与应用启动钩子。
「服务能不能起来、起来之后鉴权对不对、限流真的会 429 吗」
只有真的连一次 TCP 才算数。

这份清单就是 ``docs/ENTERPRISE-UPGRADE.md`` §P0-3 要求的
「最小渗透清单」（越权读 / 越权改 / 无 token 写 / 超大 body / 高频刷接口）。

用法::

    python scripts/smoke_http.py

脚本自己起服务、自己收尾，跑在一次性沙箱库上，不会污染开发库。
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

SMOKE_SECRET = "smoke-test-secret"
# 限流探针要能算清"第几次开始 429"，所以给个小配额
RATE_LIMIT = 6

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}" + (f"  · {detail}" if detail and not ok else ""))


def expect(name: str, resp: httpx.Response, status: int, extra=None) -> httpx.Response:
    ok = resp.status_code == status
    detail = f"期望 {status}，实得 {resp.status_code}"
    if ok and extra is not None:
        try:
            ok = bool(extra(resp))
            if not ok:
                detail = "状态码正确但内容断言失败"
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"断言抛错：{exc}"
    check(name, ok, detail)
    return resp


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def b64u(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@contextlib.contextmanager
def running_server():
    """起一个真实 uvicorn，跑在一次性 SQLite 库上；退出时收干净。"""
    root = pathlib.Path(tempfile.mkdtemp(prefix="lagent-smoke-"))
    port = free_port()
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(SRC),
        "LAB_DATABASE_URL": f"sqlite+aiosqlite:///{(root / 'smoke.db').as_posix()}",
        "LAB_JWT_SECRET": SMOKE_SECRET,
        "LAB_APP_MODE": "mock",
        # 演示口令的 KDF 成本调低：这份脚本会登录很多次，没必要每次等 140ms
        "LAB_PASSWORD_KDF_N": "1024",
        "LAB_RATE_LIMIT_PER_MINUTE": str(RATE_LIMIT),
        # 空白名单 = 不发 CORS 头，正好用来验证默认行为
        "LAB_CORS_ORIGINS": "",
        "PYTHONIOENCODING": "utf-8",
    })
    # 让脚本自己能签出与服务端一致密钥的令牌（用于构造过期/篡改样本）
    os.environ["LAB_JWT_SECRET"] = SMOKE_SECRET

    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "lagent.api:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 40
        ready = False
        while time.time() < deadline:
            if process.poll() is not None:
                out = (process.stdout.read() if process.stdout is not None else b"").decode("utf-8", "replace")
                raise RuntimeError(f"服务提前退出（code={process.returncode}）：\n{out}")
            try:
                if httpx.get(f"{base}/api/health", timeout=2).status_code == 200:
                    ready = True
                    break
            except httpx.HTTPError:
                time.sleep(0.3)
        if not ready:
            raise RuntimeError("服务在 40 秒内没有就绪")
        print(f"  服务已就绪：{base}\n")
        yield base
    finally:
        process.terminate()
        with contextlib.suppress(Exception):
            process.wait(timeout=10)
        if process.poll() is None:
            process.kill()
        shutil.rmtree(root, ignore_errors=True)


def login(client: httpx.Client, username: str, password: str) -> str:
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    resp.raise_for_status()
    return resp.json()["access_token"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# 清单
# --------------------------------------------------------------------------
def run_checks(base: str) -> None:
    from lagent.security import create_access_token

    client = httpx.Client(base_url=base, timeout=30)

    print("[1] 公开端点与认证边界")
    expect("健康检查公开可访问", client.get("/api/health"), 200)
    expect("控制台页面可访问", client.get("/"), 200,
           lambda r: "text/html" in r.headers["content-type"])
    expect("未登录读预约 → 401", client.get("/api/reservations"), 401)
    expect("未登录查身份 → 401", client.get("/api/auth/me"), 401)
    expect("未登录对话 → 401",
           client.post("/api/agent/chat", json={"message": "你好"}), 401)
    expect("未登录打管理端点 → 401", client.get("/api/users"), 401)

    print("\n[2] 登录")
    wrong = client.post("/api/auth/login", json={"username": "李娜", "password": "wrong"})
    expect("口令错误 → 401", wrong, 401)
    ghost = client.post("/api/auth/login", json={"username": "查无此人", "password": "wrong"})
    expect("账号不存在 → 401", ghost, 401)
    check("账号存在与否不可区分（防枚举）",
          wrong.json().get("detail") == ghost.json().get("detail"),
          f"{wrong.json().get('detail')} vs {ghost.json().get('detail')}")
    lina_token = login(client, "李娜", "lina@123")
    check("登录返回三段式令牌", len(lina_token.split(".")) == 3)
    lina = bearer(lina_token)
    admin = bearer(login(client, "管理员", "admin@123"))
    check("张伟 账号可登录", len(login(client, "张伟", "zhangwei@123").split(".")) == 3)
    expect("/api/auth/me 回显本人", client.get("/api/auth/me", headers=lina), 200,
           lambda r: r.json()["username"] == "李娜")

    print("\n[3] 越权读取")
    own = client.get("/api/reservations", headers=lina).json()
    check("李娜只看到自己的 1 条", len(own) == 1, f"实得 {len(own)} 条")
    escaped = client.get("/api/reservations", params={"user_id": 1}, headers=lina).json()
    check("传 ?user_id=1 也拿不到别人的", len(escaped) == 1, f"实得 {len(escaped)} 条")
    check("管理员能看到全量", len(client.get("/api/reservations", headers=admin).json()) == 2)
    expect("普通用户读用户目录 → 403", client.get("/api/users", headers=lina), 403)
    expect("普通用户读审计 → 403", client.get("/api/audit", headers=lina), 403)

    print("\n[4] 越权写入")
    zhang_rows = client.get("/api/reservations", params={"user_id": 1}, headers=admin).json()
    victim_id = zhang_rows[0]["id"]
    expect("取消他人预约 → 409",
           client.post("/api/reservations/cancel",
                       json={"reservation_id": victim_id}, headers=lina), 409)
    expect("非管理员用 as_user_id → 403",
           client.post("/api/reservations/cancel",
                       json={"reservation_id": victim_id, "as_user_id": 1}, headers=lina), 403)
    expect("老接口把 user_id 写进 body → 422",
           client.post("/api/reservations/cancel",
                       json={"reservation_id": victim_id, "user_id": 1}, headers=lina), 422)

    print("\n[5] 令牌伪造")
    head, payload, signature = lina_token.split(".")
    tampered = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    tampered["role"] = "admin"
    bad_signature = f"{head}.{b64u(tampered)}.{signature}"
    expect("改 role 不改签名 → 401",
           client.get("/api/users", headers=bearer(bad_signature)), 401)
    forged_header = b64u({"alg": "none", "typ": "JWT"})
    expect("alg 改成 none → 401",
           client.get("/api/users", headers=bearer(f"{forged_header}.{payload}.")), 401)
    expect("乱写的令牌 → 401", client.get("/api/auth/me", headers=bearer("a.b.c")), 401)
    expired = create_access_token(
        user_id=2, username="李娜", role="user", ttl_seconds=5, now_ts=1000
    )
    expect("过期令牌 → 401", client.get("/api/auth/me", headers=bearer(expired)), 401)

    print("\n[6] 身份不可伪造")
    booked = client.post(
        "/api/agent/chat",
        json={"message": "明天上午十点到十一点，紫外可见分光光度计",
              "user_id": 1, "session_id": "smoke-attr"},
        headers=lina,
    )
    ok_booking = booked.status_code == 200 and booked.json().get("booking", {})
    booked_id = (ok_booking or {}).get("reservation", {}).get("id") if ok_booking else None
    check("body 里塞 user_id=1 仍按李娜下单", booked_id is not None, booked.text[:200])
    if booked_id:
        zhang_ids = {r["id"] for r in
                     client.get("/api/reservations", params={"user_id": 1}, headers=admin).json()}
        check("该预约没被算到张伟名下", booked_id not in zhang_ids)

    print("\n[7] 滥用与边界")
    expect("超大请求体 → 413",
           client.post("/api/agent/chat", content=b"x" * 200_000,
                       headers={**lina, "Content-Type": "application/json"}), 413)

    def chunked():
        yield b'{"message": "hello"}'

    with contextlib.suppress(Exception):
        resp = client.post("/api/agent/chat", content=chunked(),
                           headers={**lina, "Content-Type": "application/json"})
        expect("不声明长度的 POST → 411", resp, 411)

    expect("未知字段 → 422",
           client.post("/api/auth/login",
                       json={"username": "李娜", "password": "lina@123", "role": "admin"}), 422)

    codes = [
        client.post("/api/agent/chat",
                    json={"message": "你好", "session_id": f"smoke-rl-{i}"},
                    headers=admin).status_code
        for i in range(RATE_LIMIT + 2)
    ]
    check(f"限流：前 {RATE_LIMIT} 次放行", codes[:RATE_LIMIT] == [200] * RATE_LIMIT, str(codes))
    check("限流：超出后 429", codes[RATE_LIMIT] == 429, str(codes))
    check("429 带 Retry-After", "retry-after" in
          client.post("/api/agent/chat", json={"message": "你好"}, headers=admin).headers)

    resp = client.get("/api/health", headers={"Origin": "http://evil.example.com"})
    check("默认不发 CORS 头", "access-control-allow-origin" not in resp.headers)

    print("\n[8] 审计留痕")
    logs = client.get("/api/audit", params={"limit": 200}, headers=admin)
    expect("管理员可读审计", logs, 200)
    rows = logs.json() if logs.status_code == 200 else []
    actions = {r["action"] for r in rows}
    check("记录了登录成功", "auth.login" in actions, str(sorted(actions)))
    check("记录了登录失败", "auth.login_failed" in actions, str(sorted(actions)))
    check("记录了越权取消被拒",
          any(r["action"] == "reservation.cancel" and r["outcome"] == "denied" for r in rows))
    check("审计不含口令明文",
          "lina@123" not in json.dumps(rows, ensure_ascii=False)
          and "admin@123" not in json.dumps(rows, ensure_ascii=False))
    users = client.get("/api/users", headers=admin).json()
    check("用户目录不回 email / 口令哈希",
          all("email" not in u and "password_hash" not in u for u in users))

    client.close()


def main() -> int:
    print("=" * 70)
    print("lab-booking-agent · HTTP 冒烟与越权清单（真实 uvicorn 进程）")
    print("=" * 70)
    try:
        with running_server() as base:
            run_checks(base)
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ 冒烟过程中断：{type(exc).__name__}: {exc}")
        return 2

    passed = sum(1 for status, _, _ in results if status == PASS)
    total = len(results)
    print("\n" + "=" * 70)
    failed = [(name, detail) for status, name, detail in results if status == FAIL]
    if failed:
        print(f"结果：{passed}/{total} 项通过，以下未通过：")
        for name, detail in failed:
            print(f"  ✗ {name}  · {detail}")
    else:
        print(f"结果：{passed}/{total} 项全部通过")
    print("=" * 70)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
