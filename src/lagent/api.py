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
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from starlette.types import ASGIApp, Receive, Scope, Send

from . import audit
from .agent.graph import build_agent_from_settings, set_catalog
from .agent.state import SessionStore
from .agent.tools import TOOL_SPECS
from .clock import now_local
from .config import Settings, get_settings
from .db import dispose_engine, init_db, session_scope
from .domain.booking import cancel_reservation, list_reservations
from .knowledge.retriever import build_retriever, fallback_reason
from .models import (
    ACTIVE_STATUSES,
    Equipment,
    Laboratory,
    Reservation,
    User,
)
from .ratelimit import SlidingWindowLimiter
from .schemas import (
    AuditLogOut,
    CancelRequest,
    ChatRequest,
    ChatResponse,
    LoginRequest,
    TokenResponse,
    UserOut,
)
from .security import (
    Principal,
    TokenError,
    create_access_token,
    hash_password,
    principal_from_token,
    uses_default_secret,
    verify_password,
)
from .seed import seed

WEB_DIR = __import__("pathlib").Path(__file__).parent / "web"

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
    await init_db()
    info = await seed()
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
    if uses_default_secret():
        # 只用 print 不用日志框架：这一条要在任何日志配置生效之前就能被看见
        print(
            "[security] ⚠ 正在使用仓库内置的默认 JWT 密钥，任何人都能伪造令牌。"
            "生产部署前请设置 LAB_JWT_SECRET。"
        )
    try:
        yield
    finally:
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
        version="1.2.0",
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
        return principal_from_token(credentials.credentials)
    except TokenError as exc:
        # 统一文案：不告诉调用方到底是签名错还是过期（少给攻击者一点情报）
        raise HTTPException(
            status_code=401,
            detail="访问令牌无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


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
@router.get("/api/health")
async def health(request: Request) -> dict:
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
    except Exception as exc:  # noqa: BLE001
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
async def index() -> FileResponse:
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


# uvicorn 的入口（`lagent.api:app`）。必须在所有 @router 注册之后再建，
# 否则 include_router 拿到的是空路由表。
app = create_app()
