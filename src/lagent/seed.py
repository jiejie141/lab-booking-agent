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
import random

from sqlalchemy import func, select

from .clock import now_local
from .config import get_settings
from .db import ensure_schema, session_scope
from .domain.booking import attach_slots
from .models import (
    LAB_BASIC_CERT,
    ROLE_USER,
    STATUS_PENDING,
    CertGrant,
    Equipment,
    Laboratory,
    Reservation,
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

# ---------------------------------------------------------------------------
# 演示账号
#
# ⚠️ **这条列表只能往后追加，不能重排、不能插队。**
#
# 原因：张伟=1 / 李娜=2 / 管理员=3 被 23 个测试文件、61 处硬编码 user_id 依赖
# （``tests/test_concurrency.py`` 直接用 ``user_id=1/2`` 调 domain 函数，
#  ``scripts/accept_deploy.py`` 也写死了 ``USER_ID = 2``）。
# 插在中间**不会报错** —— 那些用例只会静默地拿错人去测，
# 而"测试还是绿的"会让人以为改对了。
#
# 每个账号都证明一条不同的边界，这是**刻意的**（见模块 docstring）：
# 张伟缺资质 / 李娜资质齐全 / 管理员看全量 / 系统管理员独占改角色权。
DEMO_USERS: list[dict] = [
    {"username": "张伟", "email": "zhangwei@example.com", "role": "user",
     "certs": ["光谱"], "password": "zhangwei@123"},
    {"username": "李娜", "email": "lina@example.com", "role": "user",
     "certs": ["光谱", "色谱", "细胞培养", "离心"], "password": "lina@123"},
    {"username": "管理员", "email": "admin@example.com", "role": "admin",
     "certs": ["光谱", "色谱", "细胞培养", "离心"], "password": "admin@123"},
    # 2026-09-29 新增：sysadmin 在此之前**没有任何账号** ——
    # 它在 models.py 里被声明、在 Principal.is_admin 里被当成 admin 的别名，
    # 于是"三级角色"只存在于类型标注里。给它一个真实账号，
    # 层级才第一次变成可以登录进去看的东西。
    {"username": "系统管理员", "email": "sysadmin@example.com", "role": "sysadmin",
     "certs": ["光谱", "色谱", "细胞培养", "离心"], "password": "sysadmin@123"},
]

# 演示账号的口令表。**单一来源**：tests/conftest.py 从这里派生，
# 而不是自己再抄一份（抄一份的后果是加账号时两边不同步）。
DEMO_ACCOUNTS: dict[str, str] = {
    row["username"]: row["password"] for row in DEMO_USERS
}

# ---------------------------------------------------------------------------
# 合成账号（演示 / 压测用，默认 0 个）
# ---------------------------------------------------------------------------
_FAMILY_NAMES: tuple[str, ...] = (
    "王", "李", "张", "刘", "陈", "杨", "黄", "赵", "周", "吴",
    "徐", "孙", "马", "朱", "胡", "郭", "林", "何", "高", "罗",
)
_GIVEN_NAMES: tuple[str, ...] = (
    "伟", "芳", "娜", "敏", "静", "丽", "强", "磊", "洋", "勇",
    "艳", "杰", "娟", "涛", "明", "超", "雨欣", "子涵", "思远", "佳怡",
)

# 合成账号的资质分布。刻意**不是**人人都有全部资质 ——
# 一堆同质账号除了让列表变长没有任何演示价值。
# 用固定种子而不是系统随机：seed() 会在测试夹具里跑，
# 生成结果必须可复现，否则"这次绿、下次红"分不清是代码问题还是数据问题。
_SYNTHETIC_SEED = 20260929


def equipment_categories() -> tuple[str, ...]:
    """设备类别清单。**从 EQUIPMENT 派生**，不手写第二份。

    否则加一类设备时，合成用户会拿到一批约不到任何设备的人 ——
    而这在界面上看起来只是"他没预约"，看不出是数据生成错了。
    """
    return tuple(dict.fromkeys(row["category"] for row in EQUIPMENT))


def synthetic_users(
    count: int,
    categories: tuple[str, ...],
    password: str = "demo@123",
) -> list[dict]:
    """生成 ``count`` 个合成账号。**必须追加在 DEMO_USERS 之后。**

    返回值是 ``DEMO_USERS`` 同构的字典，多出两个字段供灌库时使用：

    * ``no_basic`` —— 不发「实验室安全」资质，于是他**进不了任何房间**；
    * ``cert_state`` —— ``{类别: "expired"|"revoked"}``，让授权记录带上
      真实世界里那两种失效路径（过期 vs 撤销给用户的提示语不同）。

    ⚠️ 合成账号**共用同一个口令**，这是刻意的：scrypt 每次约 140ms，
    各不同口令意味着 500 个用户要 70 秒；共用口令下
    :func:`demo_password_hash` 的缓存只需算一次。
    因此它们**只用于演示与压测**，默认数量为 0（见 config.seed_demo_users）。
    """
    if count <= 0 or not categories:
        return []

    rng = random.Random(_SYNTHETIC_SEED)
    # ⚠️ 名字池里**真的**有演示账号：李+娜=李娜（index 41）、张+伟=张伟（index 2）。
    # 撞上就是 UNIQUE constraint failed，整个 seed 直接崩 ——
    # 而且是"种子数量调到 50 才炸"这种只在特定配置下出现的崩溃。
    # 所以先把演示账号的名字占住，宁可让合成账号带个序号。
    used: set[str] = {u["username"] for u in DEMO_USERS}
    out: list[dict] = []

    for index in range(count):
        # 名字：先走姓×名的笛卡尔积，用尽之后加序号保证唯一
        # （username 有唯一索引，撞了是 IntegrityError，不是"少一个人"）
        family = _FAMILY_NAMES[index % len(_FAMILY_NAMES)]
        given = _GIVEN_NAMES[(index // len(_FAMILY_NAMES)) % len(_GIVEN_NAMES)]
        name = f"{family}{given}"
        if name in used or index >= len(_FAMILY_NAMES) * len(_GIVEN_NAMES):
            name = f"{family}{given}{index:03d}"
        used.add(name)

        # 资质条数 1..len(categories)，偏少而不是全给
        how_many = 1 + (index % max(1, len(categories)))
        picked = rng.sample(list(categories), k=min(how_many, len(categories)))

        spec: dict = {
            "username": name,
            "email": f"demo{index:03d}@example.com",
            "role": ROLE_USER,
            "certs": picked,
            "password": password,
            "no_basic": False,
            "cert_state": {},
        }

        # 四种"值得演示的异常"，各占约 5%（用取模而不是随机，保证可复现）
        bucket = index % 20
        if bucket == 3:
            # 没有基础安全资质 → 进不了任何实验室
            spec["no_basic"] = True
        elif bucket == 7 and picked:
            # 资质过期 → 提示"该复训"，与"没资质"是两句话
            spec["cert_state"] = {picked[0]: "expired"}
        elif bucket == 15 and picked:
            # 资质被撤销 → 提示"被停权"
            spec["cert_state"] = {picked[0]: "revoked"}

        out.append(spec)
    return out


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


async def seed(
    force: bool = False,
    *,
    extra_users: int | None = None,
    pending: int | None = None,
) -> dict:
    """建表并灌入种子数据。已存在且 force=False 时跳过。

    ``force=True`` 是**真正的重建**：把表删掉重来（``rebuild=True``），
    而不是只把行删空。这一点曾经写错过，代价是 README 里那句
    「要重建请加 --force」**本身是失效的**：

    只删行的话，库里缺的**列**（比如 P0-2 新增的 ``users.password_hash``）
    永远不会被补上，于是「重建」过程自己就撞在
    ``table users has no column named password_hash`` 上 ——
    用户按文档操作，拿到的是另一个看不懂的报错。

    结构校验交给 :func:`lagent.db.ensure_schema`：所有调用 seed 的路径
    （CLI、服务启动、测试夹具）都会自动获得「库过期就明确报错」的行为。

    ``extra_users`` / ``pending`` 为 ``None`` 时回落到配置项
    （``seed_demo_users`` / ``seed_pending_reservations``，默认都是 0）。
    显式传值只发生在 CLI —— **默认必须是 0**，否则每次起服务都会
    灌进一批共用口令的账号（理由见 config.py 里那段注释）。
    """
    await ensure_schema(rebuild=force)
    info: dict = {"seeded": False, "reason": ""}

    async with session_scope() as session:
        count = (await session.execute(select(func.count()).select_from(Laboratory))).scalar() or 0
        if count and not force:
            info["reason"] = f"已有 {count} 个实验室，跳过。要重建请加 --force"
            return info

        # force=True 时表刚被 drop + create 过，行本来就是空的：
        # 这里不再需要原先那套「按子表在前的顺序逐表删行」。
        # 那段代码看起来在重建、实际只删行，正是本次修掉的坑。

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

        settings = get_settings()
        # 显式 int()：让 mypy 看到的是 int 而不是 int | None（参数是可空的）
        want_users = int(
            settings.seed_demo_users if extra_users is None else extra_users
        )
        want_pending = int(
            settings.seed_pending_reservations if pending is None else pending
        )
        # 演示账号在前（id 必须稳定，见 DEMO_USERS 的注释），合成账号**追加**在后
        specs: list[dict] = list(DEMO_USERS) + synthetic_users(
            want_users,
            equipment_categories(),
            settings.seed_demo_password,
        )

        users: list[User] = []
        for row in specs:
            data = dict(row)
            # password 是种子数据的输入，不是 User 的列；换成哈希再入库。
            # no_basic / cert_state 同理 —— 它们只作用于授权记录那一步。
            password = data.pop("password", "")
            data.pop("no_basic", None)
            data.pop("cert_state", None)
            user = User(**data, password_hash=demo_password_hash(password) if password else "")
            session.add(user)
            users.append(user)
        await session.flush()

        # 资质授权（带有效期）。
        #
        # 两件事在这里一次办掉：
        # 1. **发基础安全资质。** 进任何实验室都需要「实验室安全」，
        #    而 ``User.certs`` 里没有这一项。不发的话升级当天三个演示账号
        #    全都进不了任何房间 —— 功能是好的，但没人能用，看不出效果。
        # 2. **把历史 certs 镜像成授权记录。** 让新老两条读取路径都有数据，
        #    便于对照；历史字段的兼容逻辑另由 tests/test_access.py 单独覆盖。
        #
        # ⚠️ 有效期给到很远的未来是**刻意的**：演示数据一旦会过期，
        # 用例就会在某个日期之后开始莫名其妙地失败（"李娜进不去实验室了"
        # 而代码没改过）。会过期的场景由测试自己造短效授权来覆盖。
        today = now_local().date()
        for user, spec in zip(users, specs, strict=True):
            # 合成账号里有一批**故意不发**基础安全资质：他们进不了任何房间。
            # 演示价值在于把「没资质」和「有资质但过期/被撤销」分成三种不同提示。
            categories = [] if spec.get("no_basic") else [LAB_BASIC_CERT]
            categories += list(user.certs)
            states: dict[str, str] = spec.get("cert_state") or {}
            for category in dict.fromkeys(categories):  # 保序去重
                state = states.get(category, "active")
                if state == "expired":
                    expires_at = today - dt.timedelta(days=1)
                    evidence = f"seed:{user.username}:{category}:已过期（需复训）"
                else:
                    expires_at = dt.date(2099, 12, 31)
                    evidence = f"seed:{user.username}:{category}"
                session.add(CertGrant(
                    user_id=user.id,
                    category=category,
                    granted_at=dt.date(2025, 1, 1),
                    expires_at=expires_at,
                    # 撤销走**独立的失效路径**：过期是"到点了"，撤销是"被停了"，
                    # 给用户的提示语完全不同（该复训 / 该找管理员）。
                    revoked_at=now_local() if state == "revoked" else None,
                    evidence=evidence,
                ))
        await session.flush()

        # 造两个「已有预约」，让协商有东西可谈
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
        for res in demo:
            session.add(res)
        # 演示预约也要登记占用格 —— 否则它们不参与「区间不重叠」判定，
        # 演示数据本身就成了一个绕过不变式的后门。
        await session.flush()
        for res in demo:
            await attach_slots(session, res)

        # --------------------------------------------------------------
        # 待审批队列（默认 0 条）
        #
        # 在此之前 seed **一条 pending 都不造**，也没有任何设备设了
        # ``requires_approval`` —— 于是管理员的「审批待办」面板打开是空的。
        # 演示时点进去看到"暂无待办"，而审批恰恰是 pending 状态的核心卖点：
        # 功能是好的，只是没有东西可批，看不出效果。
        #
        # ⚠️ pending **占坑**：``ACTIVE_STATUSES = (pending, confirmed)``
        # （models.py:62），domain/booking.py 里也写了"申请即占坑"。
        # 所以这里必须和上面那条 confirmed 一样走 ``attach_slots``，
        # 否则演示数据就成了绕过不变式的后门。
        # --------------------------------------------------------------
        # 要 pending 却没有一台「需审批」的设备，是配置矛盾 ——
        # 静默产出 0 条待办会让人以为开关坏了。这里自动开一台，并在返回里说明。
        approval_count = min(settings.seed_approval_equipment, len(equipment))
        if want_pending and approval_count == 0:
            approval_count = min(1, len(equipment))
        for item in equipment[:approval_count]:
            item.requires_approval = True
        if approval_count:
            await session.flush()

        pending_count = 0
        if want_pending and approval_count and len(users) > 2:
            # 变量不叫 pending —— 那是本函数的参数名，重名会遮蔽它
            pending_rows: list[Reservation] = []
            # 从演示账号之后的人里轮流取，避免全压在一个人身上
            for offset in range(want_pending):
                user = users[2 + (offset % max(1, len(users) - 2))]
                item = equipment[offset % approval_count]
                # 错开时段，避免自己撞自己（同一台设备同一时段会被唯一索引挡下）。
                # ⚠️ 还要避开**明天的演示预约**：上面那两条已经占了
                # equipment[0] 的 14:00 与 16:30，而自动开启的需审批设备
                # 恰好就是 equipment[0]。撞上的后果是 IntegrityError
                # —— 整个 seed 崩掉，而不是"少一条待办"。所以从后天开始排。
                start_hour = 9 + (offset % 8)
                day = tomorrow + dt.timedelta(days=1 + offset // 8)
                pending_rows.append(Reservation(
                    user_id=user.id, equipment_id=item.id,
                    date=day, start_time=dt.time(start_hour, 0),
                    end_time=dt.time(start_hour + 1, 0),
                    status=STATUS_PENDING,
                    purpose=f"待审批示例 #{offset + 1}",
                ))
            for res in pending_rows:
                session.add(res)
            await session.flush()
            for res in pending_rows:
                await attach_slots(session, res)
            pending_count = len(pending_rows)

        info.update({
            "seeded": True,
            "labs": len(labs),
            "equipment": len(equipment),
            "users": len(users),
            "demo_users": len(DEMO_USERS),
            "synthetic_users": len(specs) - len(DEMO_USERS),
            "reservations": len(demo),
            "pending_reservations": pending_count,
            "approval_equipment": approval_count,
            "demo_date": tomorrow.isoformat(),
        })
    return info
