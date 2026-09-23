"""FastAPI 服务层。

两处刻意的设计：

1. **按请求覆盖配置用 model_copy 造副本，绝不改全局单例。**
   上个项目在这里栽过：某个请求把检索后端写进全局 settings，之后所有请求
   都跟着变，测试之间还互相污染。config.Settings 已设 frozen=True，
   想改也改不动，只能造副本。

2. **Agent 的执行链路不共用 FastAPI 注入的 session。**
   Agent 一次跑动可能跨多轮、带重试，而 ``Depends(get_db)`` 的会话会在请求
   返回时就关掉，工具函数再去用就会撞上 session is closed。所以工具内部一律
   自己开 ``session_scope()``（见 db.py）。
"""

from __future__ import annotations

import contextlib
import datetime as dt
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
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
from .schemas import CancelRequest, ChatRequest, ChatResponse
from .seed import seed

WEB_DIR = __import__("pathlib").Path(__file__).parent / "web"


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
    try:
        yield
    finally:
        await dispose_engine()


app = FastAPI(
    title="lab-booking-agent",
    description="带真实约束协商能力的智能实验室预约 Agent",
    version="1.0.0",
    lifespan=lifespan,
)


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


@app.get("/api/tools")
async def tools_spec() -> dict:
    return {"tools": TOOL_SPECS}


# ==========================================================================
# 资源
# ==========================================================================
@app.get("/api/labs")
async def labs() -> list[dict]:
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


@app.get("/api/users")
async def users() -> list[dict]:
    async with session_scope() as session:
        rows = (await session.execute(select(User).order_by(User.id))).scalars().all()
    return [
        {"id": u.id, "username": u.username, "role": u.role, "certs": u.certs}
        for u in rows
    ]


@app.get("/api/reservations")
async def reservations(user_id: int | None = Query(default=None)) -> list[dict]:
    async with session_scope() as session:
        rows = await list_reservations(session, user_id=user_id)
    return [row.model_dump(mode="json") for row in rows]


class CancelBody(BaseModel):
    reservation_id: int
    user_id: int
    reason: str = ""


@app.post("/api/reservations/cancel")
async def cancel(body: CancelBody) -> dict:
    outcome = await cancel_reservation(
        reservation_id=body.reservation_id, user_id=body.user_id, reason=body.reason
    )
    if not outcome.ok:
        raise HTTPException(status_code=409, detail=outcome.message)
    return outcome.model_dump(mode="json")


# ==========================================================================
# 对话
# ==========================================================================
@app.post("/api/agent/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    agent = getattr(app.state, "agent", None)
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent 尚未初始化")
    try:
        return await agent.ainvoke(req)
    except Exception as exc:  # noqa: BLE001
        # 模型侧异常不该变成 500 堆栈；明确告诉前端失败了，并由前端切到表单
        raise HTTPException(status_code=502, detail=f"Agent 执行失败：{exc}") from exc


# ==========================================================================
# 规范检索（控制台可直接试）
# ==========================================================================
@app.get("/api/retrieve")
async def retrieve(q: str = Query(min_length=1), k: int = 4) -> dict:
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
async def today() -> dict:
    now = now_local()
    return {
        "date": now.date().isoformat(),
        "time": now.strftime("%H:%M"),
        "weekday": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()],
        "tomorrow": (now.date() + dt.timedelta(days=1)).isoformat(),
    }
