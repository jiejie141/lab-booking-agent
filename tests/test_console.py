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
