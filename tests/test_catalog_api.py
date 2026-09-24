"""后台维护接口（P0-4）：实验室 / 设备 / 用户的增与改。

这一组要证明的是"改数据不必改代码重新 seed"，具体落在四件事上：

1. **只有管理员能改**，且每次改动都进审计（``admin.write``）；
2. **PATCH 语义是真的**：只改传进来的字段，其余一动不动；
3. **维护动作真的改变业务行为** —— 把设备置为 maintenance 之后它必须
   约不上；去掉某人的资质之后他必须约不上**也进不去门**；
4. 冲突（资产编号重复 / 用户名重复）给 409 而不是 500。

★ 第 3 条里的"进不去门"是最容易漏的一个坑：

``User.certs``（预约设备时看它）与 ``CertGrant``（进门时看它）是两套来源。
只改裸列表的话，**移除资质不会让门禁失效** —— 因为 ``cert_states_for``
在有授权记录时以记录为准，那条记录还好好地躺着没被撤销。
于是出现"管理员取消了某人的离心机资格，而他照样刷卡进得去"。
本文件里 ``test_revoking_a_cert_also_closes_the_door`` 专门钉这一条。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from lagent.audit import ACTION_ADMIN_WRITE
from lagent.clock import now_local
from lagent.db import session_scope
from lagent.domain.access import cert_states_for
from lagent.models import (
    EQUIPMENT_MAINTENANCE,
    EQUIPMENT_NORMAL,
    EQUIPMENT_SCRAPPED,
    LAB_BASIC_CERT,
    ROLE_ADMIN,
    ROLE_USER,
    CertGrant,
    Equipment,
    Laboratory,
    User,
)

ZHANGWEI, LINA, ADMIN = 1, 2, 3
UV = 2          # 紫外可见分光光度计（不需培训）
CENTRIFUGE = 6  # 高速离心机（需「离心」资质）
LAB_ANALYSIS = 1

START = dt.time(10, 0)
END = dt.time(12, 0)


def free_day() -> dt.date:
    return now_local().date() + dt.timedelta(days=2)


def new_lab(**overrides) -> dict:
    body = {
        "building": "测试楼",
        "floor": 5,
        "room": "501",
        "capacity": 3,
        "open_hours": {"weekday": ["08:00", "20:00"]},
        "note": "由测试创建",
    }
    body.update(overrides)
    return body


def new_equipment(**overrides) -> dict:
    body = {
        "lab_id": LAB_ANALYSIS,
        "name": "测试用离心机",
        "model": "T-1",
        "code": "TEST-0001",
        "category": "离心",
        "max_hours": 2,
        "requires_training": True,
    }
    body.update(overrides)
    return body


def booking_body(equipment_id: int, **overrides) -> dict:
    body = {
        "equipment_id": equipment_id,
        "date": free_day().isoformat(),
        "start": START.strftime("%H:%M"),
        "end": END.strftime("%H:%M"),
        "purpose": "维护后下单测试",
    }
    body.update(overrides)
    return body


async def admin_writes(http, headers) -> list[dict]:
    resp = await http.get("/api/audit", headers=headers)
    assert resp.status_code == 200, resp.text
    return [row for row in resp.json() if row["action"] == ACTION_ADMIN_WRITE]


async def grants_of(user_id: int, category: str) -> list[CertGrant]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(CertGrant).where(
                    CertGrant.user_id == user_id, CertGrant.category == category
                )
            )
        ).scalars().all()
        return list(rows)


async def cert_state(user_id: int, category: str) -> str:
    async with session_scope() as session:
        states = await cert_states_for(
            session,
            user_id=user_id,
            categories=[category],
            today=now_local().date(),
        )
    return states[category].state


# ===========================================================================
# 一、谁能改
# ===========================================================================
class TestWhoMayMaintain:
    async def test_anonymous_cannot_create_a_lab(self, http):
        assert (await http.post("/api/labs", json=new_lab())).status_code == 401

    async def test_a_plain_user_cannot_create_a_lab(self, http, as_user):
        headers = await as_user("李娜")
        resp = await http.post("/api/labs", json=new_lab(), headers=headers)
        assert resp.status_code == 403

    async def test_a_plain_user_cannot_create_equipment(self, http, as_user):
        headers = await as_user("李娜")
        resp = await http.post("/api/equipment", json=new_equipment(), headers=headers)
        assert resp.status_code == 403

    async def test_a_plain_user_cannot_create_users(self, http, as_user):
        headers = await as_user("张伟")
        resp = await http.post(
            "/api/users",
            json={"username": "hacker", "email": "h@example.com",
                  "password": "long-enough"},
            headers=headers,
        )
        assert resp.status_code == 403

    async def test_a_plain_user_cannot_deactivate_anyone(self, http, as_user):
        headers = await as_user("张伟")
        resp = await http.post(f"/api/users/{ADMIN}/deactivate", headers=headers)
        assert resp.status_code == 403

    async def test_there_is_no_delete_route(self, http, as_user):
        """★ 刻意不提供删除：删掉实体会让历史记录指向不存在的对象。

        这条用例的作用是把这个决定**钉住** —— 哪天有人顺手加了个 DELETE，
        它会红，然后才有人去读 domain/catalog.py 里那段理由。
        """
        headers = await as_user("管理员")
        for path in (f"/api/labs/{LAB_ANALYSIS}", f"/api/equipment/{UV}",
                     f"/api/users/{ZHANGWEI}"):
            resp = await http.request("DELETE", path, headers=headers)
            assert resp.status_code in (404, 405), f"{path} 不该可删：{resp.status_code}"


# ===========================================================================
# 二、实验室
# ===========================================================================
class TestLabMaintenance:
    async def test_create_then_it_shows_up_in_the_directory(self, http, as_user):
        headers = await as_user("管理员")
        created = await http.post("/api/labs", json=new_lab(), headers=headers)
        assert created.status_code == 201, created.text
        payload = created.json()
        assert payload["id"] > 0
        assert payload["label"] == "测试楼5楼501"

        listing = await http.get("/api/labs", headers=headers)
        assert any(row["id"] == payload["id"] for row in listing.json())

    async def test_patch_changes_only_what_was_sent(self, http, as_user):
        """★ PATCH 语义：只改传进来的字段。

        这条看着基础，却是"更新接口"最常写坏的地方 —— 用整个模型覆盖一遍，
        于是"只想改备注"会把开放时间抹成默认，而这间实验室从此全天不开放。
        """
        headers = await as_user("管理员")
        created = await http.post("/api/labs", json=new_lab(), headers=headers)
        lab_id = created.json()["id"]

        patched = await http.patch(
            f"/api/labs/{lab_id}", json={"capacity": 9}, headers=headers
        )
        assert patched.status_code == 200, patched.text
        body = patched.json()
        assert body["capacity"] == 9
        # 其余字段原样
        assert body["building"] == "测试楼"
        assert body["open_hours"] == {"weekday": ["08:00", "20:00"]}
        assert body["note"] == "由测试创建"

    async def test_an_explicit_null_means_do_not_touch_it(self, http, as_user):
        """传 null 表示"不改"，而不是"清空"。

        把它当清空的话，前端一次"我只想改备注"的请求（其余字段序列化成 null）
        就会把开放时间抹掉。
        """
        headers = await as_user("管理员")
        lab_id = (await http.post("/api/labs", json=new_lab(), headers=headers)).json()["id"]
        patched = await http.patch(
            f"/api/labs/{lab_id}", json={"note": "改过了", "capacity": None},
            headers=headers,
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["capacity"] == 3
        assert patched.json()["note"] == "改过了"

    async def test_missing_lab_is_404(self, http, as_user):
        headers = await as_user("管理员")
        resp = await http.patch("/api/labs/99999", json={"note": "x"}, headers=headers)
        assert resp.status_code == 404

    async def test_open_hours_must_be_usable(self, http, as_user):
        """开放时间写错要在**写入时**报错。

        放到读侧兜底也能扛（不会 500），但那之后用户看到的是
        "该实验室当天不开放" —— 一个能跑、但答非所问的系统最难修。
        """
        headers = await as_user("管理员")
        cases = {
            "只有一个时刻": {"weekday": ["08:00"]},
            "结束早于开始": {"weekday": ["20:00", "08:00"]},
            "未知键": {"holiday": ["08:00", "20:00"]},
            "时刻不是 HH:MM": {"weekday": ["早上八点", "20:00"]},
        }
        for name, open_hours in cases.items():
            resp = await http.post(
                "/api/labs", json=new_lab(open_hours=open_hours), headers=headers
            )
            assert resp.status_code == 422, f"{name} 应该被拒，实际 {resp.status_code}"

    async def test_maintenance_is_audited(self, http, as_user):
        headers = await as_user("管理员")
        created = await http.post("/api/labs", json=new_lab(), headers=headers)
        assert created.status_code == 201, created.text
        rows = await admin_writes(http, headers)
        assert any(row["target_type"] == "lab" for row in rows), "维护动作没有进审计"

    async def test_unknown_field_is_rejected(self, http, as_user):
        headers = await as_user("管理员")
        resp = await http.post("/api/labs", json=new_lab(capacityy=3), headers=headers)
        assert resp.status_code == 422


# ===========================================================================
# 三、设备
# ===========================================================================
class TestEquipmentMaintenance:
    async def test_create_then_it_is_bookable(self, http, as_user):
        """★ 端到端：后台新增的设备要**真的能被约上**。

        只验"返回 201"是不够的 —— 一个漏了 category 的创建实现同样返回 201，
        而那台设备在资质校验里会永远约不上。
        """
        admin = await as_user("管理员")
        created = await http.post(
            "/api/equipment",
            json=new_equipment(code="TEST-E2E", requires_training=False),
            headers=admin,
        )
        assert created.status_code == 201, created.text
        equipment_id = created.json()["id"]

        lina = await as_user("李娜")
        booked = await http.post(
            "/api/reservations", json=booking_body(equipment_id), headers=lina
        )
        assert booked.status_code == 201, booked.text

    async def test_duplicate_asset_code_is_409(self, http, as_user):
        """资产编号重复 → 409（冲突），不是 500。

        不翻译数据库错误的话，管理员看到的是
        ``UNIQUE constraint failed: equipment.code`` 这种行话。
        """
        headers = await as_user("管理员")
        first = await http.post("/api/equipment", json=new_equipment(), headers=headers)
        assert first.status_code == 201, first.text
        second = await http.post("/api/equipment", json=new_equipment(), headers=headers)
        assert second.status_code == 409, second.text
        assert "冲突" in second.json()["detail"]

    async def test_equipment_in_a_missing_lab_is_rejected(self, http, as_user):
        headers = await as_user("管理员")
        resp = await http.post(
            "/api/equipment", json=new_equipment(lab_id=99999), headers=headers
        )
        assert resp.status_code == 409, resp.text
        assert "不存在" in resp.json()["detail"]

    async def test_taking_a_device_offline_blocks_new_bookings(self, http, as_user):
        """★ 维护动作必须真的改变业务行为：置为 maintenance 之后约不上。"""
        admin = await as_user("管理员")
        lina = await as_user("李娜")

        before = await http.post("/api/reservations", json=booking_body(UV), headers=lina)
        assert before.status_code == 201, before.text

        patched = await http.patch(
            f"/api/equipment/{UV}", json={"status": EQUIPMENT_MAINTENANCE}, headers=admin
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["status"] == EQUIPMENT_MAINTENANCE
        # 已有预约还在 —— 设备下线不会自动取消，这一点要如实报出来
        assert patched.json()["active_reservations"] >= 1
        assert "warning" in patched.json()

        after = await http.post(
            "/api/reservations", json=booking_body(UV, purpose="停机后"), headers=lina
        )
        assert after.status_code == 422, after.text
        assert "不可预约" in after.json()["detail"]

    async def test_bringing_it_back_restores_booking(self, http, as_user):
        """恢复成 normal 之后又能约 —— 维护动作必须是可逆的。"""
        admin = await as_user("管理员")
        assert (
            await http.patch(
                f"/api/equipment/{UV}", json={"status": EQUIPMENT_MAINTENANCE},
                headers=admin,
            )
        ).status_code == 200
        assert (
            await http.patch(
                f"/api/equipment/{UV}", json={"status": EQUIPMENT_NORMAL}, headers=admin
            )
        ).status_code == 200

        lina = await as_user("李娜")
        resp = await http.post("/api/reservations", json=booking_body(UV), headers=lina)
        assert resp.status_code == 201, resp.text

    async def test_missing_equipment_is_404(self, http, as_user):
        headers = await as_user("管理员")
        resp = await http.patch(
            "/api/equipment/99999", json={"status": EQUIPMENT_MAINTENANCE},
            headers=headers,
        )
        assert resp.status_code == 404

    async def test_a_bad_status_value_is_422(self, http, as_user):
        """状态拼错要被拒 —— 写进库里就是"既不是 normal 也不是 maintenance"，
        设备从此被永久拦下而界面上一切正常。"""
        headers = await as_user("管理员")
        resp = await http.patch(
            f"/api/equipment/{UV}", json={"status": "Normal"}, headers=headers
        )
        assert resp.status_code == 422, resp.text

    async def test_the_directory_keeps_lab_id(self, http, as_user):
        """列表里要带 lab_id —— 后台改设备所属房间需要它。"""
        headers = await as_user("管理员")
        created = await http.post(
            "/api/equipment", json=new_equipment(code="TEST-LABID"), headers=headers
        )
        assert created.status_code == 201, created.text
        assert created.json()["lab_id"] == LAB_ANALYSIS
        rows = (await http.get("/api/labs", headers=headers)).json()
        flat = [item for row in rows for item in row["equipment"]]
        assert any(item["code"] == "TEST-LABID" for item in flat)


# ===========================================================================
# 四、用户：★ 资质那两条是本文件的核心
# ===========================================================================
class TestUserMaintenance:
    async def test_create_then_the_new_account_can_log_in(self, http, as_user):
        """建的账号要**真的能登** —— 而不是"存在但没人能进去"。"""
        admin = await as_user("管理员")
        created = await http.post(
            "/api/users",
            json={
                "username": "新生小王",
                "email": "wang@example.com",
                "role": "user",
                "certs": [LAB_BASIC_CERT, "光谱"],
                "password": "wang@12345",
            },
            headers=admin,
        )
        assert created.status_code == 201, created.text
        assert created.json()["certs"] == [LAB_BASIC_CERT, "光谱"]
        # UserOut 刻意不含 email —— 身份信息按需最小化
        assert "email" not in created.json()

        resp = await http.post(
            "/api/auth/login", json={"username": "新生小王", "password": "wang@12345"}
        )
        assert resp.status_code == 200, resp.text

    async def test_duplicate_username_is_409(self, http, as_user):
        admin = await as_user("管理员")
        body = {"username": "李娜", "email": "other@example.com", "password": "long-enough"}
        resp = await http.post("/api/users", json=body, headers=admin)
        assert resp.status_code == 409, resp.text

    async def test_a_weak_password_is_rejected(self, http, as_user):
        """初始口令太短要在建号时就拒，而不是等它被爆破。"""
        admin = await as_user("管理员")
        resp = await http.post(
            "/api/users",
            json={"username": "短口令", "email": "s@example.com", "password": "123"},
            headers=admin,
        )
        assert resp.status_code == 422

    async def test_granting_a_cert_also_creates_a_grant_record(self, http, as_user):
        """★ 后台授予资质要同时写进 ``cert_grants``。

        只改 ``User.certs`` 裸列表的话，这份资质**没有有效期** ——
        与"资质必须看有效期"这条贯穿全系统的立场直接冲突。
        """
        admin = await as_user("管理员")
        resp = await http.patch(
            f"/api/users/{ZHANGWEI}", json={"certs": ["光谱", "焊接"]}, headers=admin
        )
        assert resp.status_code == 200, resp.text
        rows = await grants_of(ZHANGWEI, "焊接")
        assert len(rows) == 1
        assert rows[0].expires_at > now_local().date(), "授权必须有到期日"
        assert rows[0].revoked_at is None
        assert await cert_state(ZHANGWEI, "焊接") == "ok"

    async def test_revoking_a_cert_also_closes_the_door(self, http, as_user):
        """★★ 本文件最重要的一条：取消资质后，**门禁也必须失效**。

        ``cert_states_for`` 在有授权记录时以记录为准、忽略 ``User.certs``。
        所以"只把类别从裸列表里删掉"根本不管用 —— 那条授权还好好的，
         门禁照样放行。现场后果就是：管理员取消了某人的离心机资格，
        而他刷卡照进。
        """
        admin = await as_user("管理员")
        assert await cert_state(LINA, "离心") == "ok"

        resp = await http.patch(
            f"/api/users/{LINA}", json={"certs": ["光谱", "色谱", "细胞培养"]},
            headers=admin,
        )
        assert resp.status_code == 200, resp.text

        assert await cert_state(LINA, "离心") == "revoked", "取消资质后门禁仍然放行"
        # 预约侧也必须同步失效：两套来源不能只改一边
        lina = await as_user("李娜")
        booked = await http.post(
            "/api/reservations", json=booking_body(CENTRIFUGE), headers=lina
        )
        assert booked.status_code == 422, booked.text

    async def test_revocation_is_kept_not_deleted(self, http, as_user):
        """撤销而不是删行：要能回答"他曾经有过、什么时候被撤的"。"""
        admin = await as_user("管理员")
        assert (
            await http.patch(
                f"/api/users/{LINA}", json={"certs": ["光谱"]}, headers=admin
            )
        ).status_code == 200
        rows = await grants_of(LINA, "离心")
        assert rows, "授权记录不该被删掉"
        assert all(row.revoked_at is not None for row in rows)

    async def test_regranting_after_revocation_works(self, http, as_user):
        """撤了再授要能恢复 —— 且走"更晚的 granted_at"（复训换证）。"""
        admin = await as_user("管理员")
        assert (
            await http.patch(f"/api/users/{LINA}", json={"certs": ["光谱"]}, headers=admin)
        ).status_code == 200
        assert await cert_state(LINA, "离心") == "revoked"

        resp = await http.patch(
            f"/api/users/{LINA}", json={"certs": ["光谱", "离心"]}, headers=admin
        )
        assert resp.status_code == 200, resp.text
        assert await cert_state(LINA, "离心") == "ok"

    async def test_deactivated_account_cannot_log_in(self, http, as_user):
        admin = await as_user("管理员")
        created = await http.post(
            "/api/users",
            json={"username": "待停用", "email": "d@example.com",
                  "password": "long-enough"},
            headers=admin,
        )
        assert created.status_code == 201, created.text
        user_id = created.json()["id"]

        assert (
            await http.post(
                "/api/auth/login",
                json={"username": "待停用", "password": "long-enough"},
            )
        ).status_code == 200

        resp = await http.post(f"/api/users/{user_id}/deactivate", headers=admin)
        assert resp.status_code == 200, resp.text
        assert resp.json()["active"] is False

        # 空口令哈希 → verify_password 一律失败（fail-closed）
        assert (
            await http.post(
                "/api/auth/login",
                json={"username": "待停用", "password": "long-enough"},
            )
        ).status_code == 401

    async def test_deactivation_keeps_the_history(self, http, as_user):
        """停用不能顺手删掉他的预约 —— 历史记录必须还在。"""
        admin = await as_user("管理员")
        lina = await as_user("李娜")
        created = await http.post("/api/reservations", json=booking_body(UV), headers=lina)
        assert created.status_code == 201, created.text

        assert (await http.post(f"/api/users/{LINA}/deactivate", headers=admin)).status_code == 200
        listing = await http.get(f"/api/reservations?user_id={LINA}", headers=admin)
        assert listing.status_code == 200, listing.text
        assert any(
            row["date"] == free_day().isoformat() for row in listing.json()
        ), "停用把历史预约弄丢了"

    async def test_cannot_deactivate_yourself(self, http, as_user):
        """不能把自己锁在外面 —— 这条是操作类接口最基础的自保。"""
        admin = await as_user("管理员")
        resp = await http.post(f"/api/users/{ADMIN}/deactivate", headers=admin)
        assert resp.status_code == 400, resp.text

    async def test_another_admin_can_be_deactivated(self, http, as_user):
        """停用**别人**是允许的（哪怕他是管理员）——

        真正会把系统锁死的只有"把自己停用掉"，那条单独有保护。
        而"停用不改角色、也不删行"，所以停用第二个管理员之后
        仍然统计得到 1 个管理员，不会降到 0。
        """
        admin = await as_user("管理员")
        second = await http.post(
            "/api/users",
            json={"username": "副管理员", "email": "v@example.com", "role": ROLE_ADMIN,
                  "password": "long-enough"},
            headers=admin,
        )
        assert second.status_code == 201, second.text
        resp = await http.post(
            f"/api/users/{second.json()['id']}/deactivate", headers=admin
        )
        assert resp.status_code == 200, resp.text

    async def test_cannot_change_your_own_role(self, http, as_user):
        """不能改自己的角色：降成普通用户之后就没人能改回来了。"""
        admin = await as_user("管理员")
        resp = await http.patch(
            f"/api/users/{ADMIN}", json={"role": "user"}, headers=admin
        )
        assert resp.status_code == 400, resp.text

    async def test_missing_user_is_404(self, http, as_user):
        admin = await as_user("管理员")
        resp = await http.patch("/api/users/99999", json={"role": "user"}, headers=admin)
        assert resp.status_code == 404


# ===========================================================================
# 五、取值域的一致性（防止"常量改了、类型没改"这类静默漂移）
# ===========================================================================
class TestValueDomainStaysConsistent:
    def test_equipment_status_constants_match_the_literal(self):
        from typing import get_args

        from lagent.models import (
            EQUIPMENT_MAINTENANCE,
            EQUIPMENT_NORMAL,
            EQUIPMENT_SCRAPPED,
            EquipmentStatus,
        )

        allowed = set(get_args(EquipmentStatus))
        assert {EQUIPMENT_NORMAL, EQUIPMENT_MAINTENANCE, EQUIPMENT_SCRAPPED} == allowed

    def test_role_constants_match_the_literal(self):
        from typing import get_args

        from lagent.models import ROLE_ADMIN, ROLE_SYSADMIN, ROLE_USER, UserRole

        assert {ROLE_USER, ROLE_ADMIN, ROLE_SYSADMIN} == set(get_args(UserRole))

    def test_the_seeded_data_uses_known_statuses(self):
        """种子数据也要在取值域内 —— 它是"新库第一次长什么样"的来源。"""
        from lagent.seed import EQUIPMENT, LABS

        assert EQUIPMENT and LABS
        for row in EQUIPMENT:
            # seed 里的设备不写 status，走模型默认 normal
            assert "status" not in row or row["status"] in (
                EQUIPMENT_NORMAL, EQUIPMENT_MAINTENANCE, EQUIPMENT_SCRAPPED
            )

    async def test_the_model_defaults_are_valid(self, isolated_db):
        async with session_scope() as session:
            session.add(Laboratory(building="楼", floor=1, room="1"))
            await session.flush()
            lab = await session.scalar(
                select(Laboratory).where(Laboratory.building == "楼")
            )
            assert lab is not None
            session.add(Equipment(
                lab_id=lab.id, name="默认设备", code="D-1", category="光谱",
            ))
            await session.flush()
            item = await session.scalar(select(Equipment).where(Equipment.code == "D-1"))
            assert item is not None
            assert item.status == EQUIPMENT_NORMAL
            user = User(username="默认角色", email="r@example.com")
            session.add(user)
            await session.flush()
        assert user.role == ROLE_USER
