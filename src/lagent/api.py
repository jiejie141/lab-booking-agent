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
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .agent.graph import build_agent_from_settings, set_catalog
from .agent.state import SessionStore
from .agent.tools import TOOL_SPECS
from .clock import now_local
from .config import get_settings
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
from .schemas import (
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


app = FastAPI(
    title="lab-booking-agent",
    description="带真实约束协商能力的智能实验室预约 Agent（JWT + RBAC）",
    version="1.1.0",
    lifespan=lifespan,
)


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


@app.post("/api/auth/login", response_model=TokenResponse)
async def login(body: LoginRequest) -> TokenResponse:
    """用用户名 + 口令换访问令牌。

    两条安全细节：

    * **账号不存在与口令错误返回完全相同的 401。**
      分开回「用户不存在」等于免费提供一个账号枚举接口。
    * **口令校验放到线程池里跑。** scrypt 是故意慢的 CPU 密集操作，
      直接在事件循环里跑会把整个进程卡住（单 worker 下就是全站卡住）。
    """
    async with session_scope() as session:
        row = (
            await session.execute(select(User).where(User.username == body.username))
        ).scalar_one_or_none()

    stored = row.password_hash if row is not None else _dummy_hash()
    ok = await asyncio.to_thread(verify_password, body.password, stored)
    if row is None or not ok:
        raise HTTPException(status_code=401, detail="用户名或密码不正确")

    settings = get_settings()
    token = create_access_token(user_id=row.id, username=row.username, role=row.role)
    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_ttl_minutes * 60,
        user=UserOut.model_validate(row),
    )


@app.get("/api/auth/me", response_model=UserOut)
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
@app.get("/api/health")
async def health() -> dict:
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
    agent = getattr(app.state, "agent", None)
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
@app.get("/api/tools")
async def tools_spec(_: Principal = Depends(current_user)) -> dict:
    """已注册的工具规格（控制台用来展示 Agent 的能力面）。"""
    return {"tools": TOOL_SPECS}


@app.get("/api/labs")
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


@app.get("/api/users", response_model=list[UserOut])
async def users(_: Principal = Depends(require_admin)) -> list[UserOut]:
    """列出全部用户。**管理员专用**：普通用户没有任何业务理由拿到花名册。"""
    async with session_scope() as session:
        rows = (await session.execute(select(User).order_by(User.id))).scalars().all()
    return [UserOut.model_validate(u) for u in rows]


@app.get("/api/reservations")
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


@app.post("/api/reservations/cancel")
async def cancel(body: CancelRequest, user: Principal = Depends(current_user)) -> dict:
    """取消预约。取消者身份取自令牌，请求体里没有 ``user_id`` 可填。

    管理员可用 ``as_user_id`` 代他人取消；普通用户传该字段直接 403
    （显式拒绝而不是静默忽略 —— 静默忽略会让调用方以为操作生效了）。
    """
    target = user.user_id
    if body.as_user_id is not None:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="只有管理员可以代他人取消预约")
        target = body.as_user_id

    outcome = await cancel_reservation(
        reservation_id=body.reservation_id, user_id=target, reason=body.reason
    )
    if not outcome.ok:
        raise HTTPException(status_code=409, detail=outcome.message)
    return outcome.model_dump(mode="json")


# ==========================================================================
# 对话
# ==========================================================================
@app.post("/api/agent/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, user: Principal = Depends(current_user)) -> ChatResponse:
    agent = getattr(app.state, "agent", None)
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent 尚未初始化")
    # ★ 身份以令牌为准：请求体里带的 user_id 一律丢弃。
    #   model_copy 造副本而不是原地改（ChatRequest 可能被调用方复用）。
    req = req.model_copy(update={"user_id": user.user_id})
    try:
        return await agent.ainvoke(req)
    except Exception as exc:  # noqa: BLE001
        # 模型侧异常不该变成 500 堆栈；明确告诉前端失败了，并由前端切到表单
        raise HTTPException(status_code=502, detail=f"Agent 执行失败：{exc}") from exc


# ==========================================================================
# 规范检索（控制台可直接试）
# ==========================================================================
@app.get("/api/retrieve")
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
@app.get("/")
async def index() -> FileResponse:
    target = WEB_DIR / "index.html"
    if not target.exists():
        return JSONResponse({"detail": "web/index.html 不存在"}, status_code=404)
    return FileResponse(target, media_type="text/html")


@app.get("/api/today")
async def today(_: Principal = Depends(current_user)) -> dict:
    now = now_local()
    return {
        "date": now.date().isoformat(),
        "time": now.strftime("%H:%M"),
        "weekday": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()],
        "tomorrow": (now.date() + dt.timedelta(days=1)).isoformat(),
    }
