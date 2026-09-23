"""种子数据：把「一个有真实约束、真实冲突」的实验室场景灌进库。

刻意让数据本身携带冲突与权限差异，这样演示和评测才有意义：
  * 张伟只有光谱类资质 → 预约离心机必须被拒（资质约束真的会拦）
  * 李娜资质齐全 → 同样诉求能过
  * 分析楼 301 的荧光光谱仪当天下午被占 → 触发协商而非「不可预约」
  * 材料楼 412 周末不开放 → 触发「改日期」这一类放宽
  * 管理员角色 → 唯一能看全量预约与调用 /api/users 的身份

演示账号与口令固定（README 有同一份），因为「能登录进来看」是演示的前提。
口令一律以 scrypt 哈希入库，明文不落盘、不写日志。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from .clock import now_local
from .db import init_db, session_scope
from .domain.booking import attach_slots
from .models import (
    Equipment,
    Laboratory,
    Reservation,
    ReservationSlot,
    User,
)
from .security import hash_password

LABS: list[dict] = [
    {
        "building": "分析楼", "floor": 3, "room": "301", "capacity": 6,
        "open_hours": {"weekday": ["08:00", "22:00"], "weekend": ["09:00", "18:00"]},
        "note": "光谱与色谱类设备集中在此，需刷卡进入",
    },
    {
        "building": "生物楼", "floor": 2, "room": "205", "capacity": 4,
        "open_hours": {"weekday": ["08:00", "20:00"], "weekend": ["10:00", "16:00"]},
        "note": "细胞培养专用，进入前须更换专用鞋套",
    },
    {
        "building": "材料楼", "floor": 4, "room": "412", "capacity": 2,
        "open_hours": {"weekday": ["09:00", "18:00"], "weekend": ["09:00", "18:00"]},
        "note": "高速离心机专用间，需两人同时在场",
    },
]

EQUIPMENT: list[dict] = [
    {"lab": 0, "name": "荧光光谱仪", "model": "F-7000", "code": "SPEC-F7000",
     "category": "光谱", "max_hours": 4, "requires_training": True},
    {"lab": 0, "name": "紫外可见分光光度计", "model": "UV-1900", "code": "SPEC-UV1900",
     "category": "光谱", "max_hours": 4, "requires_training": False},
    {"lab": 0, "name": "高效液相色谱仪", "model": "LC-2030", "code": "CHRO-LC2030",
     "category": "色谱", "max_hours": 6, "requires_training": True},
    {"lab": 1, "name": "CO2 培养箱", "model": "MCO-170", "code": "CELL-CO2170",
     "category": "细胞培养", "max_hours": 6, "requires_training": True},
    {"lab": 1, "name": "生物安全柜", "model": "BSC-1300IIA2", "code": "CELL-BSC1300",
     "category": "细胞培养", "max_hours": 4, "requires_training": True},
    {"lab": 2, "name": "高速离心机", "model": "CR-21N", "code": "CENT-CR21N",
     "category": "离心", "max_hours": 2, "requires_training": True},
]

USERS: list[dict] = [
    {"username": "张伟", "email": "zhangwei@example.com", "role": "user",
     "certs": ["光谱"], "password": "zhangwei@123"},
    {"username": "李娜", "email": "lina@example.com", "role": "user",
     "certs": ["光谱", "色谱", "细胞培养", "离心"], "password": "lina@123"},
    {"username": "管理员", "email": "admin@example.com", "role": "admin",
     "certs": ["光谱", "色谱", "细胞培养", "离心"], "password": "admin@123"},
]

# 种子口令的哈希缓存。
#
# scrypt 是**故意慢**的（2**14 ≈ 140ms/次），如果每次 seed() 都重算 3 个口令，
# 那 200 个 pytest 用例每例一套独立库，光seed 就要多花一分多钟。
# 演示口令是固定常量，哈希也只依赖口令本身，所以进程内算一次就够。
_PASSWORD_HASH_CACHE: dict[str, str] = {}


def demo_password_hash(password: str) -> str:
    """取（并缓存）演示口令的哈希。"""
    cached = _PASSWORD_HASH_CACHE.get(password)
    if cached is None:
        cached = hash_password(password)
        _PASSWORD_HASH_CACHE[password] = cached
    return cached


async def seed(force: bool = False) -> dict:
    """建表并灌入种子数据。已存在且 force=False 时跳过。"""
    await init_db()
    info: dict = {"seeded": False, "reason": ""}

    async with session_scope() as session:
        count = (await session.execute(select(func.count()).select_from(Laboratory))).scalar() or 0
        if count and not force:
            info["reason"] = f"已有 {count} 个实验室，跳过。要重建请加 --force"
            return info

        if force:
            # 删除顺序必须是「子表在前」：占用格引用预约，预约引用设备/实验室/用户。
            # SQLite 已开 PRAGMA foreign_keys=ON，顺序错了会直接被外键拦下。
            for model in (ReservationSlot, Reservation, Equipment, Laboratory, User):
                await session.execute(model.__table__.delete())

        labs: list[Laboratory] = []
        for row in LABS:
            lab = Laboratory(**row)
            session.add(lab)
            labs.append(lab)
        await session.flush()

        equipment: list[Equipment] = []
        for row in EQUIPMENT:
            data = dict(row)
            lab_index = data.pop("lab")
            item = Equipment(lab_id=labs[lab_index].id, **data)
            session.add(item)
            equipment.append(item)
        await session.flush()

        users: list[User] = []
        for row in USERS:
            data = dict(row)
            # password 是种子数据的输入，不是 User 的列；换成哈希再入库
            password = data.pop("password", "")
            user = User(**data, password_hash=demo_password_hash(password) if password else "")
            session.add(user)
            users.append(user)
        await session.flush()

        # 造两个「已有预约」，让协商有东西可谈
        today = now_local().date()
        tomorrow = today + dt.timedelta(days=1)
        demo: list[Reservation] = []
        # 荧光光谱仪明天 14:00-16:00 已被张伟占用 → 李娜再约同一时段会触发协商
        demo.append(Reservation(
            user_id=users[0].id, equipment_id=equipment[0].id,
            date=tomorrow, start_time=dt.time(14, 0), end_time=dt.time(16, 0),
            status="confirmed", purpose="薄膜样品荧光测试",
        ))
        spectro = equipment[0]
        demo.append(Reservation(
            user_id=users[1].id, equipment_id=spectro.id,
            date=tomorrow, start_time=dt.time(16, 30), end_time=dt.time(18, 0),
            status="confirmed", purpose="量子点表征",
        ))
        for row in demo:
            session.add(row)
        # 演示预约也要登记占用格 —— 否则它们不参与「区间不重叠」判定，
        # 演示数据本身就成了一个绕过不变式的后门。
        await session.flush()
        for row in demo:
            await attach_slots(session, row)

        info.update({
            "seeded": True,
            "labs": len(labs),
            "equipment": len(equipment),
            "users": len(users),
            "reservations": len(demo),
            "demo_date": tomorrow.isoformat(),
        })
    return info
