"""工具层：领域能力的可调用封装。

**关于「谁来决定调用哪个工具」——这里与常见写法不同，值得说明。**

多数 LangGraph 教程把工具选择交给模型（bind_tools + ToolNode），模型自己决定
查空闲还是下单。但对预约系统来说，业务规则是明确的：取消意图绝不该被路由到
下单工具，而「有没有资质」更不允许由模型裁决。把工具选择交给模型只增加不确定性，
换来的灵活性这里用不上。

所以本项目把工具选择写成**显式的图路由**（见 graph.py 的条件边），
模型的职责被收窄到它真正擅长、且容错成本低的地方：把自然语言翻成结构化诉求，
以及把结构化事实措辞成自然语言。每个工具内部仍然自行校验参数与权限
（见 domain/booking.py 的写前校验），不信任上游给的任何参数。

也就是说：**模型负责理解语言，代码负责执行规则。**
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from ..config import get_settings
from ..db import session_scope
from ..domain.availability import load_active_reservations
from ..domain.booking import cancel_reservation, create_reservation, list_reservations
from ..domain.negotiate import negotiate
from ..knowledge.retriever import build_retriever, to_hits
from ..models import User
from ..schemas import (
    BookingOutcome,
    DocHit,
    NegotiationResult,
    Requirement,
)

# 工具契约声明。它不参与当前的路由（路由是写死的条件边），
# 但对外暴露了「这个系统有哪些能力」，也方便将来真的接模型选工具。
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "query_availability",
        "description": "查询满足全部约束的可用时段；无精确解时返回放宽后的备选方案",
        "params": {
            "date": "YYYY-MM-DD",
            "start": "HH:MM（可空）",
            "end": "HH:MM（可空）",
            "duration_hours": "数字（可空）",
            "equipment_name": "设备名（可空）",
            "category": "设备类别（可空）",
        },
    },
    {
        "name": "create_reservation",
        "description": "创建预约。内部做写前校验 + 设备级串行化 + 唯一索引兜底",
        "params": {
            "user_id": "整数",
            "equipment_id": "整数",
            "date": "YYYY-MM-DD",
            "start": "HH:MM",
            "end": "HH:MM",
        },
    },
    {
        "name": "cancel_reservation",
        "description": "取消预约。带版本条件的更新，避免覆盖他人的并发修改",
        "params": {"reservation_id": "整数", "user_id": "整数", "reason": "字符串"},
    },
    {
        "name": "check_admission",
        "description": "检索实验室准入与安全规范条文，给出可引用出处",
        "params": {"query": "自然语言"},
    },
    {
        "name": "list_reservations",
        "description": "列出用户自己的预约记录",
        "params": {"user_id": "整数"},
    },
]


async def tool_query_availability(
    user_id: int, requirement: Requirement, *, now: dt.datetime | None = None
) -> NegotiationResult:
    async with session_scope() as session:
        user = await session.get(User, user_id)
        if user is None:
            return NegotiationResult(satisfied=False, blockers=[f"用户 {user_id} 不存在"])
        return await negotiate(session, user, requirement, now=now)


async def tool_create_reservation(
    *,
    user_id: int,
    equipment_id: int,
    date_: dt.date,
    start: dt.time,
    end: dt.time,
    purpose: str = "",
) -> BookingOutcome:
    return await create_reservation(
        user_id=user_id,
        equipment_id=equipment_id,
        date_=date_,
        start=start,
        end=end,
        purpose=purpose,
    )


async def tool_cancel_reservation(
    *, reservation_id: int, user_id: int, reason: str = ""
) -> BookingOutcome:
    return await cancel_reservation(
        reservation_id=reservation_id, user_id=user_id, reason=reason
    )


async def tool_check_admission(query: str) -> list[DocHit]:
    settings = get_settings()
    retriever = build_retriever(settings.retrieval_backend)
    results = retriever.search(query, settings.retrieval_top_k)
    # matched_by 让它如实反映「这条是被 BM25 命中、还是两路都命中」；
    # 纯 BM25 后端下没有融合过程，该字段自然为空。
    matched = getattr(retriever, "last_matched", {}) or {}
    return to_hits(results, matched)


async def tool_list_reservations(user_id: int) -> list:
    async with session_scope() as session:
        return await list_reservations(session, user_id=user_id)


async def load_catalog() -> list[tuple[str, str]]:
    """设备目录 (名称, 类别)，供 Mock/真实模型识别用户点名的设备。"""
    from sqlalchemy import select

    from ..models import Equipment

    async with session_scope() as session:
        rows = (await session.execute(select(Equipment).order_by(Equipment.id))).scalars().all()
        catalog = [(row.name, row.category) for row in rows]
    return catalog or []


async def upcoming_for_equipment(
    equipment_id: int, date_: dt.date
) -> list:
    async with session_scope() as session:
        grouped = await load_active_reservations(session, [date_], [equipment_id])
    return grouped.get((equipment_id, date_), [])
