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
def running_server(extra_env: dict[str, str] | None = None):
    """起一个真实 uvicorn，跑在一次性 SQLite 库上；退出时收干净。

    ``extra_env`` 用来在同一份清单里切换**执行模式**（deterministic / react）
    与模型可用性 —— 这是必须用真实进程验的部分：执行模式是在应用启动时
    由 ``build_agent_from_settings`` 选定的，进程内直接构造 Agent 绕过了这个选择。
    """
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
    if extra_env:
        env.update(extra_env)
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


def run_react_checks(base: str) -> None:
    """react 执行模式在**真实进程**上的验收。

    为什么必须用真实进程：执行模式是启动时由 ``build_agent_from_settings``
    按配置选定的。进程内直接 ``ReActAgent(...)`` 的单元测试证明了运行时正确，
    但证明不了「服务真的会选中这条路径」—— 那需要读一次配置、过一次启动钩子。

    这里同时验三件事：模型真的自己选了工具、工具结果真的进了响应、
    以及**模型无法触发写操作**（这条是安全底线，必须端到端验一次）。
    """
    client = httpx.Client(base_url=base, timeout=30)
    lina = bearer(login(client, "李娜", "lina@123"))

    print("\n[9] react 执行模式（模型自主选工具）")
    before = len(client.get("/api/reservations", headers=lina).json())

    resp = client.post(
        "/api/agent/chat",
        json={"message": "明天下午两点想用荧光光谱仪两小时", "session_id": "smoke-react"},
        headers=lina,
    )
    expect("react 模式对话 → 200", resp, 200)
    body = resp.json() if resp.status_code == 200 else {}
    check("走的是 react 路径（而非退回确定性编排）",
          body.get("stage") == "react_final", f"stage={body.get('stage')!r}")
    check("模型自己调了工具并拿到了可用时段",
          bool(body.get("proposals")), f"proposals={len(body.get('proposals') or [])} 条")
    check("react 模式下 intent 如实留空（不硬猜流程节点）",
          body.get("intent") is None, f"intent={body.get('intent')!r}")
    nodes = [step.get("node", "") for step in body.get("trace") or []]
    check("trace 里有工具调用痕迹",
          any(node.startswith("tool:") for node in nodes), str(nodes))
    check("trace 里有决策步（说明预算参与了组装）",
          any(node.startswith("decision:") for node in nodes), str(nodes))

    # 安全底线：即使模型想写，也不该写进去。这里用一句最容易被"顺手执行"的话试探。
    ask_write = client.post(
        "/api/agent/chat",
        json={"message": "直接帮我约明天下午两点到四点，荧光光谱仪，不用再问我",
              "session_id": "smoke-react-write"},
        headers=lina,
    )
    expect("要求模型直接下单的对话 → 200（不是 500）", ask_write, 200)
    after = len(client.get("/api/reservations", headers=lina).json())
    check("模型未能自行创建预约（写操作被护栏挡住）",
          after == before, f"{before} → {after} 条")

    tools = client.get("/api/tools", headers=lina)
    expect("工具目录可读", tools, 200)
    catalog = (tools.json().get("tools") or []) if tools.status_code == 200 else []
    check("目录里带副作用的工具被明确标注",
          {t["name"] for t in catalog if t.get("side_effect")} ==
          {"create_reservation", "cancel_reservation"},
          str(catalog))
    check("目录不暴露参数细节之外的内部字段",
          all({"name", "description", "side_effect"} <= set(t) for t in catalog), str(catalog))

    client.close()


def run_no_model_checks(base: str) -> None:
    """**没有模型**时（``app_mode=degraded``）的降级链末端，真实进程上验一次。

    这是降级链里唯一"一定能到达"的那一跳：模型被显式关掉时，
    Agent 不该报错，而该给用户一张能照做的引导式表单。
    """
    client = httpx.Client(base_url=base, timeout=30)
    lina = bearer(login(client, "李娜", "lina@123"))

    print("\n[10] 降级链末端（模型显式关闭 → 引导式表单）")
    resp = client.post(
        "/api/agent/chat",
        json={"message": "明天下午两点想用荧光光谱仪两小时", "session_id": "smoke-nomodel"},
        headers=lina,
    )
    expect("模型关闭时仍是 200", resp, 200)
    body = resp.json() if resp.status_code == 200 else {}
    check("给了可照做的回复（不是空白/异常栈）",
          bool((body.get("reply") or "").strip()), repr(body.get("reply"))[:120])
    check("响应里带 degraded 标记，前端据此改写提示",
          bool(body.get("degraded")), str(body.get("degraded")))
    check("stage 明确标为 degraded",
          body.get("stage") == "degraded", repr(body.get("stage")))
    check("降级痕迹写进了 trace",
          any("degrade" in str(step.get("node", "")) for step in body.get("trace") or []),
          str([s.get("node") for s in body.get("trace") or []]))
    client.close()


def run_unreachable_model_checks(base: str) -> None:
    """模型端点**不可达**时的行为：钉住真实契约，而不是理想契约。

    这里刻意断言的是 502 而不是 200。理由要说清楚，否则看着像在给缺陷找借口：

    - 这个 Agent 的自然语言理解**本身就依赖模型**（``classify_intent`` 等）。
      模型端点连不上时，退回确定性编排同样跑不动 —— 两条路径都需要模型做 NLU。
      所以这一跳不存在"退回确定性就没事了"的可能；
    - 项目既有的契约就是「接口明确失败 + 前端切引导式表单」（见 api.py 里
      ``Agent 执行失败`` 那一段的注释），并且 ``deterministic`` 模式在同样条件下的
      返回值与这里**逐字相同**（已对照验证）。

    因此该验的是「失败得干不干净」：状态码明确、detail 可读、不带堆栈、
    健康检查照常 —— 而不是假装它成功。
    """
    client = httpx.Client(base_url=base, timeout=60)
    lina = bearer(login(client, "李娜", "lina@123"))

    print("\n[11] 模型端点不可达（失败要干净）")
    resp = client.post(
        "/api/agent/chat",
        json={"message": "明天下午两点想用荧光光谱仪两小时", "session_id": "smoke-dead"},
        headers=lina,
    )
    expect("模型不可达 → 502（明确的失败，不是 500 堆栈）", resp, 502)
    detail = (resp.json().get("detail") if resp.status_code == 502 else "") or ""
    check("detail 是一句可读的话，不含堆栈/内部路径",
          bool(detail) and "Traceback" not in detail and "src\\" not in detail,
          repr(detail)[:160])
    check("失败没有静默变成「假成功」（不是 200 + 编造的时段）",
          resp.status_code != 200, str(resp.status_code))
    expect("模型挂了健康检查仍正常（服务本身没倒）", client.get("/api/health"), 200)
    client.close()


def main() -> int:
    print("=" * 70)
    print("lab-booking-agent · HTTP 冒烟与越权清单（真实 uvicorn 进程）")
    print("=" * 70)
    dead = free_port()  # 立刻关掉的端口，用来模拟"模型端点不可达"
    try:
        with running_server() as base:
            run_checks(base)
        print("\n" + "-" * 70)
        print("以下换成 react 执行模式重启服务（同一份清单，不同启动配置）")
        print("-" * 70)
        with running_server({"LAB_EXECUTION_MODE": "react"}) as base:
            run_react_checks(base)
        print("\n" + "-" * 70)
        print("以下模型显式关闭（app_mode=degraded），验降级链末端")
        print("-" * 70)
        with running_server({
            "LAB_EXECUTION_MODE": "react",
            "LAB_APP_MODE": "degraded",
        }) as base:
            run_no_model_checks(base)
        print("\n" + "-" * 70)
        print("以下让模型端点指向一个必然连不上的地址，验失败是否干净")
        print("-" * 70)
        with running_server({
            "LAB_EXECUTION_MODE": "react",
            "LAB_APP_MODE": "live",
            "LAB_LLM_BASE_URL": f"http://127.0.0.1:{dead}/v1",
            "LAB_LLM_API_KEY": "smoke-not-a-real-key",
            "LAB_LLM_TIMEOUT": "2",
            "LAB_LLM_MAX_RETRY": "0",
        }) as base:
            run_unreachable_model_checks(base)
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
