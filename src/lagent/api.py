"""FastAPI 服务层。

四处刻意的设计：

1. **按请求覆盖配置用 model_copy 造副本，绝不改全局单例。**
   上个项目在这里栽过：某个请求把检索后端写进全局 settings，之后所有请求
   都跟着变，测试之间还互相污染。config.Settings 已设 frozen=True，
   想改也改不动，只能造副本。

2. **Agent 的执行链路不共用 FastAPI 注入的 session。**
   Agent 一次跑动可能跨多轮、带重试，而 ``Depends(get_db)`` 的会话会在请求
   返回时就关掉，工具函数再去用就会撞上 session is closed。所以工具内部一律
   自己开 ``session_scope()``（见 db.py）。

3. **身份只来自 token，绝不读请求体。**
   这是 P0-2 修掉的核心缺陷：``user_id`` 曾经是请求体里的一个整数
   （还是个默认 1），谁都能冒充谁；``/api/reservations`` 不传参返回全库。
   现在所有写操作走 ``Depends(current_user)``，管理处走 ``Depends(require_admin)``。

4. **鉴权策略是「默认拒绝 + 显式放行」。**
   只有 ``/api/health``（存活探针）、``/api/auth/login``（拿 token）、
   ``/``（登录页）是公开的；其余全部需要令牌。
   公开清单短且集中，审起来一眼能看完 —— 这比"给敏感接口打补丁"可靠得多。

5. **应用由 ``create_app()`` 造，而不是模块级单例。**
   中间件（CORS 白名单、体积上限）的配置必须在构造时读进来，
   如果写成模块级单例，测试就没法用不同的配置各造一个应用 ——
   只能去测"生产那一份"，等于没法验证白名单真的生效。

6. **健康检查分三档，因为「活着」「能干活」「有多少家底」是三件事。**
   ``/api/health`` 只证明进程还能响应（**不碰数据库**），
   ``/api/health/ready`` 逐项报告依赖，``/api/health/details`` 才是业务统计。
   合并成一个接口有一个很具体的后果：数据库一抖，存活探针就跟着失败，
   编排系统于是不停地重启一个**完全健康**的进程 —— 重启永远修不好数据库，
   但会把故障放大成"服务一直在重启"。所以存活探针不许依赖任何下游。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hmac
import json
import time
from collections.abc import AsyncIterator, MutableMapping
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select, text
from sqlalchemy.orm import selectinload
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__, audit
from .agent.graph import build_agent_from_settings, set_catalog
from .agent.state import SessionStore
from .agent.tools import TOOL_SPECS
from .clock import now_local
from .config import Settings, get_settings
from .db import SchemaDriftError, dispose_engine, revision_status, session_scope
from .domain.access import (
    EntryDecision,
    issue_permit,
    verify_entry,
    verify_exit,
)
from .domain.booking import cancel_reservation, create_reservation, list_reservations
from .knowledge.retriever import build_retriever, fallback_reason
from .metrics import (
    http_in_progress_dec,
    http_in_progress_inc,
    init_sweep_gauges,
    record_http_request,
    record_rate_limited,
    set_build_info,
)
from .metrics import (
    render as render_metrics,
)
from .models import (
    ACTIVE_STATUSES,
    DENY_IDENTITY_MISMATCH,
    PERMIT_CHECKED_IN,
    EntryPermit,
    Equipment,
    Laboratory,
    Reservation,
    User,
)
from .obs import (
    REQUEST_ID_HEADER,
    bind_request_id,
    bind_user,
    clear_user_context,
    configure_from_settings,
    get_logger,
    new_request_id,
    sanitize_request_id,
)
from .ratelimit import SlidingWindowLimiter
from .schemas import (
    AccessIssueRequest,
    AccessIssueResponse,
    AccessVerifyRequest,
    AccessVerifyResponse,
    AuditLogOut,
    CancelRequest,
    ChatRequest,
    ChatResponse,
    InsideEntry,
    LoginRequest,
    ReservationCreate,
    TokenResponse,
    UserOut,
)
from .security import (
    InsecureSecretError,
    Principal,
    TokenError,
    create_access_token,
    hash_password,
    principal_from_token,
    secret_problem,
    verify_password,
)
from .seed import seed
from .sweep import build_runner

WEB_DIR = __import__("pathlib").Path(__file__).parent / "web"

_log = get_logger("lagent.api")

# 存活探针的路径。单独提出来是因为访问日志要对它降级（见 RequestContextMiddleware）——
# 探针可能每几秒一次，按 INFO 记会把业务日志冲掉。
HEALTH_PATH = "/api/health"
READY_PATH = "/api/health/ready"
DETAILS_PATH = "/api/health/details"
METRICS_PATH = "/metrics"

# 被监控系统按固定节奏打的那些路径。访问日志对它们一律降到 DEBUG：
# 它们的量由自己的节奏决定，与业务无关 —— 一个 15 秒一次的抓取
# 攒一天就是 5760 行日志，足够把真出问题时那几行淹没。
# （P1-3 只降了 /api/health；P1-4 加了 /metrics 与就绪探针，同理。）
PROBE_PATHS = frozenset({HEALTH_PATH, READY_PATH, METRICS_PATH})

# 请求没匹配到任何路由时用的路由标签。**必须是个常量**，不能退回真实 path ——
# 那是个客户端可控的字符串，每个不同的值都会长出一条新的时间序列。
UNMATCHED_ROUTE = "__unmatched__"

# 下单失败原因 → HTTP 状态码。只映射 HTTP 本来就表达得了的**粗粒度**：
# 没找到 / 参数不对 / 没权限 / 冲突。更细的方向（conflict 该多给替代时段、
# contention 该扩容）由指标与审计承接 —— 把它们塞进状态码里只会得到一个
# 谁都看不懂的新码。
#
# ``unknown`` 是**唯一的例外**，且必须显式写在这里：它表示的是
# 「领域层返回了失败却没给分类」，也就是**我们的代码漏了 reason**，
# 而不是用户做错了什么。映射成 409 会让前端提示"换个时间再试" ——
# 而这件事重试一万次也不会变。给它 500 才能让监控响起来。
# 其余一律是"业务上的失败"，客户端照着改就行。
_BOOKING_STATUS = {
    "not_found": 404,
    "invalid": 422,
    "forbidden": 403,
    "state": 409,
    "conflict": 409,
    "contention": 409,
    "unknown": 500,
}

# 进程启动时刻（monotonic）。用 monotonic 而不是墙钟：启动时长要能在
# NTP 校正前后保持一致，否则会出现"运行时间变短了"这种没法解释的现象。
_PROCESS_STARTED = time.monotonic()

# 口令校验是 CPU 密集的（scrypt ≈140ms）。为了让"用户不存在"与"口令错误"
# 两种路径耗时接近（否则响应时间本身就是一个账号枚举侧信道），
# 账号不存在时也走一次等价成本的假校验。哈希在首次需要时算一次并缓存。
_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("dummy-password-for-constant-time-login")
    return _DUMMY_HASH


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # 第一件事就配日志：后面所有 print 都该改成 logger（P1-3）。
    # 放在最前面不是因为"顺序好看"，而是因为**启动阶段的失败最需要结构化日志** ——
    # 服务起不来时，一个能按字段检索的 JSON 行比一句自由文本有用得多。
    configure_from_settings()

    # ★ 签名密钥 **fail-closed**（原来是只打一条 WARNING 然后照常跑）。
    # 一个能伪造任意身份（含管理员）的密钥，不该有"先跑起来再说"的余地：
    # 告警会被忽略，异常不会。留的退路是**显式**的 LAB_ALLOW_INSECURE_DEFAULTS，
    # 默认是关的 —— 所以生产环境不可能"忘了配"，只可能是有人主动打开。
    #
    # 位置刻意**在一切副作用之前**：建库、灌种子、起清扫循环都是有副作用的，
    # 「先干完活再说你配置不对」会把一次干净的配置错误变成"库也建了、
    # 种子也灌了、然后退出了"，排查时还得先分清楚哪些是它留下的。
    problem = secret_problem()
    if problem is not None:
        if not get_settings().allow_insecure_defaults:
            raise InsecureSecretError(
                f"{problem}。请设置 LAB_JWT_SECRET；"
                "仅限本地演示可显式设置 LAB_ALLOW_INSECURE_DEFAULTS=true"
                "（该模式下每次启动都会打一条 CRITICAL）。"
            )
        # 允许了也要喊出来：结构化日志 + CRITICAL，
        # 让「生产环境出现 insecure_secret_allowed」能成为一条告警规则。
        _log.critical(
            "已显式允许不安全的签名密钥：%s。仅限本地演示，"
            "切勿用于任何可被他人访问的环境。",
            problem,
            extra={"event": "insecure_secret_allowed"},
        )

    # seed() 内部第一步就是 ensure_schema()：建表 + 校验结构与代码一致。
    # 校验放在这里而不是等业务代码崩，是因为漂移的失败形态又难懂又不一致：
    # 库里缺 users.password_hash 时报的是「no such column」加五十行堆栈，
    # 而且**只有查 users 的路径才会失败** —— 寒暄类对话照样正常，
    # 服务看起来是好的，一到真下单才炸。这种「部分可用」比直接报错更难查。
    try:
        info = await seed()
    except SchemaDriftError as exc:
        # 这条刻意**同时**用 print 和 logger：
        # 进程马上要退出，终端上要有人能一眼看见；而日志里要留一条可被告警抓到的
        # ERROR。只留其中一条都会漏掉一类使用者（人是看终端的，告警是读日志的）。
        print(f"\n[启动失败] {exc}\n")
        _log.error("启动失败：数据库结构与代码不一致", extra={"reason": str(exc)})
        raise RuntimeError("数据库结构与代码不一致，已中止启动（修法见上方提示）") from None

    app.state.seed_info = info

    # 把设备目录灌给模型层，让它能识别用户点名的设备
    async with session_scope() as session:
        rows = (await session.execute(select(Equipment))).scalars().all()
        set_catalog([(row.name, row.category) for row in rows])

    app.state.store = SessionStore()
    app.state.agent = build_agent_from_settings(app.state.store)
    # 限流器挂在 app.state 上而不是模块级：每个应用实例一份，
    # 测试之间不会互相把配额用光（模块级单例曾让第二个用例莫名 429）。
    settings = get_settings()
    app.state.chat_limiter = SlidingWindowLimiter(settings.rate_limit_per_minute, 60.0)
    # 把「这批指标是哪个版本、什么模式」写进指标本身：排障时第一个要回答的
    # 问题是「指标变化的那个时刻，代码/配置变了没有」，而这个标签就是答案。
    set_build_info(
        version=__version__,
        app_mode=settings.app_mode,
        retrieval_backend=settings.retrieval_backend,
    )

    # 后台清扫。挂在 app.state 上而不是模块级单例：每个应用实例一份，
    # 测试之间不会互相把对方的循环留下（模块级单例曾让第二个用例莫名 429）。
    runner = build_runner()
    app.state.sweeper = runner
    if settings.sweep_enabled:
        # 开启之前先把「从未成功过」显式写成 0：这样「从没跑成」与「很久没跑成」
        # 可以用同一条告警表达式覆盖，不必再写一个 absent() 分支
        # （详见 metrics.init_sweep_gauges）。
        init_sweep_gauges([task.name for task in runner.tasks])
        runner.start()
        _log.info(
            "后台清扫已启动",
            extra={"interval_seconds": runner.interval_seconds, "event": "sweep_started"},
        )

    try:
        yield
    finally:
        # 必须真的停：否则热重载 / 测试里会累积出多个循环同时写库。
        await runner.stop()
        await dispose_engine()


# ==========================================================================
# 中间件：体积上限 + CORS 白名单
# ==========================================================================
async def _reject(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def _route_label(scope: Scope) -> str:
    """取这次请求命中的**路由模板**，没命中就用常量 ``__unmatched__``。

    为什么不能退回真实 path：真实 path 是客户端可控的
    （``/api/labs/1``、``/api/labs/2``……），拿它当指标标签，每来一个新 id
    就长出一条新的时间序列 —— 一个把可观测性做成故障源的经典死法。
    模板的取值集合 = 路由条数，天然有界。

    实测（``_probe_route.py`` 验过）：FastAPI 在路由匹配时把命中的 route 写回
    **同一个** ``scope``，所以最外层中间件在**响应阶段**读得到它；
    没进路由的请求（404、被体积校验在读 body 前拦下的 413/411）读不到，
    统一落到 ``__unmatched__``。
    """
    route = scope.get("route")
    template = getattr(route, "path", "")
    return template if isinstance(template, str) and template else UNMATCHED_ROUTE


class RequestContextMiddleware:
    """给每个请求绑一个 request_id、写一条访问日志、并把 id 回传给调用方（P1-3）。

    **为什么必须是中间件（最外层），而不是在端点里加参数**：

    1. ``BodySizeLimitMiddleware`` 会在**读 body 之前**直接回 413/411，
       请求根本没进到路由 —— 在端点里绑 id 的话，这些响应没有任何关联 id，
       而"被边界拦下"恰恰是最需要排查的一类请求。
    2. 关联 id 要覆盖的不只是一个函数，还有它调用的审计、门禁、模型层。
       中间件是唯一能保证"整条调用链都在同一个上下文里"的位置。

    **为什么把 id 放进响应头**：出问题时用户/前端能报出这个 id，
    于是可以从一条日志直接定位到那一次请求的全部记录。
    没有它就只剩"大概几点钟出的问题"。

    顺序上它由 ``create_app()`` **最后**添加 —— Starlette 的 ``add_middleware``
    是"后添加的更靠外"，所以这样它才能包住体积校验那层。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # lifespan / websocket 不走这里。给它们绑 id 没有意义，
            # 反而会让"没有请求上下文"这件事变得看不出来。
            await self.app(scope, receive, send)
            return

        inbound = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }.get(REQUEST_ID_HEADER.lower())
        # 客户端给的值**先校验再用**，不合格就自己生成一个（见 obs 模块的说明）
        request_id = sanitize_request_id(inbound) or new_request_id()

        method = scope.get("method", "GET").upper()
        # 只记路径，不记 query string：查询串可能带用户输入（甚至参数化的令牌），
        # 而"哪个接口、多慢、什么结果"这三件事不需要它。
        path = scope.get("path", "")
        started = time.perf_counter()
        outcome: dict[str, Any] = {"status": 500, "failed": False}

        # 形参类型必须是 MutableMapping[str, Any] 而不是 dict：ASGI 的类型约定
        # 就是 MutableMapping（我们要往 message 里塞 headers，所以要可变），
        # 写成 dict 会在 mypy 这里报 arg-type —— 是个签名不匹配，不是逻辑错，
        # 但"签名不匹配"正是这类包装层最容易埋错的地方。
        async def send_with_id(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                outcome["status"] = int(message.get("status", 500))
                headers = list(message.get("headers") or [])
                headers.append(
                    (
                        REQUEST_ID_HEADER.lower().encode("latin-1"),
                        request_id.encode("latin-1"),
                    )
                )
                message["headers"] = headers
            await send(message)

        # 并发量必须在这里增减，而且**递减要放在最外层 finally** ——
        # 异常路径上少减一次，这个 gauge 就会一路往上爬，读起来像"服务卡住了"。
        http_in_progress_inc(method)
        with bind_request_id(request_id):
            try:
                await self.app(scope, receive, send_with_id)
            except Exception:
                outcome["failed"] = True
                raise
            finally:
                elapsed = time.perf_counter() - started
                route = _route_label(scope)
                fields = {
                    "method": method,
                    "path": path,
                    "route": route,
                    "status": outcome["status"],
                    "duration_ms": round(elapsed * 1000, 1),
                    "client": (scope.get("client") or ("", 0))[0],
                }
                if outcome["failed"]:
                    # 异常已经由上层转成 500，这里负责留下"哪个请求炸了"
                    _log.error("请求处理异常", extra=fields)
                elif path in PROBE_PATHS:
                    # 探针/抓取按固定节奏打，与业务量无关：按 INFO 记会把业务日志
                    # 冲掉（15 秒一次的抓取攒一天就是 5760 行）。
                    # 降成 DEBUG 而不是丢掉：要查探针本身是否正常时仍然拿得到。
                    _log.debug("探针", extra=fields)
                else:
                    _log.info("请求", extra=fields)
                # 抓取 /metrics 的这次请求**自己不进指标**：Prometheus 每 15 秒来一次，
                # 而真实业务可能一分钟才几次 —— 记进去的话 QPS 曲线主要反映
                # "抓取频率"，那个指标就废了。
                # （日志那边同理降到了 DEBUG，两处是同一个理由。）
                if path != METRICS_PATH:
                    record_http_request(
                        method=method,
                        route=route,
                        status=int(outcome["status"]),
                        duration_seconds=elapsed,
                    )
                # ⚠️ 顺序不能反：身份必须在**写完访问日志之后**才清掉，
                # 否则这条日志就丢了自己的 user_id。清掉的理由见 obs.clear_user_context：
                # request_id 靠 token 还原，而身份只 set 不还原 ——
                # 上下文一旦被复用（测试的 ASGI 传输层就是），
                # 下一条匿名请求的日志就会带着上一条请求的身份。
                clear_user_context()
        http_in_progress_dec(method)


class BodySizeLimitMiddleware:
    """拒绝过大的请求体（413），以及带 body 却不声明长度的请求（411）。

    ``Content-Length`` 检查刻意放在**读 body 之前**：若先读完再校验，
    内存已经花出去了，拦下来也没意义。

    对分块传输（无 Content-Length）直接回 411 Length Required，是个取舍：
    要真正拦住分块 body 得包装 ASGI 的 ``receive``，但那样抛出的异常会被
    FastAPI 的 ExceptionMiddleware 吞成 500。对纯 JSON 接口来说，
    要求声明长度是合理且可解释的约束。
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        if method in ("POST", "PUT", "PATCH"):
            headers = {
                key.decode("latin-1").lower(): value
                for key, value in scope.get("headers", [])
            }
            raw_length = headers.get("content-length")
            if raw_length is None:
                await _reject(send, 411, "请求必须声明 Content-Length")
                return
            try:
                declared = int(raw_length)
            except ValueError:
                await _reject(send, 400, "Content-Length 不是合法整数")
                return
            if declared > self.max_bytes:
                await _reject(send, 413, f"请求体超过上限 {self.max_bytes} 字节")
                return

        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    """造一个应用实例。中间件的配置在构造时读取，所以能按需各造一份。"""
    settings = settings or get_settings()
    application = FastAPI(
        title="lab-booking-agent",
        description="带真实约束协商能力的智能实验室预约 Agent（JWT + RBAC + 审计）",
        version=__version__,
        lifespan=lifespan,
    )
    application.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    if settings.cors_origin_list:
        # 只在配了白名单时才挂 CORS 中间件。默认空 = 不发任何 CORS 头 =
        # 浏览器只允许同源 —— 比 allow_origins=["*"] 安全得多的默认值。
        application.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )
    # 路由挂在 APIRouter 上，由工厂 include 进来。
    # 若直接 @app.get，路由会绑死在模块级那一个实例上 ——
    # create_app() 造出来的第二个应用会「一个接口都没有」（曾如此，测试里全是 404）。
    application.include_router(router)
    # ⚠️ 必须**最后**添加：Starlette 的 add_middleware 是"后添加的更靠外"。
    # 放最后它才能包住 BodySizeLimitMiddleware ——
    # 那层会在读 body 之前直接回 413/411，请求根本不进路由，
    # 而"被边界拦下"恰恰是最需要关联 id 的一类请求。
    application.add_middleware(RequestContextMiddleware)
    return application


# 所有业务路由都注册在这个 router 上（见 create_app 的注释）。
router = APIRouter()


# ==========================================================================
# 认证与授权
# ==========================================================================
# auto_error=False：缺 Authorization 头时不让 FastAPI 自己抛 403，
# 交给我们统一回 401 + WWW-Authenticate（语义上 401 才是"没认证"，
# 403 是"认证了但没权限"，两者混用会让前端无法区分"该登录"还是"该找管理员"）。
_bearer = HTTPBearer(auto_error=False)


async def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    """从 Bearer 令牌解出调用方身份。所有需要登录的端点都依赖它。"""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=401, detail="缺少访问令牌", headers={"WWW-Authenticate": "Bearer"}
        )
    try:
        principal = principal_from_token(credentials.credentials)
    except TokenError as exc:
        # 统一文案：不告诉调用方到底是签名错还是过期（少给攻击者一点情报）
        raise HTTPException(
            status_code=401,
            detail="访问令牌无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    # 解出身份就把「这次请求是谁」写进上下文（P1-3）。放在这里而不是各个端点里：
    # 它是唯一一个所有需要登录的端点都会经过的地方，加一次就到处生效 ——
    # 包括这条请求后续在审计、门禁、模型层里打的日志，全部自动带上 user_id。
    bind_user(principal.user_id, principal.username)
    return principal


async def require_admin(user: Principal = Depends(current_user)) -> Principal:
    """"必须是管理员"的依赖。普通用户命中即 403 —— 认证通过但权限不足。"""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


async def chat_quota(
    request: Request, user: Principal = Depends(current_user)
) -> Principal:
    """按用户给对话接口限流。

    限流按**用户**而不是按 IP：同一个实验室出口 IP 后面可能站着几十个人，
    按 IP 会让一个人把别人的额度吃光。已认证的前提下按用户是更准的维度。
    """
    limiter: SlidingWindowLimiter | None = getattr(request.app.state, "chat_limiter", None)
    if limiter is not None and limiter.enabled:
        allowed, retry_after = limiter.hit(f"user:{user.user_id}")
        if not allowed:
            # 限流拒绝数（P1-4）：这个数字是"该扩容还是该让人少刷"的唯一依据。
            # 只看 429 的日志回答不了"是一两个人刷还是所有人都被限"。
            record_rate_limited("chat")
            raise HTTPException(
                status_code=429,
                detail=f"请求过于频繁，请 {retry_after} 秒后再试",
                headers={"Retry-After": str(retry_after)},
            )
    return user


def _client_host(request: Request | None) -> str:
    """后端看到的来源地址。不解析 X-Forwarded-For —— 那是网关的职责，
    在这里"顺便信任"一个客户端可伪造的头，只会污染审计数据。"""
    return request.client.host if request is not None and request.client else ""


@router.post("/api/auth/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request) -> TokenResponse:
    """用用户名 + 口令换访问令牌。

    三条安全细节：

    * **账号不存在与口令错误返回完全相同的 401。**
      分开回「用户不存在」等于免费提供一个账号枚举接口。
    * **口令校验放到线程池里跑。** scrypt 是故意慢的 CPU 密集操作，
      直接在事件循环里跑会把整个进程卡住（单 worker 下就是全站卡住）。
    * **失败的登录也进审计。** 「某个账号被连续试了 200 次」这个模式
      只有把失败记下来才看得见。
    """
    async with session_scope() as session:
        row = (
            await session.execute(select(User).where(User.username == body.username))
        ).scalar_one_or_none()

    stored = row.password_hash if row is not None else _dummy_hash()
    ok = await asyncio.to_thread(verify_password, body.password, stored)
    if row is None or not ok:
        await audit.record(
            action=audit.ACTION_LOGIN_FAILED,
            outcome=audit.OUTCOME_DENIED,
            actor_id=row.id if row is not None else None,
            actor_name=body.username,
            target_type="user",
            detail="用户名或密码不正确",
            client_host=_client_host(request),
        )
        raise HTTPException(status_code=401, detail="用户名或密码不正确")

    settings = get_settings()
    token = create_access_token(user_id=row.id, username=row.username, role=row.role)
    await audit.record(
        action=audit.ACTION_LOGIN,
        actor_id=row.id,
        actor_name=row.username,
        target_type="user",
        target_id=row.id,
        detail=f"role={row.role}",
        client_host=_client_host(request),
    )
    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_ttl_minutes * 60,
        user=UserOut.model_validate(row),
    )


@router.get("/api/auth/me", response_model=UserOut)
async def me(user: Principal = Depends(current_user)) -> UserOut:
    """回显当前令牌对应的身份，供控制台启动时校验 token 是否还有效。"""
    async with session_scope() as session:
        row = await session.get(User, user.user_id)
    if row is None:
        raise HTTPException(status_code=401, detail="令牌对应的用户已不存在")
    return UserOut.model_validate(row)


# ==========================================================================
# 健康与元信息
# ==========================================================================
# 三档分开，因为「活着」「能干活」「有多少家底」是三件不同的事，
# 而且它们的**调用方**也不同：编排系统看前两个（且不带凭据），人看第三个。
#
# 合并成一个接口的后果很具体：数据库一抖，存活探针跟着失败，
# 编排系统于是一直重启一个完全健康的进程 —— 重启修不好数据库，
# 却把一次降级放大成"服务一直在重启"。
@dataclass(frozen=True)
class HealthCheck:
    """一个检查项。

    ``critical`` 与 ``ok`` 分开是这套设计的核心：**"没通过"不等于"不能用"**。
    刻意降级的部署（app_mode=degraded）、缺可选依赖因而回退到 BM25 的检索、
    停掉的清扫循环，都不该被算成"服务不可用" —— 把它们混进 critical，
    结果就是没人再相信这个探针。
    """

    name: str
    ok: bool
    critical: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "critical": self.critical,
            "detail": self.detail,
        }


async def _check_database() -> HealthCheck:
    """数据库可达 + **结构版本与代码一致**。

    版本这一条不能省：库停在一个旧 revision 上时，服务的失败形态是
    「部分可用」（寒暄正常、一下单就报 no such column），那正是 P1-1 花力气
    消灭的东西。宁可明确报"没就绪"，也不要对外宣称自己是好的。

    只做 ``SELECT 1`` 与读一次版本号，**不调 ``schema_drift()``** ——
    后者要遍历全部模型与表，那是启动路径该付的成本，不是每几秒一次的探针该付的。
    """
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        current, head = await revision_status()
    except Exception as exc:  # noqa: BLE001 - 探针的职责是"如实报告失败"，不是把异常冒成 500
        return HealthCheck("database", False, True, f"不可用：{type(exc).__name__}: {exc}")
    if current != head:
        return HealthCheck(
            "database",
            False,
            True,
            f"结构版本 {current or '未由迁移接管'} ≠ 代码 head {head}；"
            f"请先 python main.py migrate",
        )
    return HealthCheck("database", True, True, f"可用，revision={current}")


def _check_agent(request: Request) -> HealthCheck:
    settings = get_settings()
    agent = getattr(request.app.state, "agent", None)
    available = bool(agent and agent.client is not None)
    # degraded 模式**刻意**不建 Agent（退化为引导式表单，这是产品承诺的降级路径）。
    # 把它算成"没就绪"，等于让一个按设计运行的部署永远不被认为可用。
    critical = settings.app_mode != "degraded"
    if available:
        return HealthCheck("agent", True, critical, f"可用（{settings.app_mode}）")
    return HealthCheck(
        "agent",
        False,
        critical,
        "不可用：降级为引导式表单" if not critical else "不可用：app_mode 要求 Agent 在线",
    )


def _check_retrieval() -> HealthCheck:
    reason = fallback_reason()
    # 缺可选依赖（chromadb）会自动回退到内置 BM25 —— 那是**设计内的降级**，
    # 检索仍然可用。所以 ok=not reason，但永远不是 critical。
    detail = reason or f"backend={get_settings().retrieval_backend}"
    return HealthCheck("retrieval", not reason, False, detail)


def _check_sweeper(request: Request) -> HealthCheck:
    settings = get_settings()
    if not settings.sweep_enabled:
        # 配置关掉的不算异常。报告它，但明确说清"这是配置"而不是"它坏了"。
        return HealthCheck("sweeper", True, False, "已按配置关闭")
    runner = getattr(request.app.state, "sweeper", None)
    running = bool(runner and runner.running)
    # 清扫停了**不会**让门禁失守（verify_entry 自己判凭证过期，见 sweep.py），
    # 所以它不是 critical。但它必须被看见 —— 这就是这个检查项存在的全部理由，
    # 对应的告警见 metrics 的 lagent_sweep_last_success_timestamp_seconds。
    return HealthCheck(
        "sweeper", running, False, "运行中" if running else "未在运行（门禁安全性不受影响）"
    )


@router.get(HEALTH_PATH)
async def health() -> dict:
    """存活探针（liveness）：只证明「进程还能响应」。

    **刻意不碰数据库、不碰任何下游。** 原因见本段开头的注释。
    """
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "app_mode": settings.app_mode,
        "now": now_local().isoformat(timespec="seconds"),
        # 一个在崩溃重启的进程，uptime 会一直很小 —— 这是"它在反复重启"最直接的
        # 证据，而且不需要读任何外部系统。墙钟被 NTP 校正也不会让它跳变
        # （用的是 monotonic，见模块顶部的 _PROCESS_STARTED）。
        "uptime_seconds": round(time.monotonic() - _PROCESS_STARTED, 1),
    }


@router.get(READY_PATH)
async def ready(request: Request, response: Response) -> dict:
    """就绪探针（readiness）：逐项报告依赖，**任一致命项失败就回 503**。

    公开是刻意的：编排系统的探针默认不带凭据，要凭据的探针等于没探针。
    所以这里只回「哪些检查通过/失败」，**不回任何计数与业务量** ——
    那些在 ``/api/health/details`` 里，需要登录。
    """
    checks = [
        await _check_database(),
        _check_agent(request),
        _check_retrieval(),
        _check_sweeper(request),
    ]
    usable = all(check.ok or not check.critical for check in checks)
    if not usable:
        # 用 503 而不是 200 + 正文里写 not_ready：编排系统看的是状态码，
        # 一个恒为 200 的就绪探针在编排系统眼里永远就绪 —— 等于没做。
        response.status_code = 503
    return {
        "status": "ready" if usable else "not_ready",
        "checks": [check.as_dict() for check in checks],
        "now": now_local().isoformat(timespec="seconds"),
    }


@router.get(DETAILS_PATH)
async def health_details(
    request: Request, _: Principal = Depends(current_user)
) -> dict:
    """业务统计与控制台需要的运行信息。**需要登录。**

    P1-4 把它从公开的 ``/api/health`` 上搬过来并挂了认证：原来任何人不需要凭据
    就能读到"库里有多少用户、多少条预约"。单看不致命，但那是免费的容量情报，
    而这类"顺手公开"的信息一旦被别人依赖上就很难再收回去。
    """
    settings = get_settings()
    async with session_scope() as session:
        counts = {
            "labs": (await session.execute(select(func.count()).select_from(Laboratory))).scalar(),
            "equipment": (await session.execute(select(func.count()).select_from(Equipment))).scalar(),
            "users": (await session.execute(select(func.count()).select_from(User))).scalar(),
            "reservations": (await session.execute(select(func.count()).select_from(Reservation))).scalar(),
            "active": (
                await session.execute(
                    select(func.count()).select_from(Reservation).where(Reservation.status.in_(ACTIVE_STATUSES))
                )
            ).scalar(),
        }
    # 注意用 request.app 而不是模块级的 app：create_app() 可以造出多个实例
    # （测试就是这么用的），写死模块级单例会让测试改到"另一个应用"的状态上。
    agent = getattr(request.app.state, "agent", None)
    return {
        "status": "ok",
        "app": settings.app_name,
        "app_mode": settings.app_mode,
        "agent_available": bool(agent and agent.client is not None),
        "retrieval_backend": settings.retrieval_backend,
        "retrieval_degraded_reason": fallback_reason(),
        "database": settings.database_url.split("://")[0],
        "timezone": settings.timezone,
        "now": now_local().isoformat(timespec="seconds"),
        "counts": counts,
    }


# ==========================================================================
# 指标（P1-4）
# ==========================================================================
_METRICS_KEY_HEADER = "X-Metrics-Key"


async def metrics_caller(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """放行抓取端：预共享密钥，或管理员令牌（便于人工 curl 排障）。

    与门禁的 ``gate_caller`` 是同一套「默认拒绝」逻辑：**没配密钥 = 只认管理员
    令牌**，不会因为忘了配就敞开。刻意不把它抽象成一个参数化的鉴权工厂 ——
    两处各写一遍，读的人一眼能看完整，而且这两个接口的风险面并不相同
    （一个决定"开不开门"，一个只泄露容量数字）。

    ⚠️ **先判"功能关了没"，再判身份**：关掉的时候要回 404 而不是 401。
    顺序反了的话，一个未带凭据的探测会拿到 401 —— 那等于对外承认
    "这里有个需要凭据的接口"。功能关掉就该看起来像是**不存在**。
    """
    if not get_settings().metrics_enabled:
        raise HTTPException(status_code=404, detail="未启用指标端点")
    expected = get_settings().metrics_api_key
    presented = request.headers.get(_METRICS_KEY_HEADER, "")
    if expected and presented and hmac.compare_digest(presented, expected):
        return
    if credentials is not None and credentials.credentials:
        with contextlib.suppress(TokenError):
            if principal_from_token(credentials.credentials).is_admin:
                return
    raise HTTPException(
        status_code=401,
        detail="指标接口需要设备密钥或管理员令牌",
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.get(METRICS_PATH, include_in_schema=False)
async def metrics(_: None = Depends(metrics_caller)) -> Response:
    """Prometheus 文本格式（0.0.4）抓取端点。

    ``include_in_schema=False``：它不是给业务方调用的接口，
    列进 /docs 只会让"公开接口清单"变得不可读。

    认证与「关掉」的行为都由 ``metrics_caller`` 一处决定
    （关掉 → 404，未授权 → 401）—— 策略放在两个地方就会漂移。
    """
    return Response(
        content=render_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
        # 不让任何中间层缓存抓取结果：否则看到的是缓存时刻的数据，
        # 而"指标不更新"这件事极难被怀疑到缓存头上。
        headers={"Cache-Control": "no-store"},
    )


# ==========================================================================
# 工具与资源
# ==========================================================================
@router.get("/api/tools")
async def tools_spec(_: Principal = Depends(current_user)) -> dict:
    """已注册的工具规格（控制台用来展示 Agent 的能力面）。"""
    return {"tools": TOOL_SPECS}


@router.get("/api/labs")
async def labs(_: Principal = Depends(current_user)) -> list[dict]:
    """实验室与设备目录。需登录（目录本身不敏感，但按"默认拒绝"统一处理）。"""
    async with session_scope() as session:
        stmt = (
            select(Laboratory)
            .options(selectinload(Laboratory.equipment))
            .order_by(Laboratory.id)
        )
        rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": lab.id,
            "label": lab.label,
            "building": lab.building,
            "floor": lab.floor,
            "room": lab.room,
            "capacity": lab.capacity,
            "open_hours": lab.open_hours,
            "note": lab.note,
            "equipment": [
                {
                    "id": item.id,
                    "name": item.name,
                    "model": item.model,
                    "code": item.code,
                    "category": item.category,
                    "status": item.status,
                    "max_hours": item.max_hours,
                    "requires_training": item.requires_training,
                }
                for item in lab.equipment
            ],
        }
        for lab in rows
    ]


@router.get("/api/users", response_model=list[UserOut])
async def users(
    request: Request, admin: Principal = Depends(require_admin)
) -> list[UserOut]:
    """列出全部用户。**管理员专用**：普通用户没有任何业务理由拿到花名册。"""
    async with session_scope() as session:
        rows = (await session.execute(select(User).order_by(User.id))).scalars().all()
    # 读花名册也留痕：审计不只记"改了什么"，也要能回答"谁看过什么"
    await audit.record(
        action=audit.ACTION_ADMIN_READ,
        actor_id=admin.user_id,
        actor_name=admin.username,
        target_type="user_directory",
        detail=f"返回 {len(rows)} 条",
        client_host=_client_host(request),
    )
    return [UserOut.model_validate(u) for u in rows]


@router.get("/api/audit", response_model=list[AuditLogOut])
async def audit_logs(
    limit: int = Query(default=100, ge=1, le=500),
    action: str | None = Query(default=None, max_length=48),
    _: Principal = Depends(require_admin),
) -> list[AuditLogOut]:
    """审计流水（管理员专用）。只读：没有任何接口能改或删审计记录。"""
    rows = await audit.recent_logs(limit, action=action)
    return [AuditLogOut.model_validate(row) for row in rows]


@router.get("/api/reservations")
async def reservations(
    user_id: int | None = Query(default=None),
    user: Principal = Depends(current_user),
) -> list[dict]:
    """查询预约记录。

    **非管理员只能看到自己的**：查询串里传 ``user_id=别人`` 会被无视并改写为
    令牌里的身份，而不是报错 —— 这样前端不需要知道权限细节，
    而越权读取在服务端就断了（不依赖前端自觉）。
    管理员传 ``user_id`` 可看指定用户，不传则看全量。
    """
    scope = user_id if user.is_admin else user.user_id
    async with session_scope() as session:
        rows = await list_reservations(session, user_id=scope)
    return [row.model_dump(mode="json") for row in rows]


@router.post("/api/reservations", status_code=201)
async def create(body: ReservationCreate, request: Request, user: Principal = Depends(current_user)) -> dict:
    """**表单式**下单 —— 不经过模型（P0-3）。

    与对话入口共用 ``domain.booking.create_reservation``，
    所以资质、开放时间、粒度对齐、唯一索引兜底这些不变式**一个都不少**；
    差别只在于"谁把需求翻译成槽位"：这里由调用方直接给结构化字段。

    失败时按 ``reason`` 选状态码，而不是一律 409：
    P1-4 给下单结果加了七种机器可读的分类，如果 HTTP 层把它们又压成一个码，
    那层分类就白做了。映射只覆盖 HTTP 本来就表达得了的粗粒度（没找到 / 参数不对 /
    没权限 / 冲突），更细的方向由指标与审计承接。
    """
    target = user.user_id
    if body.as_user_id is not None:
        if not user.is_admin:
            await audit.record(
                action=audit.ACTION_BOOK,
                outcome=audit.OUTCOME_DENIED,
                actor_id=user.user_id,
                actor_name=user.username,
                target_type="equipment",
                target_id=body.equipment_id,
                detail="非管理员尝试用 as_user_id 代他人下单",
                client_host=_client_host(request),
            )
            raise HTTPException(status_code=403, detail="只有管理员可以代他人预约")
        target = body.as_user_id

    outcome = await create_reservation(
        user_id=target,
        equipment_id=body.equipment_id,
        date_=body.date,
        start=body.start,
        end=body.end,
        purpose=body.purpose,
    )
    await audit.record(
        action=audit.ACTION_BOOK,
        outcome=audit.OUTCOME_OK if outcome.ok else audit.OUTCOME_DENIED,
        actor_id=user.user_id,
        actor_name=user.username,
        target_type="equipment",
        target_id=body.equipment_id,
        detail=(
            f"as_user_id={target} " if target != user.user_id else ""
        ) + outcome.message,
        client_host=_client_host(request),
    )
    if not outcome.ok:
        # 默认分支给 500：走到这里的标签要么是新增的 ``BookingReason`` 忘了
        # 在上面登记，要么是领域层漏了分类 —— 两种都是**我们的**问题。
        # 假装成 409（冲突）会让用户去重试一个永远重试不成的操作。
        raise HTTPException(
            status_code=_BOOKING_STATUS.get(outcome.outcome_label, 500),
            detail=outcome.message,
        )
    return outcome.model_dump(mode="json")


@router.post("/api/reservations/cancel")
async def cancel(
    body: CancelRequest,
    request: Request,
    user: Principal = Depends(current_user),
) -> dict:
    """取消预约。取消者身份取自令牌，请求体里没有 ``user_id`` 可填。

    管理员可用 ``as_user_id`` 代他人取消；普通用户传该字段直接 403
    （显式拒绝而不是静默忽略 —— 静默忽略会让调用方以为操作生效了）。
    """
    target = user.user_id
    if body.as_user_id is not None:
        if not user.is_admin:
            # 「想动别人的东西」是最该留痕的一类请求，拒绝也要记
            await audit.record(
                action=audit.ACTION_CANCEL,
                outcome=audit.OUTCOME_DENIED,
                actor_id=user.user_id,
                actor_name=user.username,
                target_type="reservation",
                target_id=body.reservation_id,
                detail="非管理员尝试用 as_user_id 代他人取消",
                client_host=_client_host(request),
            )
            raise HTTPException(status_code=403, detail="只有管理员可以代他人取消预约")
        target = body.as_user_id

    outcome = await cancel_reservation(
        reservation_id=body.reservation_id, user_id=target, reason=body.reason
    )
    await audit.record(
        action=audit.ACTION_CANCEL,
        outcome=audit.OUTCOME_OK if outcome.ok else audit.OUTCOME_DENIED,
        actor_id=user.user_id,
        actor_name=user.username,
        target_type="reservation",
        target_id=body.reservation_id,
        detail=(
            f"as_user_id={target} " if target != user.user_id else ""
        ) + outcome.message,
        client_host=_client_host(request),
    )
    if not outcome.ok:
        raise HTTPException(status_code=409, detail=outcome.message)
    return outcome.model_dump(mode="json")


# ==========================================================================
# 对话
# ==========================================================================
@router.post("/api/agent/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    request: Request,
    user: Principal = Depends(chat_quota),
) -> ChatResponse:
    agent = getattr(request.app.state, "agent", None)
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent 尚未初始化")
    # ★ 身份以令牌为准：请求体里带的 user_id 一律丢弃。
    #   model_copy 造副本而不是原地改（ChatRequest 可能被调用方复用）。
    req = req.model_copy(update={"user_id": user.user_id})
    try:
        response = await agent.ainvoke(req)
    except Exception as exc:
        # 模型侧异常不该变成 500 堆栈；明确告诉前端失败了，并由前端切到表单
        raise HTTPException(status_code=502, detail=f"Agent 执行失败：{exc}") from exc

    # 只在真的产生下单动作时留痕，避免把每句闲聊都写进审计表
    booking = response.booking
    if booking is not None:
        await audit.record(
            action=audit.ACTION_BOOK,
            outcome=audit.OUTCOME_OK if booking.ok else audit.OUTCOME_FAILED,
            actor_id=user.user_id,
            actor_name=user.username,
            target_type="reservation",
            target_id=booking.reservation.id if booking.reservation else "",
            detail=booking.message,
            client_host=_client_host(request),
        )
    return response


# ==========================================================================
# 规范检索（控制台可直接试）
# ==========================================================================
@router.get("/api/retrieve")
async def retrieve(
    q: str = Query(min_length=1),
    k: int = 4,
    _: Principal = Depends(current_user),
) -> dict:
    settings = get_settings()
    retriever = build_retriever(settings.retrieval_backend)
    hits = retriever.search(q, k)
    matched = getattr(retriever, "last_matched", {}) or {}
    return {
        "backend": settings.retrieval_backend,
        "degraded_reason": fallback_reason(),
        "stats": retriever.stats(),
        "hits": [
            {
                "heading": chunk.heading,
                "source": chunk.source,
                "text": chunk.text,
                "score": round(float(score), 4),
                "matched_by": matched.get(chunk.fingerprint, []),
            }
            for chunk, score in hits
        ],
    }


# ==========================================================================
# 控制台
# ==========================================================================
@router.get("/")
async def index() -> Response:
    target = WEB_DIR / "index.html"
    if not target.exists():
        return JSONResponse({"detail": "web/index.html 不存在"}, status_code=404)
    return FileResponse(target, media_type="text/html")


@router.get("/api/today")
async def today(_: Principal = Depends(current_user)) -> dict:
    now = now_local()
    return {
        "date": now.date().isoformat(),
        "time": now.strftime("%H:%M"),
        "weekday": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()],
        "tomorrow": (now.date() + dt.timedelta(days=1)).isoformat(),
    }


# ==========================================================================
# 人员准入（门禁）
# ==========================================================================
# 门禁机不是"某个用户"，发不了用户令牌，所以走一把预共享的设备密钥。
# 两种身份都接受：管理员令牌（便于人工核验/排障）或设备密钥。
# 都没有 → 401。这个接口能决定"开不开门"，绝不能匿名可用。
_GATE_KEY_HEADER = "X-Gate-Key"


async def gate_caller(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal | None:
    """识别调用方是门禁设备还是管理员。返回 None 表示"设备身份"。"""
    expected = get_settings().gate_api_key
    presented = request.headers.get(_GATE_KEY_HEADER, "")
    if expected and presented and hmac.compare_digest(presented, expected):
        return None
    if credentials is not None and credentials.credentials:
        with contextlib.suppress(TokenError):
            principal = principal_from_token(credentials.credentials)
            if principal.is_admin:
                return principal
    raise HTTPException(
        status_code=401,
        detail="门禁接口需要设备密钥或管理员令牌",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _decision_to_response(
    decision: EntryDecision, lab_label: str = "", user_name: str = ""
) -> AccessVerifyResponse:
    return AccessVerifyResponse(
        granted=decision.ok,
        reason_code=decision.reason_code,
        message=decision.message,
        permit_id=decision.permit_id,
        user_id=decision.user_id,
        user_name=user_name,
        lab_id=decision.lab_id,
        lab_label=lab_label,
    )


@router.post("/api/access/verify")
async def access_verify(
    req: AccessVerifyRequest,
    _caller: Principal | None = Depends(gate_caller),
) -> AccessVerifyResponse:
    """核验一次进门/出门。门禁机在几百毫秒内要拿到是/否。

    ``precheck=true`` 用于门禁屏预显示：只判定、不核销、不占座 ——
    否则"屏幕上看一眼能不能进"就把凭证消耗掉了。
    """
    async with session_scope() as session:
        lab = await session.get(Laboratory, req.lab_id)
        label = _lab_label(lab) if lab is not None else ""
        if req.direction == "out":
            if req.user_id is None:
                # 用 identity_mismatch 而不是 no_such_user：这是"没识别出你是谁"，
                # 不是"系统里没这个人"。原因码若混用，事后统计就会把
                # "有人忘带卡"算成"有人拿假卡"，处置动作完全不同。
                return AccessVerifyResponse(
                    granted=False,
                    reason_code=DENY_IDENTITY_MISMATCH,
                    message="出场需要识别到身份（请刷卡或进行人脸核验）。",
                    lab_id=req.lab_id,
                    lab_label=label,
                )
            decision = await verify_exit(
                session, user_id=req.user_id, gate_id=req.gate_id
            )
            return _decision_to_response(decision, label)

        decision = await verify_entry(
            session,
            lab_id=req.lab_id,
            credential=req.credential,
            identity_user_id=req.user_id,
            gate_id=req.gate_id,
            check_in=not req.precheck,
        )
        user_name = ""
        if decision.user_id is not None:
            user = await session.get(User, decision.user_id)
            user_name = user.username if user is not None else ""
        return _decision_to_response(decision, label, user_name)


@router.get("/api/access/inside")
async def access_inside(
    lab_id: int | None = Query(default=None),
    _: Principal = Depends(require_admin),
) -> dict:
    """当前在馆名单。管理员专用 —— 名单本身就是敏感信息。

    它回答的是安全场景里最要紧的一个问题：**"现在楼里都有谁"**。
    火灾、事故、清场时没有这张名单，就只能靠喊。
    """
    async with session_scope() as session:
        stmt = (
            select(EntryPermit, User, Laboratory)
            .join(User, User.id == EntryPermit.user_id)
            .join(Laboratory, Laboratory.id == EntryPermit.lab_id)
            .where(EntryPermit.status == PERMIT_CHECKED_IN)
            .order_by(EntryPermit.checked_in_at)
        )
        if lab_id is not None:
            stmt = stmt.where(EntryPermit.lab_id == lab_id)
        rows = (await session.execute(stmt)).all()

    items = [
        InsideEntry(
            permit_id=permit.id,
            user_id=user.id,
            username=user.username,
            lab_id=lab.id,
            lab_label=_lab_label(lab),
            valid_from=permit.valid_from.strftime("%H:%M"),
            valid_to=permit.valid_to.strftime("%H:%M"),
            checked_in_at=(
                permit.checked_in_at.strftime("%Y-%m-%d %H:%M")
                if permit.checked_in_at
                else None
            ),
        )
        for permit, user, lab in rows
    ]
    return {"count": len(items), "inside": items}


@router.post("/api/access/issue")
async def access_issue(
    req: AccessIssueRequest,
    admin: Principal = Depends(require_admin),
) -> AccessIssueResponse:
    """管理员手工签发凭证（访客、临时人员、预约系统之外的补救）。

    有两个刻意的约束：

    1. **只允许管理员**。能绕过预约流程的人越少越好；
    2. **必须写理由**，并写进审计。这不是形式主义 —— "谁在什么时候给谁开了后门"
       是这类系统最该被追问的一件事。
    """
    async with session_scope() as session:
        permit, plain = await issue_permit(
            session,
            user_id=req.user_id,
            lab_id=req.lab_id,
            date_=req.date,
            valid_from=req.valid_from,
            valid_to=req.valid_to,
            source="admin_grant",
        )
        # 必须在会话内把值取出来：commit 后属性会过期，
        # 出了 with 再读会撞上 DetachedInstanceError（而且只在某些驱动上出现）。
        permit_id = permit.id
        required = list(permit.required_certs or [])

    await audit.record(
        action="access.issue",
        actor_id=admin.user_id,
        actor_name=admin.username,
        target_type="lab",
        target_id=req.lab_id,
        detail=f"手工签发入室凭证给用户 {req.user_id}；理由：{req.reason}",
    )
    return AccessIssueResponse(
        permit_id=permit_id,
        user_id=req.user_id,
        lab_id=req.lab_id,
        date=req.date,
        valid_from=req.valid_from,
        valid_to=req.valid_to,
        credential=plain,
        required_certs=required,
    )


def _lab_label(lab: Laboratory) -> str:
    return f"{lab.building}{lab.floor}楼{lab.room}"


# uvicorn 的入口（`lagent.api:app`）。必须在所有 @router 注册之后再建，
# 否则 include_router 拿到的是空路由表。
app = create_app()
