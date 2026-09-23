"""工具层：领域能力的可调用封装 + 向 harness 注册的契约。

**关于「谁来决定调用哪个工具」——这里有两种模式，值得说清。**

多数 LangGraph 教程把工具选择交给模型（bind_tools + ToolNode），模型自己决定
查空闲还是下单。但对预约系统来说，业务规则是明确的：取消意图绝不该被路由到
下单工具，而「有没有资质」更不允许由模型裁决。把工具选择交给模型只增加不确定性，
换来的灵活性在**这条业务**上用不上。

所以本项目的默认模式（``execution_mode=deterministic``）把工具选择写成
**显式的图路由**（见 graph.py 的条件边），模型的职责被收窄到它真正擅长、
且容错成本低的地方：把自然语言翻成结构化诉求，以及把结构化事实措辞成自然语言。

但「在这条业务上不需要」不等于「不该有」。通用 Agent 场景（模糊需求下自己
摸索该查什么）必须让模型选工具，那正是 JD 里 Tool/Function Calling 的含义。
所以另一种模式（``execution_mode=react``）把选择权交回模型，两条路径
**共用本模块注册的同一份工具**（见 :func:`build_registry`）。

也就是说：**模型负责理解语言，代码负责执行规则 —— 而"谁来选工具"是一个
可配置的执行策略，不是世界观。**

安全边界不依赖模式：每个工具内部仍然自行校验参数与权限
（见 domain/booking.py 的写前校验），不信任上游给的任何参数；
有副作用的工具还额外带 ``side_effect`` 标记，默认不被暴露给模型。
另外 **``user_id`` 不出现在任何工具的参数 schema 里** ——
它由 :func:`build_registry` 从认证结果注入，沿用 P0-2「身份不来自请求体」的结论。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..config import get_settings
from ..db import session_scope
from ..domain.availability import load_active_reservations
from ..domain.booking import cancel_reservation, create_reservation, list_reservations
from ..domain.negotiate import negotiate
from ..harness import DEFAULT_RESULT_LIMIT, ToolRegistry, ToolSpec
from ..knowledge.retriever import build_retriever, to_hits
from ..models import User
from ..schemas import (
    BookingOutcome,
    DocHit,
    NegotiationResult,
    Requirement,
)

# ==========================================================================
# 工具参数契约（pydantic 模型）
# ==========================================================================
# 用参数模型而不是手写 schema 字典：模型既用于**生成**给模型的 JSON Schema，
# 也用于**校验**模型回传的参数。一处定义两处生效，就不会出现
# 「改了字段忘了改 schema」那种漂移。
#
# 全部 ``extra="forbid"``：模型多给一个字段应当被明确拒绝并回喂，
# 而不是静默忽略 —— 静默忽略会让它以为参数生效了，下一轮继续错。


class QueryAvailabilityParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: dt.date = Field(description="要查询的日期，格式 YYYY-MM-DD")
    start: dt.time | None = Field(default=None, description="期望开始时间，如 14:00")
    end: dt.time | None = Field(default=None, description="期望结束时间，如 16:00")
    duration_hours: float | None = Field(default=None, description="期望时长（小时）")
    equipment_name: str | None = Field(default=None, description="设备名，如 荧光光谱仪")
    category: str | None = Field(default=None, description="设备类别，如 光谱 / 离心")


class CheckAdmissionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(description="要检索的规范问题，用自然语言，例如「离心机需要什么资质」")


class ListReservationsParams(BaseModel):
    """无参数工具。空模型而不是 None，让注册表不必为它写特例。"""

    model_config = ConfigDict(extra="forbid")


class CreateReservationParams(BaseModel):
    """**有副作用**：会真的占用时段。默认不暴露给模型，见 harness/tools.py。

    注意这里没有 ``user_id`` —— 下单人来自认证，不来自模型。
    """

    model_config = ConfigDict(extra="forbid")

    equipment_id: int = Field(description="设备 ID，来自可用性查询结果")
    date: dt.date = Field(description="预约日期 YYYY-MM-DD")
    start: dt.time = Field(description="开始时间，如 14:00")
    end: dt.time = Field(description="结束时间，如 16:00")
    purpose: str = Field(default="", description="用途说明")


class CancelReservationParams(BaseModel):
    """**有副作用**：会取消他人的/自己的预约记录。默认不暴露给模型。"""

    model_config = ConfigDict(extra="forbid")

    reservation_id: int = Field(description="要取消的预约 ID")
    reason: str = Field(default="", description="取消原因")


# ==========================================================================
# 领域实现
# ==========================================================================
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


# ==========================================================================
# 注册到 harness：契约 + 绑定
# ==========================================================================
# 描述文案是**给模型看的**，所以写成「什么时候该用它」而不是「它是什么」。
_TOOL_DESCRIPTIONS: dict[str, str] = {
    "query_availability": (
        "查询某个日期下满足约束的可用时段；若没有精确解，会返回放宽后的备选方案"
        "（每条都标注放宽了什么）。用户提出预约/查空闲需求时用这个。"
    ),
    "check_admission": (
        "检索实验室准入资质与安全规范条文，返回可引用的出处。"
        "用户问「需要什么资质」「有什么规定」「安全要求」时用这个。"
    ),
    "list_reservations": "列出当前登录用户自己的预约记录。",
    "create_reservation": (
        "创建预约。会真的占用时段，属于写操作 —— 需用户确认后执行。"
    ),
    "cancel_reservation": "取消一条预约。会改变记录状态，属于写操作 —— 需用户确认后执行。",
}


def build_registry(
    *,
    user_id: int,
    now: dt.datetime | None = None,
    result_limit: int = DEFAULT_RESULT_LIMIT,
) -> ToolRegistry:
    """把领域能力注册成 harness 的工具。

    ``user_id`` 在这里被**闭包注入**，而不是放进参数模型 ——
    这是 P0-2 那条结论的延续：身份只能来自认证结果，任何出现在
    「模型可填字段」里的身份都等于把越权入口重新打开。

    新增一个业务工具只需要在本函数里多注册一条，harness 一行不用改。
    """
    registry = ToolRegistry()

    async def _query_availability(**kw: Any) -> NegotiationResult:
        requirement = Requirement(
            date=kw.get("date"),
            start=kw.get("start"),
            end=kw.get("end"),
            duration_hours=kw.get("duration_hours"),
            equipment_name=kw.get("equipment_name"),
            category=kw.get("category"),
        ).normalized()
        return await tool_query_availability(user_id, requirement, now=now)

    async def _check_admission(**kw: Any) -> list[DocHit]:
        return await tool_check_admission(str(kw.get("query", "")))

    async def _list_reservations(**_: Any) -> list:
        return await tool_list_reservations(user_id)

    async def _create_reservation(**kw: Any) -> BookingOutcome:
        return await tool_create_reservation(
            user_id=user_id,
            equipment_id=int(kw["equipment_id"]),
            date_=kw["date"],
            start=kw["start"],
            end=kw["end"],
            purpose=str(kw.get("purpose") or ""),
        )

    async def _cancel_reservation(**kw: Any) -> BookingOutcome:
        return await tool_cancel_reservation(
            reservation_id=int(kw["reservation_id"]),
            user_id=user_id,
            reason=str(kw.get("reason") or ""),
        )

    registry.register(ToolSpec(
        name="query_availability",
        description=_TOOL_DESCRIPTIONS["query_availability"],
        params=QueryAvailabilityParams,
        handler=_query_availability,
        result_limit=result_limit,
    ))
    registry.register(ToolSpec(
        name="check_admission",
        description=_TOOL_DESCRIPTIONS["check_admission"],
        params=CheckAdmissionParams,
        handler=_check_admission,
        result_limit=result_limit,
    ))
    registry.register(ToolSpec(
        name="list_reservations",
        description=_TOOL_DESCRIPTIONS["list_reservations"],
        params=ListReservationsParams,
        handler=_list_reservations,
        result_limit=result_limit,
    ))
    # 下面两个带 side_effect=True：默认不进入给模型的工具清单，
    # 但仍可由业务代码（图路由 / 确认后的动作）显式调用。
    registry.register(ToolSpec(
        name="create_reservation",
        description=_TOOL_DESCRIPTIONS["create_reservation"],
        params=CreateReservationParams,
        handler=_create_reservation,
        side_effect=True,
        result_limit=result_limit,
    ))
    registry.register(ToolSpec(
        name="cancel_reservation",
        description=_TOOL_DESCRIPTIONS["cancel_reservation"],
        params=CancelReservationParams,
        handler=_cancel_reservation,
        side_effect=True,
        result_limit=result_limit,
    ))
    return registry


def tool_catalog() -> list[dict[str, Any]]:
    """给 ``/api/tools`` 与 ``doctor`` 用的展示清单。

    从 :func:`build_registry` **派生**而不是再手写一份：之前 ``TOOL_SPECS``
    是与实现分开维护的第二份清单，正是「两份定义必然漂移」的典型。
    这里用一个占位 user_id 建注册表只为取契约（handler 不会被调用）。
    """
    registry = build_registry(user_id=0)
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "params": list(spec.params.model_fields),
            "side_effect": spec.side_effect,
        }
        for spec in registry.specs()
    ]


# 兼容原有调用点（api.py 的 /api/tools、cli.py 的 tools 与 doctor）。
# 语义从「手写清单」变成「从注册表派生」，内容据此保持同步。
TOOL_SPECS: list[dict[str, Any]] = tool_catalog()
