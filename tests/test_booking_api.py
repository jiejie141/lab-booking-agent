"""表单式下单接口 ``POST /api/reservations``（P0-3）。

这一组要证明的不是"接口能返回 201"，而是三件具体的事：

1. **不经过模型也能下单**，且走的是与对话入口**同一套**领域逻辑 ——
   资质、开放时间、粒度对齐、单次上限、区间不重叠，一个都不能少。
   这条如果不成立，这个接口就只是"绕过了校验的快速通道"。
2. **失败要分得清**：404（没这个设备/人）/ 422（参数或业务规则不合法）/
   403（越权）/ 409（时段被占）各有其位。一律 409 的话，前端只能
   弹一句"出错了"，P1-4 给下单结果做的七种分类就白做了。
3. **身份只认令牌**：``as_user_id`` 越权要被拒**且留痕** ——
   "想动别人的东西"是最该记下来的一类请求，拒绝也要记。

关于用例里的时间（这段值得读，它是这类测试最常见的坑）：

  * 日期由 ``free_day()`` 从"今天"推出（今天 +2 天），**不写死日期**。
    seed 在**明天**的荧光光谱仪上放了两笔预约（见 seed.py 末尾），
    固定日期会撞上它们，于是测试变成"验种子数据"而不是"验接口"。
  * 时间窗固定 10:00-12:00，它**同时**落在工作日（08:00-22:00）与
    周末（09:00-18:00）的开放时间内，所以用例在周几跑都一样。
    写死 14:00-16:00 也能过，但那是因为种子恰好用的是下午 ——
    这种"恰好"会在某次改种子数据时静默变成红。
"""

from __future__ import annotations

import datetime as dt

from lagent.api import _BOOKING_STATUS
from lagent.audit import ACTION_BOOK, OUTCOME_DENIED, OUTCOME_OK
from lagent.clock import now_local
from lagent.schemas import BookingOutcome

# 演示账号（seed.py）：张伟只有「光谱」，李娜资质齐全，管理员是 admin 角色
ZHANGWEI, LINA, ADMIN = 1, 2, 3

# 紫外可见分光光度计：光谱类、**不需培训** → 张伟也约得上，用作"成功路径"
UV = 2
# 高速离心机：离心类、需培训、单次上限 2 小时 → 用来验资质与上限
CENTRIFUGE = 6
# 紫外可见分光光度计在分析楼301；离心机在材料楼412，两间房互不干扰
SAME_LAB_OTHER_DEVICE = 3  # 高效液相色谱仪（也在分析楼301）

START = dt.time(10, 0)
END = dt.time(12, 0)


def free_day() -> dt.date:
    """一个种子数据肯定没占的日期。"""
    return now_local().date() + dt.timedelta(days=2)


def payload(equipment_id: int, **overrides) -> dict:
    """默认是一份**合法**的请求体；用例只覆盖自己要测的那一项。

    刻意让默认值全部合法：这样"因为写错了别的字段而失败"的用例会
    在断言上露出来（状态码不是预期值），而不是被当成被测行为。
    """
    body = {
        "equipment_id": equipment_id,
        "date": free_day().isoformat(),
        "start": START.strftime("%H:%M"),
        "end": END.strftime("%H:%M"),
        "purpose": "接口测试",
    }
    body.update(overrides)
    return body


def as_admin(audit_rows: list[dict], action: str) -> list[dict]:
    return [row for row in audit_rows if row["action"] == action]


async def rows_on_the_free_day(http, headers) -> list[dict]:
    """只看用例自己造的那一天。

    seed 在**明天**放了两条演示预约，其中一条就是李娜的 ——
    直接数 ``/api/reservations`` 的条数会把它们算进来，
    于是"被拒的请求没落库"这条断言变成在验种子数据。
    """
    listing = await http.get("/api/reservations", headers=headers)
    assert listing.status_code == 200, listing.text
    return [row for row in listing.json() if row["date"] == free_day().isoformat()]


# ===========================================================================
# 一、成功路径
# ===========================================================================
class TestHappyPath:
    async def test_booking_without_the_model_returns_201(self, http, as_user):
        """★ 核心：不经过模型，也能下成一单。"""
        headers = await as_user("李娜")
        resp = await http.post("/api/reservations", json=payload(UV), headers=headers)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["reservation"]["id"] > 0
        assert body["reservation"]["status"] == "confirmed"
        assert body["reservation"]["equipment_id"] == UV
        assert body["reservation"]["date"] == free_day().isoformat()

    async def test_the_new_booking_shows_up_in_the_query(self, http, as_user):
        """★ 201 只是"接口说成功了"，查得到才算真的落库。

        两者分开看是必要的：一个只写进会话没提交的实现，同样能返回 201。
        """
        headers = await as_user("李娜")
        created = await http.post("/api/reservations", json=payload(UV), headers=headers)
        assert created.status_code == 201, created.text

        listing = await http.get("/api/reservations", headers=headers)
        assert listing.status_code == 200, listing.text
        rows = [r for r in listing.json() if r["id"] == created.json()["reservation"]["id"]]
        assert len(rows) == 1
        assert rows[0]["start_time"].startswith("10:00")

    async def test_the_same_window_on_another_device_is_fine(self, http, as_user):
        """冲突是**按设备**算的，不是"这个人这个时段已经有了"。

        这条看着显然，但它是"区间不重叠"最容易写坏的一种形态：
        把唯一索引或复检条件写成只按用户去重，就会误伤跨设备预约。
        """
        headers = await as_user("李娜")
        first = await http.post("/api/reservations", json=payload(UV), headers=headers)
        second = await http.post(
            "/api/reservations",
            json=payload(SAME_LAB_OTHER_DEVICE, purpose="另一台设备"),
            headers=headers,
        )
        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        assert second.json()["reservation"]["equipment_id"] == SAME_LAB_OTHER_DEVICE

    async def test_a_successful_booking_is_audited(self, http, as_user):
        """下单必须留痕，且与对话入口用的是同一套审计动作。

        否则"这条预约是谁建的"在审计里查不到 —— 表单入口就成了盲区。
        """
        lina = await as_user("李娜")
        created = await http.post("/api/reservations", json=payload(UV), headers=lina)
        assert created.status_code == 201, created.text

        admin = await as_user("管理员")
        logs = await http.get("/api/audit", headers=admin)
        assert logs.status_code == 200, logs.text
        books = as_admin(logs.json(), ACTION_BOOK)
        assert any(
            row["actor_id"] == LINA and row["outcome"] == OUTCOME_OK for row in books
        )


# ===========================================================================
# 二、业务规则：★ 这组决定"这个接口是不是绕过了校验"
# ===========================================================================
class TestTheSameInvariantsAsTheChatEntry:
    async def test_unknown_equipment_is_404(self, http, as_user):
        """没这个设备 → 404（不是 400、不是 409）。"""
        headers = await as_user("李娜")
        resp = await http.post("/api/reservations", json=payload(99999), headers=headers)
        assert resp.status_code == 404, resp.text
        assert "不存在" in resp.json()["detail"]

    async def test_unaligned_time_is_422(self, http, as_user):
        """★ 粒度不对齐要被拒，而不是"向下取整凑合一下"。

        不对齐会让占用格算漏（``_ensure_aligned`` 的注释有完整推导），
        结果是"看起来约上了，其实 10:30 之后的时段没被保护"。
        """
        headers = await as_user("李娜")
        resp = await http.post(
            "/api/reservations",
            json=payload(UV, start="10:10"),
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert "30 分钟" in resp.json()["detail"]

    async def test_past_time_is_422(self, http, as_user):
        """已经过去的时间点不能约。"""
        headers = await as_user("李娜")
        yesterday = (now_local().date() - dt.timedelta(days=1)).isoformat()
        resp = await http.post(
            "/api/reservations", json=payload(UV, date=yesterday), headers=headers
        )
        assert resp.status_code == 422, resp.text
        assert "过去" in resp.json()["detail"]

    async def test_missing_certificate_is_422(self, http, as_user):
        """★ 资质约束必须仍然生效 —— 张伟只有「光谱」，约不了离心机。

        这是"表单入口是不是绕过了校验"的判据里最硬的一条：
        如果它放行，任何人都能约走自己没资格操作的设备。
        """
        headers = await as_user("张伟")
        resp = await http.post(
            "/api/reservations", json=payload(CENTRIFUGE), headers=headers
        )
        assert resp.status_code == 422, resp.text
        assert "离心" in resp.json()["detail"]

    async def test_exceeding_the_device_limit_is_422(self, http, as_user):
        """单次上限（离心机 2 小时）也要照旧生效。"""
        headers = await as_user("李娜")
        resp = await http.post(
            "/api/reservations", json=payload(CENTRIFUGE, end="13:00"), headers=headers
        )
        assert resp.status_code == 422, resp.text
        assert "单次最长" in resp.json()["detail"]

    async def test_outside_opening_hours_is_422(self, http, as_user):
        """开放时间之外不能约 —— 06:00 连工作日都没开门。"""
        headers = await as_user("李娜")
        resp = await http.post(
            "/api/reservations",
            json=payload(UV, start="06:00", end="07:00"),
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert "开放时间" in resp.json()["detail"]

    async def test_empty_or_reversed_window_is_422(self, http, as_user):
        """结束早于开始：这是参数错误，不是冲突。"""
        headers = await as_user("李娜")
        resp = await http.post(
            "/api/reservations", json=payload(UV, start="12:00", end="10:00"),
            headers=headers,
        )
        assert resp.status_code == 422, resp.text
        assert "晚于" in resp.json()["detail"]


# ===========================================================================
# 三、冲突
# ===========================================================================
class TestConflict:
    async def test_overlapping_window_is_409(self, http, as_user):
        """★ 时段被占 → 409，且要说出被谁占了（用户才能改时间）。"""
        headers = await as_user("李娜")
        first = await http.post("/api/reservations", json=payload(UV), headers=headers)
        assert first.status_code == 201, first.text

        second = await http.post(
            "/api/reservations",
            json=payload(UV, start="11:00", end="13:00"),
            headers=headers,
        )
        assert second.status_code == 409, second.text
        assert "占用" in second.json()["detail"]

    async def test_a_conflict_from_someone_else_is_still_409(self, http, as_user):
        """被**别人**占了同样是 409 —— 冲突与"是谁的"无关。

        这条与"跨设备不冲突"是一对：合起来才说明冲突判据是
        ``(设备, 日期, 时段)``，而不是别的什么。
        """
        lina = await as_user("李娜")
        assert (await http.post("/api/reservations", json=payload(UV), headers=lina)).status_code == 201

        zhangwei = await as_user("张伟")
        second = await http.post(
            "/api/reservations", json=payload(UV, purpose="张伟也想用"),
            headers=zhangwei,
        )
        assert second.status_code == 409, second.text

    async def test_the_rejected_attempt_leaves_nothing_behind(self, http, as_user):
        """被拒的请求不能留下半条预约 —— 否则"这坑没了"是假象。"""
        headers = await as_user("李娜")
        assert (await http.post("/api/reservations", json=payload(UV), headers=headers)).status_code == 201
        assert (
            await http.post(
                "/api/reservations", json=payload(UV, start="11:00", end="13:00"),
                headers=headers,
            )
        ).status_code == 409

        assert len(await rows_on_the_free_day(http, headers)) == 1, "被拒的那次不该落库"

    async def test_a_failed_booking_is_audited_as_denied(self, http, as_user):
        """失败也要留痕，否则"有人反复在撞某个时段"查不出来。"""
        headers = await as_user("李娜")
        assert (await http.post("/api/reservations", json=payload(UV), headers=headers)).status_code == 201
        assert (
            await http.post(
                "/api/reservations", json=payload(UV, start="11:00", end="13:00"),
                headers=headers,
            )
        ).status_code == 409

        admin = await as_user("管理员")
        logs = await http.get("/api/audit", headers=admin)
        denied = [
            row for row in as_admin(logs.json(), ACTION_BOOK)
            if row["outcome"] == OUTCOME_DENIED and row["actor_id"] == LINA
        ]
        assert denied, "失败的下单没有进审计"


# ===========================================================================
# 四、身份与越权
# ===========================================================================
class TestIdentity:
    async def test_anonymous_cannot_book(self, http):
        """没令牌不能下单 —— 预约是写操作，且会占用公共资源。"""
        resp = await http.post("/api/reservations", json=payload(UV))
        assert resp.status_code == 401

    async def test_a_plain_user_cannot_book_for_someone_else(self, http, as_user):
        """★ ``as_user_id`` 是管理员能力。普通用户传它 → 403，不是静默忽略。

        静默忽略（当成给自己下单）更"顺滑"，但调用方会以为代预约生效了，
        而实际约在了自己名下 —— 现场就是"老师说学生没给他约上"。
        """
        headers = await as_user("张伟")
        resp = await http.post(
            "/api/reservations", json=payload(UV, as_user_id=LINA), headers=headers
        )
        assert resp.status_code == 403, resp.text
        assert "管理员" in resp.json()["detail"]

    async def test_the_impersonation_attempt_is_audited(self, http, as_user):
        """★ 越权尝试必须留痕 —— 这是安全事件，不是普通的业务失败。"""
        headers = await as_user("张伟")
        assert (
            await http.post(
                "/api/reservations", json=payload(UV, as_user_id=LINA), headers=headers
            )
        ).status_code == 403

        admin = await as_user("管理员")
        logs = await http.get("/api/audit", headers=admin)
        attempts = [
            row for row in as_admin(logs.json(), ACTION_BOOK)
            if row["actor_id"] == ZHANGWEI and row["outcome"] == OUTCOME_DENIED
        ]
        assert attempts, "越权尝试没有进审计"
        assert "as_user_id" in attempts[0]["detail"]

    async def test_admin_can_book_for_someone_else(self, http, as_user):
        """管理员代预约：约成，且**落在被代那个人名下**。

        "落在谁名下"必须查得到 —— 只看 201 的话，一个把 ``as_user_id``
        忽略掉、约在管理员自己名下的实现同样返回 201。
        """
        admin = await as_user("管理员")
        created = await http.post(
            "/api/reservations",
            json=payload(UV, as_user_id=ZHANGWEI, purpose="代张伟预约"),
            headers=admin,
        )
        assert created.status_code == 201, created.text

        listing = await http.get(f"/api/reservations?user_id={ZHANGWEI}", headers=admin)
        assert listing.status_code == 200, listing.text
        mine = [r for r in listing.json() if r["purpose"] == "代张伟预约"]
        assert len(mine) == 1, "预约没有落在被代预约人的名下"

    async def test_admin_booking_for_a_missing_user_is_404(self, http, as_user):
        """代一个不存在的人下单 → 404，而不是"约在管理员名下"。"""
        admin = await as_user("管理员")
        resp = await http.post(
            "/api/reservations", json=payload(UV, as_user_id=99999), headers=admin
        )
        assert resp.status_code == 404, resp.text
        assert "不存在" in resp.json()["detail"]


# ===========================================================================
# 五、请求体与状态码映射
# ===========================================================================
class TestContract:
    async def test_unknown_field_is_rejected(self, http, as_user):
        """严格模式：字段名写错要**明确报错**，不能被静默忽略。

        预约是强约束业务，"我传了 equipmentId 它却约了别的设备"是最糟的失败。
        """
        headers = await as_user("李娜")
        resp = await http.post(
            "/api/reservations", json=payload(UV, equipmentId=UV), headers=headers
        )
        assert resp.status_code == 422, resp.text

    async def test_purpose_is_optional(self, http, as_user):
        """事由可以省略（默认值 ""），但超长要被拒。"""
        headers = await as_user("李娜")
        ok = await http.post("/api/reservations", json=payload(UV), headers=headers)
        assert ok.status_code == 201, ok.text

        too_long = await http.post(
            "/api/reservations", json=payload(UV, purpose="很" * 500), headers=headers
        )
        assert too_long.status_code == 422

    async def test_unclassified_failure_is_500_not_a_fake_conflict(self, http, as_user, monkeypatch):
        """★ 领域层漏了分类时，接口不能把它伪装成"冲突"。

        ``BookingOutcome.outcome_label`` 对未分类的失败兜底成 ``unknown``
        （而不是 ``ok``）就是为了让它露出来。如果 HTTP 层接着把它映射成
        409，前端就会提示用户"换个时间试试" —— 而真实情况是**我们的代码
        漏了分类**，重试一万次也没用。500 才会让监控响。
        """
        import lagent.api as api_module

        async def unclassified(**kwargs) -> BookingOutcome:
            return BookingOutcome(ok=False, message="未分类的失败（测试注入）")

        monkeypatch.setattr(api_module, "create_reservation", unclassified)
        headers = await as_user("李娜")
        resp = await http.post("/api/reservations", json=payload(UV), headers=headers)
        assert resp.status_code == 500, resp.text

    def test_unknown_is_not_left_to_the_default_branch(self):
        """映射表要显式覆盖 ``unknown``。

        依赖 ``.get(..., 409)`` 的默认值，等于把"未分类失败当冲突"这件事
        藏在一个没人会读的分支里。显式写出来，改的人才看得见。
        """
        assert _BOOKING_STATUS["unknown"] == 500
        # 业务上能分类的失败，一个都不能落进默认分支
        for label in ("not_found", "invalid", "forbidden", "state", "conflict", "contention"):
            assert label in _BOOKING_STATUS
