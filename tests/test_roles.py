"""角色层级与权限模型的守卫测试（P0-3）。

这个文件存在的理由只有一条：**角色体系加了层级，就得有人盯着它别退化。**

在这之前，"三级角色"事实上只有两级：``Principal.is_admin`` 把 admin 与
sysadmin 折成同一个布尔值（security.py），sysadmin 只是一个**没人实现的声明**。
而 ``USER_ROLES`` 这个"角色清单单一来源"全仓**零消费** —— 加角色漏登记不报错。

所以下面钉住的不是"这些函数返回什么"，而是四条**容易被改回去的设计决定**：

1. 角色取值域是封闭的，且与类型标注一致（不能出现"清单里有、类型里没有"）；
2. sysadmin 必须**真的**比 admin 多一个能力 —— 否则层级是假的；
3. 演示账号的 id 必须稳定（几十个测试文件里写着 user_id=1/2/3）；
4. 提权必须被拒：不能把别人设成 / 改成不低于自己的角色。

★ 第 4 条是最容易被"顺手放通"的：写业务时为了省事加一句
``if actor.role == "admin"``，提权面就开了。这里的用例就是那道闸。
"""

from __future__ import annotations

from typing import get_args

import pytest
from sqlalchemy import func, select

from lagent.db import dispose_engine, init_db, isolated_database, session_scope
from lagent.models import (
    CAP_ROLE_MANAGE,
    CAP_USER_READ_ALL,
    ROLE_ADMIN,
    ROLE_CAPS,
    ROLE_RANK,
    ROLE_SYSADMIN,
    ROLE_USER,
    STATUS_PENDING,
    USER_ROLES,
    Equipment,
    Reservation,
    ReservationSlot,
    User,
    UserRole,
    role_capabilities,
    role_rank,
)
from lagent.security import Principal
from lagent.seed import DEMO_ACCOUNTS, DEMO_USERS, seed

ZHANGWEI, LINA, ADMIN, SYSADMIN = 1, 2, 3, 4


# ===========================================================================
# 一、取值域：角色清单必须是封闭的，且与类型标注一致
# ===========================================================================
class TestRoleVocabularyIsClosed:
    def test_user_roles_matches_the_literal(self):
        """``UserRole`` 与 ``USER_ROLES`` 必须一致。

        这两处一旦漂移，就会出现"类型说合法、运行时说不合法"（或反过来），
        而报错信息会指向完全不相干的地方。
        """
        assert tuple(USER_ROLES) == tuple(get_args(UserRole))

    def test_every_role_has_a_rank_and_a_capability_set(self):
        """★ 加角色时最容易漏的就是这一条。

        漏登记的后果不是报错，而是**这个角色的账号一个能力点都没有**：
        表现为"什么都做不了"，和"权限配错了"长得一模一样，只能靠人猜。
        """
        for role in USER_ROLES:
            assert role in ROLE_RANK, f"{role} 没有等级"
            assert role in ROLE_CAPS, f"{role} 没有能力点"

    def test_rank_strictly_increases(self):
        """等级必须严格递增 —— 它是"能不能碰这个账号"的比较依据，
        两个同级角色会让"严格更高"这条规则失效。"""
        ranks = [ROLE_RANK[r] for r in (ROLE_USER, ROLE_ADMIN, ROLE_SYSADMIN)]
        assert ranks == sorted(set(ranks)), f"等级不是严格递增：{ranks}"

    def test_unknown_role_fails_closed(self):
        """未知角色 → 等级 0、能力点空集。

        **不能抛异常**：这条路径会在读老数据时走到，抛异常等于
        "库里有一条认不出的角色，于是整个花名册打不开"。
        fail-closed（当普通用户）才是对的。
        """
        assert role_rank("boss") == 0
        assert role_capabilities("boss") == ()


# ===========================================================================
# 二、能力点：sysadmin 与 admin 的差异必须真实存在
# ===========================================================================
class TestCapabilityModel:
    def test_sysadmin_has_a_capability_admin_lacks(self):
        """★★ 本文件最重要的一条：sysadmin 不能只是"另一个名字的 admin"。

        在整个项目里，这是唯一一处把「三级角色」从类型标注变成真实差异的地方。
        这条用例要是红了，说明层级又被拍平回两级了。
        """
        admin_caps = set(role_capabilities(ROLE_ADMIN))
        sysadmin_caps = set(role_capabilities(ROLE_SYSADMIN))
        assert sysadmin_caps - admin_caps, "sysadmin 与 admin 完全同权，层级是假的"
        assert CAP_ROLE_MANAGE in sysadmin_caps
        assert CAP_ROLE_MANAGE not in admin_caps

    def test_sysadmin_is_a_superset_of_admin(self):
        """sysadmin = admin + 改角色的权力，不是一套并列的权限。

        如果它是并集之外的另一套，就会出现"sysadmin 反而批不了预约"这种洞。
        """
        assert set(role_capabilities(ROLE_ADMIN)) <= set(
            role_capabilities(ROLE_SYSADMIN)
        )

    def test_normal_user_has_no_capabilities(self):
        assert role_capabilities(ROLE_USER) == ()

    @pytest.mark.parametrize(
        ("role", "capability", "expected"),
        [
            (ROLE_USER, CAP_USER_READ_ALL, False),
            (ROLE_ADMIN, CAP_USER_READ_ALL, True),
            (ROLE_SYSADMIN, CAP_USER_READ_ALL, True),
            (ROLE_ADMIN, CAP_ROLE_MANAGE, False),
            (ROLE_SYSADMIN, CAP_ROLE_MANAGE, True),
        ],
    )
    def test_principal_can(self, role: str, capability: str, expected: bool):
        assert Principal(user_id=1, username="x", role=role).can(capability) is expected

    def test_is_admin_is_derived_from_capabilities(self):
        """``is_admin`` 保留是为了那 12 处调用点，但它必须是**推导出来的**，
        不能退化成又一处字符串比较 —— 否则加角色又要手改。"""
        assert Principal(user_id=1, username="x", role=ROLE_ADMIN).is_admin is True
        assert Principal(user_id=1, username="x", role=ROLE_SYSADMIN).is_admin is True
        assert Principal(user_id=1, username="x", role=ROLE_USER).is_admin is False


# ===========================================================================
# 三、演示账号：id 必须稳定，且真的能登录
# ===========================================================================
class TestDemoAccounts:
    async def test_demo_account_ids_are_stable(self, http):
        """★ 演示账号的 id 是**契约**，不是巧合。

        二十几个测试文件里写着 ``user_id=1``（张伟）/ ``2``（李娜）/ ``3``（管理员），
        文档与演示脚本里也是这么写的。所以新账号只能**往后追加** ——
        往中间插一个，全仓会以一种"看起来像业务逻辑错了"的方式红掉。
        """
        users = (await http.get("/api/users", headers=await _as(http, "管理员"))).json()
        by_name = {u["username"]: u["id"] for u in users}
        assert by_name["张伟"] == ZHANGWEI
        assert by_name["李娜"] == LINA
        assert by_name["管理员"] == ADMIN
        assert by_name["系统管理员"] == SYSADMIN

    async def test_every_demo_account_can_log_in(self, http):
        """README 里那张账号表必须**逐条能用** ——
        演示时照着表输口令，输到第三行登不进去是最尴尬的一种失败。"""
        for username, password in DEMO_ACCOUNTS.items():
            resp = await http.post(
                "/api/auth/login", json={"username": username, "password": password}
            )
            assert resp.status_code == 200, f"{username} 登不进去：{resp.text}"

    async def test_me_exposes_rank_and_capabilities(self, http):
        """前端不再比较角色字符串，靠的就是这两个字段。
        它们要是没下发，前端会**静默地**把所有管理员功能藏起来。"""
        for username, expected_role in (
            ("张伟", ROLE_USER),
            ("管理员", ROLE_ADMIN),
            ("系统管理员", ROLE_SYSADMIN),
        ):
            me = (
                await http.get("/api/auth/me", headers=await _as(http, username))
            ).json()
            assert me["role"] == expected_role
            assert me["rank"] == role_rank(expected_role)
            assert set(me["capabilities"]) == set(role_capabilities(expected_role))

    async def test_demo_users_are_declared_in_one_place(self):
        """账号表只有一处定义：seed.DEMO_USERS。
        DEMO_ACCOUNTS 从它派生，测试口令也从它派生 —— 不允许第二份清单。"""
        assert [u["username"] for u in DEMO_USERS] == list(DEMO_ACCOUNTS)
        assert all(u.get("password") for u in DEMO_USERS)


async def _as(http, username: str) -> dict:
    """登录并返回请求头（本文件不依赖 conftest 的 as_user，便于单独跑）。"""
    token = (
        await http.post(
            "/api/auth/login",
            json={"username": username, "password": DEMO_ACCOUNTS[username]},
        )
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ===========================================================================
# 四、提权必须被拒 —— 这一组是闸门
# ===========================================================================
class TestEscalationIsRejected:
    @pytest.mark.parametrize("role", [ROLE_ADMIN, ROLE_SYSADMIN])
    async def test_admin_cannot_create_a_peer_or_higher(self, http, role):
        """admin 造不出与自己同级、更造不出比自己高的账号。"""
        admin = await _as(http, "管理员")
        resp = await http.post(
            "/api/users",
            json={"username": "越权", "email": "e@example.com", "role": role,
                  "password": "long-enough"},
            headers=admin,
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["reason"] == "role_escalation"

    async def test_granting_an_admin_role_requires_sysadmin(self, http):
        """★ 第二道锁：把人设成 admin **额外**需要 role.manage。

        光有"等级严格更高"还不够 —— sysadmin 造 admin 时等级是够的，
        但"谁能决定谁是管理员"这件事要单独收口到 sysadmin。
        """
        # admin 撞的是第一道锁（等级不够），这里验证原因码区分得开
        admin = await _as(http, "管理员")
        resp = await http.post(
            "/api/users",
            json={"username": "副手", "email": "d@example.com", "role": ROLE_ADMIN,
                  "password": "long-enough"},
            headers=admin,
        )
        assert resp.json()["reason"] == "role_escalation"

        sysadmin = await _as(http, "系统管理员")
        ok = await http.post(
            "/api/users",
            json={"username": "副手", "email": "d@example.com", "role": ROLE_ADMIN,
                  "password": "long-enough"},
            headers=sysadmin,
        )
        assert ok.status_code == 201, ok.text

    async def test_sysadmin_cannot_create_another_sysadmin(self, http):
        """sysadmin 也不能复制自己（rank 2 >= 2）。
        否则两个 sysadmin 可以互相把对方的角色改掉。"""
        sysadmin = await _as(http, "系统管理员")
        resp = await http.post(
            "/api/users",
            json={"username": "影子", "email": "s@example.com",
                  "role": ROLE_SYSADMIN, "password": "long-enough"},
            headers=sysadmin,
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["reason"] == "role_escalation"

    async def test_an_unknown_role_is_422_not_500(self, http):
        """角色拼错是**客户端错误**，不是服务端崩溃。"""
        admin = await _as(http, "管理员")
        resp = await http.post(
            "/api/users",
            json={"username": "拼错", "email": "w@example.com", "role": "Admin",
                  "password": "long-enough"},
            headers=admin,
        )
        assert resp.status_code == 422, resp.text

    async def test_admin_cannot_patch_a_higher_role(self, http):
        """不能改等级不低于自己的账号 —— 否则 admin 能改掉 sysadmin 的口令。"""
        admin = await _as(http, "管理员")
        resp = await http.patch(
            f"/api/users/{SYSADMIN}", json={"certs": ["光谱"]}, headers=admin
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["reason"] == "role_escalation"

    async def test_editing_your_own_profile_is_still_allowed(self, http):
        """闸门不能关过头：改**自己的**资料是正常操作。

        "等级不低于自己"这条规则里，自己的等级当然等于自己 ——
        所以必须显式放过，否则管理员连给自己补一条资质都做不到。
        危险的那件（改自己的角色）另有一条 400 挡着。
        """
        admin = await _as(http, "管理员")
        resp = await http.patch(
            f"/api/users/{ADMIN}", json={"certs": ["光谱"]}, headers=admin
        )
        assert resp.status_code == 200, resp.text

        # 但改自己的角色仍然是 400（会把自己锁死在外面）
        resp = await http.patch(
            f"/api/users/{ADMIN}", json={"role": ROLE_USER}, headers=admin
        )
        assert resp.status_code == 400, resp.text

    async def test_sysadmin_can_patch_an_admin(self, http):
        """反向路径要通 —— 否则 sysadmin 这个角色没有实际用处。"""
        sysadmin = await _as(http, "系统管理员")
        resp = await http.patch(
            f"/api/users/{ADMIN}", json={"certs": ["光谱", "色谱"]}, headers=sysadmin
        )
        assert resp.status_code == 200, resp.text

    async def test_admin_still_can_manage_normal_users(self, http):
        """闸门不能关过头：admin 管普通用户这条主路径必须照旧。"""
        admin = await _as(http, "管理员")
        created = await http.post(
            "/api/users",
            json={"username": "新生", "email": "n@example.com", "role": ROLE_USER,
                  "password": "long-enough"},
            headers=admin,
        )
        assert created.status_code == 201, created.text
        patched = await http.patch(
            f"/api/users/{created.json()['id']}", json={"certs": ["光谱"]}, headers=admin
        )
        assert patched.status_code == 200, patched.text


# ===========================================================================
# 五、种子规模可配（P1）：数量是配置，不是改代码
# ===========================================================================
@pytest.fixture
async def scratch_db(tmp_path):
    """一个独立的空库，用来跑 seed 的各种数量组合。"""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'scale.db').as_posix()}"
    async with isolated_database(url):
        await init_db()
        yield url
    await dispose_engine()


async def _count(model) -> int:
    async with session_scope() as session:
        return (await session.execute(
            select(func.count()).select_from(model)
        )).scalar() or 0


class TestSeedScale:
    async def test_default_seed_has_only_demo_accounts(self, scratch_db):
        """默认不变：只灌演示账号。
        现有 200+ 用例依赖"库里就这几个人"，默认开批量会把它们全冲掉。"""
        info = await seed(force=True)
        assert info["users"] == len(DEMO_USERS)
        assert info["synthetic_users"] == 0
        assert await _count(User) == len(DEMO_USERS)

    async def test_extra_users_are_appended_after_demo_accounts(self, scratch_db):
        """合成账号只能**追加**在演示账号之后 —— 演示账号的 id 是契约。"""
        info = await seed(force=True, extra_users=50)
        assert info["users"] == len(DEMO_USERS) + 50
        assert info["synthetic_users"] == 50
        async with session_scope() as session:
            rows = (await session.execute(
                select(User).order_by(User.id)
            )).scalars().all()
        assert [r.username for r in rows[:len(DEMO_USERS)]] == [
            u["username"] for u in DEMO_USERS
        ]
        assert all(r.role == ROLE_USER for r in rows[len(DEMO_USERS):])

    async def test_synthetic_users_are_reproducible(self, scratch_db):
        """同一数量两次 seed 必须得到同一批人。
        演示前重跑一次种子，结果变了就没法照着脚本念了。"""
        await seed(force=True, extra_users=30)
        async with session_scope() as session:
            first = [r.username for r in (
                await session.execute(select(User).order_by(User.id))
            ).scalars().all()]
        await seed(force=True, extra_users=30)
        async with session_scope() as session:
            second = [r.username for r in (
                await session.execute(select(User).order_by(User.id))
            ).scalars().all()]
        assert first == second

    async def test_synthetic_users_can_log_in_with_the_demo_password(self, scratch_db):
        """批量账号要**真的能登** —— 否则"500 个用户"只是个数字。"""
        from lagent.security import verify_password

        await seed(force=True, extra_users=20)
        async with session_scope() as session:
            rows = (await session.execute(
                select(User).where(User.id > len(DEMO_USERS))
            )).scalars().all()
        assert len(rows) == 20
        assert all(r.password_hash for r in rows)
        assert all(verify_password("demo@123", r.password_hash) for r in rows)

    async def test_pending_reservations_are_seeded_on_demand(self, scratch_db):
        """★ 此前 seed 一条 pending 都不造、也没有需审批的设备
        —— 管理员的审批面板打开是空的，"能审批"这个卖点看不出来。"""
        info = await seed(force=True, extra_users=10, pending=6)
        assert info["pending_reservations"] == 6
        assert info["approval_equipment"] >= 1, "至少要开一台需审批的设备"
        async with session_scope() as session:
            pending = (await session.execute(
                select(Reservation).where(Reservation.status == STATUS_PENDING)
            )).scalars().all()
            approval = (await session.execute(
                select(Equipment).where(Equipment.requires_approval.is_(True))
            )).scalars().all()
        assert len(pending) == 6
        assert approval, "一条待办都没有，审批面板必然是空的"

    async def test_pending_reservations_still_occupy_slots(self, scratch_db):
        """★ pending 是占坑的（ACTIVE_STATUSES 含 pending）。
        造演示数据**不能**绕过这条不变式，否则演示出来的系统比真的宽松。"""
        await seed(force=True, extra_users=10, pending=4)
        async with session_scope() as session:
            rows = (await session.execute(
                select(Reservation).where(Reservation.status == STATUS_PENDING)
            )).scalars().all()
            assert len(rows) == 4
            # 直接数占用格：``row.slots`` 是懒加载关系，在异步会话外用会炸
            ids = [row.id for row in rows]
            occupied = (await session.execute(
                select(ReservationSlot.reservation_id)
                .where(ReservationSlot.reservation_id.in_(ids))
            )).scalars().all()
        assert set(occupied) == set(ids), "有待审批预约没占坑，绕过了冲突判定"

    async def test_asking_for_pending_enables_approval_equipment(self, scratch_db):
        """要 pending 却没有需审批设备 = 配置矛盾。
        静默产出 0 条会让人以为开关坏了，所以自动开一台并写进返回值。"""
        async with session_scope() as session:
            before = (await session.execute(
                select(func.count()).select_from(Equipment)
                .where(Equipment.requires_approval.is_(True))
            )).scalar() or 0
        assert before == 0, "种子默认不该有待审批设备"
        info = await seed(force=True, pending=3)
        assert info["approval_equipment"] >= 1

    async def test_demo_ids_survive_a_large_seed(self, scratch_db):
        """★ 最关键的一条：用户量放大到几百，演示账号的 id 必须还是 1/2/3/4。"""
        await seed(force=True, extra_users=200, pending=20)
        async with session_scope() as session:
            rows = {r.username: r.id for r in (
                await session.execute(select(User).order_by(User.id))
            ).scalars().all()}
        assert rows["张伟"] == ZHANGWEI
        assert rows["李娜"] == LINA
        assert rows["管理员"] == ADMIN
        assert rows["系统管理员"] == SYSADMIN
