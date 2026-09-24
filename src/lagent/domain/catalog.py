"""后台维护：实验室 / 设备 / 用户的增与改（P0-4）。

这一层存在的理由只有一个：**让"改数据"不必走"改代码 + 重新 seed"**。
原先新增一台设备要动 ``seed.py`` 再重建库 —— 而重建库会把真实的预约
一起清掉。对一个已经在用的系统来说，这不是"麻烦"，是不可接受。

关于**为什么没有删除**（这条要在代码里说清楚，否则会被当成遗漏）：

预约、门禁事件、审计都以 ``equipment_id`` / ``user_id`` 为外键引用这些实体。
真把它们删掉，历史记录就指向不存在的对象 —— 而"出事之后查不出是谁在用
哪台设备"恰好是这套系统最不能接受的状态。所以对外不提供删除，改用两个
语义明确的替代：

  * 设备 → ``status = "scrapped"``（报废：**不可预约**，历史与占用格原样保留）；
  * 用户 → 停用（清空口令哈希；``verify_password`` 对空串一律失败，
    于是"不可登录"是 fail-closed 且已被测试钉住）。

★ 另一个容易踩的坑：**资质有两套来源**。

  * ``User.certs`` —— 裸列表，**预约设备**时看它（``booking._validate``）；
  * ``CertGrant`` —— 带有效期的授权记录，**门禁核验**时看它。

后台给某人加一个类别，如果只改裸列表，那么对"已经有授权记录的老用户"
门禁**仍然判 missing**（``cert_states_for`` 在有授权记录时会忽略裸列表）。
结果就是"能约设备却进不了门" —— 两边都不报错，只有用户卡在门口。

所以 :func:`sync_certs` 把两处一起维护：新增类别补一条授权记录，
移除类别**撤销而不是删除**（谁在什么时候被取消了什么资格，必须查得到）。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..clock import now_local
from ..config import get_settings
from ..models import (
    ACTIVE_STATUSES,
    CertGrant,
    Equipment,
    Laboratory,
    Reservation,
    User,
)
from ..schemas import (
    EquipmentCreate,
    EquipmentUpdate,
    LabCreate,
    LabUpdate,
    UserCreate,
    UserUpdate,
)


class CatalogError(RuntimeError):
    """维护操作被拒绝。消息是**可以直接显示给管理员**的人话。"""


def changes_of(payload: Any) -> dict[str, Any]:
    """取出 PATCH 里真正要改的字段。

    两条规则，缺一个都会出事：

    1. ``exclude_unset=True`` —— 没传的字段不能参与赋值，否则
       "只改备注"会把其余字段全刷成 None；
    2. 显式传 ``null`` 也当作"不改" —— PATCH 语义里 null 是最自然的
       "我不管这个字段"，把它当"清空"会让一次手滑把开放时间抹掉。
       真要清空就传该类型的空值（``""`` / ``0`` / ``[]``）。
    """
    return {
        key: value
        for key, value in payload.model_dump(exclude_unset=True).items()
        if value is not None
    }


# --------------------------------------------------------------------------
# 实验室
# --------------------------------------------------------------------------
async def create_lab(session: AsyncSession, payload: LabCreate) -> Laboratory:
    lab = Laboratory(**payload.model_dump())
    session.add(lab)
    await _flush(session, "实验室")
    return await load_lab(session, lab.id) or lab


async def update_lab(
    session: AsyncSession, lab: Laboratory, payload: LabUpdate
) -> Laboratory:
    changes = changes_of(payload)
    if not changes:
        return lab
    for key, value in changes.items():
        setattr(lab, key, value)
    await _flush(session, "实验室")
    return await load_lab(session, lab.id) or lab


# --------------------------------------------------------------------------
# 设备
# --------------------------------------------------------------------------
async def create_equipment(
    session: AsyncSession, payload: EquipmentCreate
) -> Equipment:
    lab = await session.get(Laboratory, payload.lab_id)
    if lab is None:
        raise CatalogError(f"实验室 {payload.lab_id} 不存在")
    item = Equipment(**payload.model_dump())
    session.add(item)
    await _flush(session, "设备", hint=f"资产编号 {payload.code} 可能已被占用")
    return item


async def update_equipment(
    session: AsyncSession, item: Equipment, payload: EquipmentUpdate
) -> Equipment:
    changes = changes_of(payload)
    if not changes:
        return item
    if "lab_id" in changes:
        lab = await session.get(Laboratory, changes["lab_id"])
        if lab is None:
            raise CatalogError(f"实验室 {changes['lab_id']} 不存在")
    for key, value in changes.items():
        setattr(item, key, value)
    await _flush(session, "设备", hint=f"资产编号 {changes.get('code', '')} 可能已被占用")
    return item


async def count_active_reservations(session: AsyncSession, equipment_id: int) -> int:
    """这台设备上还有多少条**占着资源**的预约（pending + confirmed）。

    停用/报废设备时用它给管理员一句提示：设备下线了，但坑还被占着，
    那些预约不会自己消失 —— 不说出来的话，就会变成"学生到了门口发现
    设备没了，而系统里他还约着"。
    """
    total = await session.scalar(
        select(func.count())
        .select_from(Reservation)
        .where(
            Reservation.equipment_id == equipment_id,
            Reservation.status.in_(ACTIVE_STATUSES),
        )
    )
    return int(total or 0)


# --------------------------------------------------------------------------
# 用户
# --------------------------------------------------------------------------
async def create_user(session: AsyncSession, payload: UserCreate) -> User:
    from ..security import hash_password

    data = payload.model_dump()
    password = data.pop("password")
    user = User(**data, password_hash=hash_password(password))
    session.add(user)
    await _flush(session, "用户", hint=f"用户名 {payload.username} 或邮箱已存在")
    await sync_certs(session, user, payload.certs, actor="create")
    return user


async def update_user(session: AsyncSession, user: User, payload: UserUpdate) -> User:
    from ..security import hash_password

    changes = changes_of(payload)
    if not changes:
        return user
    password = changes.pop("password", None)
    certs = changes.pop("certs", None)
    for key, value in changes.items():
        setattr(user, key, value)
    if password is not None:
        # 重设口令即等于"恢复登录"：停用就是把哈希清空，填上新的就解锁了
        user.password_hash = hash_password(password)
    await _flush(session, "用户", hint="用户名或邮箱可能已被占用")

    if certs is not None:
        await sync_certs(session, user, certs, actor="update")
    return user


async def deactivate_user(session: AsyncSession, user: User) -> User:
    """停用：清空口令哈希。

    为什么不去加一个 ``is_active`` 列：那需要迁移，且会引入"停用后
    已有预约怎么办"的第二种语义。而**空口令哈希本来就不可登录**
    （``verify_password`` 对空串一律失败，fail-closed），语义已经存在，
    不需要新状态。重新设一次口令即恢复登录。

    历史数据（预约 / 门禁 / 审计）一条都不动。
    """
    user.password_hash = ""
    await _flush(session, "用户")
    return user


async def sync_certs(
    session: AsyncSession,
    user: User,
    categories: list[str],
    *,
    actor: str,
) -> tuple[list[str], list[str]]:
    """把 ``User.certs`` 与 ``CertGrant`` 维护成一致。返回 (新增, 移除)。

    ★ 这是"能约设备却进不了门"那个坑的修复点（见模块 docstring）。
    """
    today = now_local().date()
    valid_days = get_settings().cert_valid_days
    wanted = [c for c in dict.fromkeys(categories) if c.strip()]
    current = list(user.certs or [])

    added = [c for c in wanted if c not in current]
    removed = [c for c in current if c not in wanted]

    existing = (
        await session.execute(
            select(CertGrant).where(CertGrant.user_id == user.id)
        )
    ).scalars().all()
    grants: list[CertGrant] = list(existing)

    for category in added:
        same = [g for g in grants if g.category == category]
        same = [g for g in existing if g.category == category]
        # 唯一索引打在 (user_id, category, granted_at) 上 —— 同一天重复授予会撞车。
        # 「复训换证 = 更晚的 granted_at」，所以顺延一天，且这与
        # 「以最新授予的那条为准」的判定规则本来就是一致的。
        latest = max((g.granted_at for g in same), default=None)
        granted = (
            latest + dt.timedelta(days=1)
            if latest is not None and latest >= today
            else today
        )
        grant = CertGrant(
            user_id=user.id,
            category=category,
            granted_at=granted,
            expires_at=today + dt.timedelta(days=valid_days),
            evidence=f"admin:{actor}",
            note=f"后台维护授予，默认有效期 {valid_days} 天",
        )
        session.add(grant)
        grants.append(grant)

    now = now_local()
    for category in removed:
        # 撤销而不是删除：留着"曾经授过、何时被撤"这条线索
        for grant in (g for g in grants if g.category == category):
            if grant.revoked_at is None:
                grant.revoked_at = now

    user.certs = wanted
    await _flush(session, "资质")
    return added, removed


async def load_lab(session: AsyncSession, lab_id: int) -> Laboratory | None:
    """带设备预加载地取实验室。

    ⚠️ 必须 eager load：异步 session 里访问未加载的关系会抛 MissingGreenlet
    （懒加载需要一个同步 IO 上下文，async 驱动里没有）。
    刚新建出来的对象关系还没被加载过，而序列化要读 ``lab.equipment``。
    """
    return (
        await session.execute(
            select(Laboratory)
            .options(selectinload(Laboratory.equipment))
            .where(Laboratory.id == lab_id)
        )
    ).scalar_one_or_none()




async def load_equipment(session: AsyncSession, equipment_id: int) -> Equipment | None:
    return (
        await session.execute(
            select(Equipment)
            .options(selectinload(Equipment.lab))
            .where(Equipment.id == equipment_id)
        )
    ).scalar_one_or_none()


# --------------------------------------------------------------------------
# 内部
# --------------------------------------------------------------------------
async def _flush(session: AsyncSession, what: str, *, hint: str = "") -> None:
    """flush 并把唯一约束冲突翻译成人话。

    不翻译的话管理员看到的是 ``UNIQUE constraint failed: equipment.code``
    这种数据库行话 —— 而真正需要告诉他的是"这个资产编号已经用过了"。
    """
    try:
        await session.flush()
    except IntegrityError as exc:
        raise CatalogError(
            f"{what}保存失败：与已有记录冲突{'（' + hint + '）' if hint else ''}"
        ) from exc
