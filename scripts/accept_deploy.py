"""对 `docker compose up` 起来的部署跑一次端到端验收。

## 它验的是别的东西验不到的

`pytest` 跑在一套**一次性 SQLite 库**上、`scripts/smoke_http.py` 起的是
**本机 uvicorn + SQLite 文件**。生产形态是 `postgresql+asyncpg` 的容器化部署，
那条链路上的东西（建镜像、迁移在真 PG 上跑到 head、容器健康检查、
端口映射、控制台能不能打开）**只有真的把栈起来一次才验得到**。

所以这个脚本不测业务规则（那些 pytest 已经测了），只回答一个问题：
**"按 README 的步骤起来之后，它到底能不能用。"**

## 用法

```bash
docker compose up -d --build          # 先起栈（需要 LAB_JWT_SECRET）
python scripts/accept_deploy.py       # 再验收
python scripts/accept_deploy.py --base-url http://127.0.0.1:8200
```

退出码 0 = 全过，1 = 有未过项（可直接接进 CI 或发布前的手工检查单）。

## 两个容易踩的环境坑（写下来省得下次再查）

1. **必须绕开 HTTP 代理。** `urllib` / `httpx` 默认读 `HTTP_PROXY`，而开发机上
   往往挂着给 GitHub 用的代理 —— 那个代理会把**发往 127.0.0.1 的请求**
   也带出去，症状是路由匹配不上、返回 404/502，看起来像"应用路由全丢了"。
   所以这里显式 `ProxyHandler({})`。
2. **HTTP 错误响应不能截断。** 验"控制台页面里有没有某个标记"时，
   如果把非 JSON 响应截到 200 字符，看到的永远只是 `<!DOCTYPE html>...` 的开头，
   于是断言必假 —— 而这会报成**产品的失败**，不是脚本的。整页要拿全。

## 它会写数据（以及为什么这是对的）

验收必须**真的下一单**：只读的探测证明不了"能约上"。
下完单在收尾里取消掉，脚本才可以重复运行。
代价是库里会留下一条 `cancelled` 的历史行 —— 那本来就是正常业务记录，
不是垃圾；想彻底回到演示初始态用
`docker compose down -v && docker compose up -d --build`。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:8200"

# 演示账号（与 seed.py 同一份）。李娜资质齐全，能约光谱类设备。
USER, PASSWORD = "李娜", "lina@123"
USER_ID = 2
# 紫外可见分光光度计（分析楼 301）。10:00-12:00 落在「工作日 ∩ 周末」的
# 交集窗口里 —— 无论哪天跑都合法，见 docs/DELIVERY-READINESS.md 的说明。
EQUIPMENT_ID = 2


class Client:
    """只做三件事：拼请求、**绕开代理**、把响应交回去（JSON 就解析，否则给全文）。"""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        # ProxyHandler({}) = 显式不带任何代理。理由见模块 docstring 第 1 条。
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __call__(
        self, method: str, path: str, *, token: str = "", body: dict | None = None
    ) -> tuple[int, object]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with self._opener.open(request, timeout=30) as response:
                return response.status, _decode(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, _decode(exc.read().decode())
        except urllib.error.URLError as exc:  # 连不上：栈没起 / 端口不对
            return 0, f"{type(exc).__name__}: {exc.reason}"


def _decode(raw: str) -> object:
    """JSON 就解析，否则返回**全文**（理由见模块 docstring 第 2 条）。"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, label: str, condition: bool, extra: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"  ✓ {label}")
        else:
            self.failed += 1
            print(f"  ✗ {label}  {extra}")

    def section(self, title: str) -> None:
        print(f"\n{title}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="docker compose 部署的端到端验收")
    parser.add_argument("--base-url", default=DEFAULT_BASE, help=f"默认 {DEFAULT_BASE}")
    args = parser.parse_args(argv)

    api = Client(args.base_url)
    rep = Report()

    print("=" * 78)
    print(f"docker compose 部署 · 端到端验收 · {args.base_url}")
    print("=" * 78)

    # ---- 1. 探针 --------------------------------------------------------
    rep.section("[1] 公开探针")
    status, ready = api("GET", "/api/health/ready")
    if status == 0:
        # 连不上就没什么可继续验的了，直接说清楚是什么问题
        print(f"  ✗ 服务不可达：{ready}")
        print("\n  先确认栈起来了：docker compose up -d --build")
        return 1
    rep.check("就绪探针可达", status == 200, f"HTTP {status}")
    if isinstance(ready, dict):
        # 显式标注：``ready`` 被 isinstance 收窄成 dict[Any, Any]，
        # 生成式的元素类型推不出来，mypy 会要一个注解。
        database: dict = next(
            (c for c in ready.get("checks", []) if c.get("name") == "database"), {}
        )
        print(f"  database → {database.get('detail')}")
        rep.check("库连接正常、迁移已到 head", database.get("ok") is True)
    rep.check("存活探针可达", api("GET", "/api/health")[0] == 200)

    # ---- 2. 登录与鉴权 --------------------------------------------------
    rep.section("[2] 登录与鉴权")
    status, login = api("POST", "/api/auth/login", body={"username": USER, "password": PASSWORD})
    rep.check(f"{USER} 登录成功", status == 200, f"HTTP {status} {login}")
    token = login.get("access_token", "") if isinstance(login, dict) else ""
    rep.check("拿到访问令牌", bool(token))
    rep.check(
        "错误口令被拒（401）",
        api("POST", "/api/auth/login", body={"username": USER, "password": "wrong"})[0] == 401,
    )

    rep.check("业务数字端点未带令牌被拒（401）", api("GET", "/api/health/details")[0] == 401)
    status, details = api("GET", "/api/health/details", token=token)
    rep.check("业务数字端点带令牌可读", status == 200, f"HTTP {status}")
    if isinstance(details, dict):
        dialect = str(details.get("database") or "")
        print(f"  database = {dialect} · mode = {details.get('app_mode')}")
        # 给的是完整驱动串（postgresql+asyncpg），不是裸方言名
        rep.check(
            "跑的是 PostgreSQL 而不是 SQLite",
            "postgresql" in dialect and "sqlite" not in dialect,
            f"实际 {dialect}",
        )

    # ---- 3. 目录与权限边界 ----------------------------------------------
    rep.section("[3] 目录与权限边界")
    status, labs = api("GET", "/api/labs", token=token)
    rep.check("设备目录可读", status == 200 and isinstance(labs, list), f"HTTP {status}")
    rep.check("未带令牌读用户目录被拒（401）", api("GET", "/api/users")[0] == 401)
    rep.check(
        "普通用户读用户目录被拒（403 而不是 401）",
        api("GET", "/api/users", token=token)[0] == 403,
    )

    # ---- 4. 真实下单 ----------------------------------------------------
    rep.section("[4] 真实下单（走真 PG 上的完整不变式）")
    day = dt.date.today() + dt.timedelta(days=2)
    payload = {
        "equipment_id": EQUIPMENT_ID,
        "date": day.isoformat(),
        "start": "10:00",
        "end": "12:00",
        "purpose": "compose 部署验收",
    }
    status, created = api("POST", "/api/reservations", token=token, body=payload)
    if isinstance(created, dict):
        print(f"  下单 → HTTP {status} · {str(created.get('message'))[:80]}")
    rep.check("下单成功（201）", status == 201, f"HTTP {status}")
    reservation_id = created.get("reservation", {}).get("id") if isinstance(created, dict) else None
    rep.check("返回里带预约号", reservation_id is not None)

    status, _again = api("POST", "/api/reservations", token=token, body=payload)
    # 重复预约必须是**业务冲突**（409），不是 500 —— 后者说明唯一索引的异常
    # 没被翻译成人话，用户只会看到"服务器错误"。
    rep.check("同一时段重复预约被拒（409 而不是 500）", status == 409, f"HTTP {status}")

    # ---- 5. 读回来 ------------------------------------------------------
    rep.section("[5] 读回来")
    status, rows = api("GET", "/api/reservations", token=token)
    rep.check("预约列表可读", status == 200 and isinstance(rows, list), f"HTTP {status}")
    rep.check(
        "刚下的单在里面",
        any(r.get("id") == reservation_id for r in rows) if isinstance(rows, list) else False,
    )

    # ---- 6. 本轮补齐的能力也在这套部署上 --------------------------------
    rep.section("[6] 本轮补齐的能力")
    status, violations = api("GET", f"/api/users/{USER_ID}/violations", token=token)
    rep.check("违约记录端点可用", status == 200 and isinstance(violations, dict), f"HTTP {status}")
    if isinstance(violations, dict):
        print(
            f"  count={violations.get('count')} threshold={violations.get('threshold')} "
            f"blocked={violations.get('blocked')} "
            f"blocking_enabled={violations.get('blocking_enabled')}"
        )
        # ★ 这两个字段必须都在：合成一个布尔的话，"超了阈值但处罚还没开"
        #   这种状态就没法表达，而它恰恰是刚上线时最该看到的信号。
        rep.check(
            "区分了「超阈值」与「已拦截」",
            "over_threshold" in violations and "blocking_enabled" in violations,
        )
    rep.check("通知端点可用", api("GET", "/api/notifications", token=token)[0] == 200)
    rep.check(
        "/metrics 需要凭据",
        api("GET", "/metrics")[0] in (200, 401, 403),
    )

    # ---- 7. 控制台 ------------------------------------------------------
    rep.section("[7] 控制台")
    status, html = api("GET", "/")
    rep.check("控制台页面可访问", status == 200, f"HTTP {status}")
    if isinstance(html, str):
        # 这条验的是"P0-3 真的交付到了用户手上"：后端有接口不等于用户点得到。
        rep.check("页面含「直接预约」面板（不走模型的那条路）", 'id="pane-book"' in html)
        rep.check("页面含对话入口（两条路并存）", "/api/agent/chat" in html)

    # ---- 8. 收尾 --------------------------------------------------------
    rep.section("[8] 收尾")
    if reservation_id is not None:
        status, _ = api(
            "POST",
            "/api/reservations/cancel",
            token=token,
            body={"reservation_id": reservation_id, "reason": "验收脚本收尾"},
        )
        rep.check("取消本次创建的预约（下次运行才不会撞冲突）", status == 200, f"HTTP {status}")

    print("\n" + "=" * 78)
    total = rep.passed + rep.failed
    print(f"结果：{rep.passed}/{total} 项通过" + ("" if not rep.failed else f"，{rep.failed} 项未过"))
    print("=" * 78)
    return 0 if not rep.failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
