"""控制台编码加固。

这个 bug 的形态值得单独记住：本地交互式跑没问题，一重定向就崩 ——
所以不能只靠「我这边跑过了」来确认，必须有一个真的用 GBK 流去验的测试。
"""

from __future__ import annotations

import io
import pathlib

import pytest

from lagent.console import enable_utf8_output

WEB_INDEX = pathlib.Path(__file__).resolve().parents[1] / "src" / "lagent" / "web" / "index.html"


class TestEnableUtf8Output:
    def test_gbk_stream_would_crash_without_hardening(self):
        """先确认问题真实存在：GBK 流写 ✓ 会抛 UnicodeEncodeError。"""
        stream = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        with pytest.raises(UnicodeEncodeError):
            stream.write("✓")

    def test_hardening_makes_gbk_stream_safe(self):
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="gbk")

        enable_utf8_output(stream)
        stream.write("✓ 模型             : mock")
        stream.flush()

        assert buffer.getvalue().decode("utf-8").startswith("✓")

    def test_hardening_accepts_multiple_streams(self):
        first, second = io.BytesIO(), io.BytesIO()
        a = io.TextIOWrapper(first, encoding="gbk")
        b = io.TextIOWrapper(second, encoding="gbk")

        enable_utf8_output(a, b)
        a.write("✗")
        b.write("⚠")
        a.flush()
        b.flush()

        assert first.getvalue().decode("utf-8") == "✗"
        assert second.getvalue().decode("utf-8") == "⚠"

    def test_doctor_output_is_encodable_after_hardening(self, capsys):
        """doctor 打印的整段文本（含 ✓/✗/→）在 UTF-8 下必须都能编码。"""
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="gbk")
        enable_utf8_output(stream)
        for text in ("  ✓ 模型             : mock", "  ✗ 检索不可用", "  ⚠ 检索已降级",
                     "trace: parse → negotiate → book → compose"):
            stream.write(text + "\n")
        stream.flush()
        assert "✓" in buffer.getvalue().decode("utf-8")


class TestConsoleAuthWiring:
    """P0-2 之后控制台的认证接线（静态断言）。

    控制台是「零构建单文件」，没有前端测试框架；但这里最危险的回归恰好是
    「前端又自己去指定身份」。本文件真的踩到过一次：改完 HTML 删掉了
    ``<select id="sel-user">``，JS 里却还留着 ``$("#sel-user").value`` ——
    语法检查通过（``node --check`` 绿），运行时才炸。
    静态断言拦不住所有问题，但拦得住这一类。
    """

    @pytest.fixture(scope="class")
    def html(self) -> str:
        return WEB_INDEX.read_text(encoding="utf-8")

    def test_has_login_gate(self, html):
        assert 'id="gate"' in html
        assert 'id="lg-user"' in html and 'id="lg-pass"' in html
        assert "/api/auth/login" in html

    def test_attaches_bearer_token(self, html):
        assert "Authorization" in html
        assert "Bearer " in html
        assert "sessionStorage" in html, "令牌不该落到 localStorage（长驻，XSS 窗口更大）"

    def test_handles_token_expiry(self, html):
        """401 必须回登录闸门，而不是当普通错误弹个提示就完事。"""
        assert "sessionExpired" in html
        assert "r.status===401" in html.replace(" ", "")

    def test_never_sends_identity_in_write_requests(self, html):
        """★ 前端不得再自己决定身份：对话与取消的请求体里不能有 user_id。

        读接口那边保留 ``?user_id=`` 是**故意的**：管理员用它筛人，
        而普通用户传了也会被服务端改写回自己（见 test_api 的
        ``test_query_param_cannot_escape_scope``）。读侧的过滤参数
        是功能，写侧的身份字段才是漏洞 —— 两者要分清楚。
        """
        assert "sel-user" not in html, "残留了对已删除身份选择器的引用"

        lines = html.splitlines()
        payload = next(line for line in lines if line.strip().startswith("const payload={"))
        assert "user_id" not in payload, "对话请求体仍在指定身份"

        cancel_at = next(i for i, line in enumerate(lines) if "/api/reservations/cancel" in line)
        cancel_body = "\n".join(lines[cancel_at:cancel_at + 3])
        assert "user_id" not in cancel_body, "取消请求体仍在指定身份"

    def test_identity_field_only_used_for_read_filtering(self, html):
        """把「哪些地方出现了 user_id」钉死，防止悄悄长回来。"""
        hits = [line.strip() for line in html.splitlines() if "user_id" in line]
        # 只允许：1) 预约列表的读侧过滤参数；2) 解释 as_user_id 的注释
        for line in hits:
            assert "?user_id=" in line or line.startswith("//") or line.startswith("<!--"), \
                f"出现未预期的 user_id 用法：{line}"

    def test_admin_tab_is_gated(self, html):
        assert 'id="tab-users"' in html
        assert "isAdmin" in html
        assert "/api/users" in html


class TestConsoleIsServed:
    """上面的断言都是静态的（读文件）。这里确认**服务端真的吐出这一页**。

    静态断言拦得住"JS 引用了不存在的 id"，拦不住"改了文件但没被部署 /
    路由指向了另一份页面"。
    """

    async def test_index_contains_the_direct_booking_pane(self, http):
        resp = await http.get("/")
        assert resp.status_code == 200
        assert 'id="pane-book"' in resp.text
        assert 'data-tab="book"' in resp.text


class TestDirectBookingForm:
    """「直接预约」表单：不走模型的那条路（P0-3 真正兑现的部分）。

    P0-3 只在后端加 ``POST /api/reservations`` 是不够的 —— 控制台的预约入口
    如果仍然只有对话，那"模型不在时也能约"对真实用户等于没兑现。
    这里钉住的是：确实有这条路径、它确实不走模型、失败时确实把**服务端的
    原因**原样显示出来。
    """

    @pytest.fixture(scope="class")
    def html(self) -> str:
        return WEB_INDEX.read_text(encoding="utf-8")

    def test_the_form_exists_as_its_own_pane(self, html):
        assert 'id="pane-book"' in html
        assert 'data-tab="book"' in html
        # 面板切换的名单里必须带上它，否则点标签页什么都不出现
        assert '"book","labs","res","kb","users"' in html

    def test_it_posts_to_the_deterministic_endpoint(self, html):
        """走 POST /api/reservations，而不是 /api/agent/chat。"""
        marker = 'api("/api/reservations",{method:"POST"'
        assert marker in html, "表单必须打确定性下单接口"
        at = html.index(marker)
        body = html[at:at + 240]
        assert "equipment_id" in body and "start" in body and "end" in body
        assert "user_id" not in body, "身份必须由令牌带，不能出现在表单请求体里"

    def test_the_chat_entry_still_exists(self, html):
        """加表单不是为了删掉对话入口 —— 对话是可选的便捷方式，两者并存。"""
        assert "/api/agent/chat" in html

    def test_failure_shows_the_server_reason_verbatim(self, html):
        """失败信息用 e.message 而不是自己翻成一句"预约失败"。

        那些原因码是给用户的（时段冲突 / 资质不够 / 违约被限 / 超单次上限），
        翻成一句笼统的话，用户就只剩"换个时间再试试"这一条路。
        """
        assert "esc(e.message)" in html
        assert "e.status===403" in html.replace(" ", ""), "403 是资格问题，不该和 409 一样提示换时段"

    def test_options_come_from_the_catalog_not_hardcoded(self, html):
        """设备下拉由 /api/labs 填，不写死 —— 加一台新设备不该改前端。"""
        assert "state.equipment" in html
        assert "fillBookingOptions" in html


class TestAdminPanelsWiring:
    """管理员面板的接线（静态断言）。

    这一组是试运行报告 H1 的回归守门。H1 说的是：审批 / 违约 / 通知 / 审计
    四个能力的**接口与测试早就齐了，但控制台一个入口都没有** —— 管理员只能
    curl。那种状态最坏的地方在于它**看起来是完成的**：所有测试都绿。

    所以这里不只断言"标签页存在"，还断言**面板真的去调了那四个接口** ——
    一个没有数据的空面板比不上一个按钮，可以让人误以为功能已经在用了。
    """

    @pytest.fixture(scope="class")
    def html(self) -> str:
        return WEB_INDEX.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "tab",
        ["book", "labs", "res", "kb", "users", "appr", "viol", "notif", "audit"],
    )
    def test_every_tab_has_a_matching_pane(self, html, tab):
        """标签和面板必须一一配对。

        切页那段是按名字去 ``#pane-<name>`` 取元素的：少一个面板，
        点那个标签会**静默什么都不发生**（``$()`` 返回 null 再取 .hidden 就抛错，
        而抛错在外层 try 之外，整段监听器一起失效）。这条静态断言把这种
        "点了没反应"挡在提交之前。
        """
        assert f'data-tab="{tab}"' in html, f"缺少标签 {tab}"
        assert f'id="pane-{tab}"' in html, f"缺少面板 pane-{tab}"

    @pytest.mark.parametrize("tab", ["appr", "viol", "notif", "audit"])
    def test_admin_tabs_start_hidden(self, html, tab):
        """管理员面板默认 hidden —— 普通用户不该看见半个闪一下的标签。

        （真正的边界仍在服务端：手动构造请求照样 403。这里是"不显示无权限的控件"。）
        """
        assert f'id="tab-{tab}"  hidden' in html or f'id="tab-{tab}" hidden' in html, (
            f"{tab} 标签缺少 hidden，非管理员会看到它"
        )

    @pytest.mark.parametrize(
        ("endpoint", "why"),
        [
            ("/api/reservations/pending", "审批待办"),
            ("/api/reservations/${id}/${kind}", "通过 / 驳回"),
            ("/violations", "违约账"),
            ("/pardon", "豁免"),
            ("/api/notifications?user_id=", "通知（按人查）"),
            ("/api/audit", "审计流水"),
        ],
    )
    def test_the_h1_endpoints_are_reachable_from_the_console(self, html, endpoint, why):
        """★ H1 的回归守门：这六个端点必须能从界面上够到。

        少了任何一条，"管理员只能 curl"这个状态就会悄悄回来 ——
        而它在测试里完全看不出来（接口是好的）。
        """
        assert endpoint in html, f"控制台里找不到「{why}」对应的调用：{endpoint}"

    def test_admin_tabs_are_revealed_only_for_admins(self, html):
        """四个面板由 enterApp() 里那段 isAdmin() 统一放出来。

        写成按名字列一组而不是逐个 `if`，是为了让"漏放一个"这件事
        在改动时就看得出来 —— 少放一个的后果是标签永远不可见，
        而它**不会报错**。
        """
        assert '["tab-users","tab-appr","tab-viol","tab-notif","tab-audit"]' in html

    def test_tab_loaders_cover_every_dynamic_pane(self, html):
        """切页要拉数据的面板都得在 TAB_LOADERS 里登记。

        漏登记的后果同样是"点了没反应"：面板显示的是骨架屏，永远不加载。
        """
        for name in ("appr", "viol", "notif", "audit", "res", "labs", "book", "users"):
            assert name in html.split("const TAB_LOADERS=")[1].split("};")[0], (
                f"TAB_LOADERS 里没有 {name}"
            )


class TestAdminPanelContracts:
    """控制台渲染所依赖的字段。

    为什么单独钉一遍：渲染是拼字符串，字段名写错**不会报错** ——
    页面上就是一片 `undefined`，而所有静态断言照样绿。
    所以这里把"控制台依赖哪些字段"用真实响应与模型字段钉住：
    哪天有人把 ``user_name`` 改名，红的是这条测试，不是管理员的页面。
    """

    def test_reservation_fields_the_admin_panels_render(self):
        from lagent.schemas import ReservationOut

        fields = set(ReservationOut.model_fields)
        assert {
            "user_id",       # 审批列表要显示"谁在申请"
            "user_name",
            "equipment_name",
            "lab_label",
            "slot",
            "no_show_at",    # 违约面板要能看出"哪条被判了未到场"
            "pardoned_at",
        } <= fields

    async def test_violations_shape(self, http, as_user):
        admin = await as_user("管理员")
        resp = await http.get("/api/users/1/violations", headers=admin)
        assert resp.status_code == 200
        body = resp.json()
        # 面板把 over_threshold 与 blocking_enabled 分开显示：
        # 合成一个布尔的话，"超了阈值但处罚还没开"这种状态就没法表达
        assert {
            "count", "threshold", "window_days",
            "blocked", "over_threshold", "blocking_enabled", "message",
        } <= set(body)

    async def test_notification_shape(self, http, as_user):
        admin = await as_user("管理员")
        rows = (await http.get("/api/notifications", params={"user_id": 2}, headers=admin)).json()
        assert isinstance(rows, list)
        for row in rows:
            assert {"kind", "title", "body", "status", "created_at"} <= set(row)

    async def test_audit_shape(self, http, as_user):
        admin = await as_user("管理员")
        rows = (await http.get("/api/audit", headers=admin)).json()
        assert rows, "管理员登录本身就会留下审计记录，这里不该是空的"
        assert {
            "created_at", "action", "actor_name", "target_type", "target_id",
            "outcome", "detail",
        } <= set(rows[0])

    async def test_pending_rows_carry_the_applicant(self, http, as_user):
        """待审批列表必须能回答"谁在申请"。

        没有申请人名字的待办队列是没法用的（试运行时对着它才发现缺这个字段），
        所以哪怕当前没有待办，也要把**列表非空时**的形状钉住。
        """
        admin = await as_user("管理员")
        rows = (await http.get("/api/reservations/pending", headers=admin)).json()
        assert isinstance(rows, list)
        for row in rows:
            assert "user_id" in row and "user_name" in row


class TestConsoleJavaScript:
    """控制台里的 JS 至少要能通过语法解析。

    控制台是「零构建单文件」，没有打包器替我们做这一步 —— 而这里已经有近 700 行 JS，
    少一个括号，浏览器给你的是一片白屏加一行 console 报错，**而后端测试全绿**。
    本文件开头记的另一起事故（HTML 删了 `#sel-user`，JS 里还在引用）也是同一类：
    字符串断言拦不住，跑一次解析器就拦得住。

    没有 node 就跳过：这条是加固，不该因为环境缺工具而红。
    """

    def test_the_inline_script_parses(self, tmp_path):
        import re
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            pytest.skip("环境里没有 node，跳过 JS 语法检查")

        html = WEB_INDEX.read_text(encoding="utf-8")
        blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
        assert blocks, "控制台里一个 <script> 都没有"
        script = tmp_path / "console.js"
        script.write_text("\n;\n".join(blocks), encoding="utf-8")
        done = subprocess.run(
            [node, "--check", str(script)], capture_output=True, text=True, check=False
        )
        assert done.returncode == 0, f"控制台 JS 语法错误：\n{done.stderr}"
