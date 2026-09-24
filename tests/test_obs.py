"""结构化日志与请求关联（P1-3）。

## 这个文件在证什么

「日志改成 JSON 了」本身**不是**可验收的成果 —— 随便 ``print`` 一行 JSON 也是 JSON。
真正要证的是四件事，缺任何一件这套东西在排障时都用不上：

1. **一条链路能被挑出来。** 同一个 request_id 必须同时出现在
   HTTP 访问日志、审计表、门禁流水里。只出现在访问日志里等于没用：
   出问题时要问的是"这次请求写了什么审计、开了哪扇门"。
2. **id 能被用户报出来。** 响应头必须回传同一个 id，
   否则用户只能说"大概十点那会儿出错了"，你还是得全量翻日志。
3. **边界拒绝的请求也有 id。** 413/411 是在读 body 之前就被挡下的，
   根本没进路由 —— 而这恰恰是最需要排查的一类请求。这一条钉的是
   中间件的**添加顺序**，顺序反了它会静默失效。
4. **入站的 id 不可信。** ``X-Request-Id`` 是调用方给的，
   原样落进 JSON 日志就是一条**日志注入**通道：
   ``X-Request-Id: abc\\n{"level":"ERROR",...}`` 能让告警系统吃下一条伪造的 ERROR。

另外单独一组是 ``ContextVar`` 的选型正确性：嵌套绑定要还原对（用 token 而不是
把旧值 set 回去），异常路径也要还原 —— 还原错的表现是"另一条请求的日志里
出现别人的 request_id"，这种串味极难从现象反推原因。
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from lagent.audit import ACTION_LOGIN
from lagent.db import session_scope
from lagent.models import AccessEvent, AuditLog
from lagent.obs import (
    REQUEST_ID_HEADER,
    JsonFormatter,
    TextFormatter,
    bind_request_id,
    bind_user,
    clear_user_context,
    configure_from_settings,
    configure_logging,
    current_request_id,
    current_user_id,
    current_user_name,
    get_logger,
    new_request_id,
    sanitize_request_id,
)


# ==========================================================================
# 隔离：这个文件测的是**进程级可变状态**
# ==========================================================================
@pytest.fixture(autouse=True)
def _clean_identity_context() -> Iterator[None]:
    """每个用例前后都把身份上下文清空。

    ``bind_user`` 写的是一个**进程级**的 ContextVar。前面那组 TestBindUser
    必须亲手 set 一个值才能断言，而它没有对应的"还原"动作 ——
    于是那个值会留在上下文里漏给后面的用例，症状是
    "一条完全匿名的健康探针请求，日志里却带着 user_id: 7"。

    这与 ``conftest`` 里默认关掉后台清扫是同一条理由：
    **跨用例共享的可变状态就是不确定性**，哪怕它只在特定的执行顺序下发作。
    值得注意的是，这个漏出来的值**不是**被测代码漏的 ——
    请求链路上的泄漏由中间件里的 ``clear_user_context()`` 兜住（有专门的用例），
    这里兜的是"测试自己写进全局状态"的那一半。
    """
    clear_user_context()
    yield
    clear_user_context()

# ==========================================================================
# 工具
# ==========================================================================
@contextlib.contextmanager
def captured_logs(level: int = logging.DEBUG) -> Iterator[io.StringIO]:
    """把根 logger 的级别调到 ``level`` 并挂一个 JSON handler 收进内存。

    **必须显式改根级别**：应用的 lifespan 会把根级别设成 INFO（见
    ``configure_from_settings``），而 ``lagent.api`` 自己没有级别，
    于是有效级别继承为 INFO —— DEBUG 记录在**产生处**就被丢掉了，
    挂在 handler 上加什么过滤都救不回来。

    退出时恢复 handler 列表与级别，避免这条用例污染后面所有用例的日志。
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    root.addHandler(handler)
    root.setLevel(level)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.setLevel(saved_level)
        del saved_handlers  # 只用于说明"我们没有动别人的 handler"


@contextlib.contextmanager
def pristine_root_logger() -> Iterator[logging.Logger]:
    """保存/恢复根 logger 的 handler、级别，以及降噪表里那几个库的级别。

    ``configure_logging`` 的职责就是**接管全局**，所以测它必然要动全局状态。
    不还原的话，后面的用例会在一套"不知道谁配的"日志环境里跑。
    """
    root = logging.getLogger()
    noise = ("uvicorn.access", "httpx", "httpcore", "aiosqlite")
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_noise = {name: logging.getLogger(name).level for name in noise}
    try:
        yield root
    finally:
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                root.removeHandler(handler)
        for handler in saved_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(saved_level)
        for name, level in saved_noise.items():
            logging.getLogger(name).setLevel(level)


def json_lines(stream: io.StringIO) -> list[dict[str, Any]]:
    """把捕获到的日志逐行解析成 dict。

    顺带钉住「一行一个 JSON」：解析失败就直接让用例红，
    而不是用正则从半行文本里捞 —— 那等于把"格式坏了"这件事放过。
    """
    rows = []
    for line in stream.getvalue().splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def find(rows: list[dict[str, Any]], *, msg: str) -> dict[str, Any] | None:
    for row in rows:
        if row.get("msg") == msg:
            return row
    return None


def find_request(rows: list[dict[str, Any]], *, path: str) -> dict[str, Any] | None:
    """按**路径**找那条访问日志。

    ⚠️ 不能只用 ``msg == "请求"`` 取第一条：``as_user``／``login`` 会先打一次
    ``POST /api/auth/login``，它同样是"请求"。于是"第一条请求日志"经常是登录，
    而登录端点的依赖链上没有 ``bind_user`` —— 拿它断言 ``user_id`` 必然 KeyError，
    但看上去像是"认证依赖没生效"，方向全错。
    """
    for row in rows:
        if row.get("msg") == "请求" and row.get("path") == path:
            return row
    return None


# ==========================================================================
# 入站 request_id 的白名单校验
# ==========================================================================
class TestSanitizeRequestId:
    """入站值是**不可信输入**，这一组全是"应该被丢掉"的形态。"""

    def test_keeps_a_normal_id(self):
        assert sanitize_request_id("abc-123_XYZ.def") == "abc-123_XYZ.def"

    def test_keeps_a_uuid(self):
        value = "0f8e5b1c-2a3d-4e5f-8a9b-0c1d2e3f4a5b"
        assert sanitize_request_id(value) == value

    def test_strips_surrounding_whitespace(self):
        # 网关捎带空格很常见，这种是"能用"的，不该当成注入丢掉
        assert sanitize_request_id("  abc123  ") == "abc123"

    def test_empty_and_none_become_empty(self):
        assert sanitize_request_id(None) == ""
        assert sanitize_request_id("") == ""
        assert sanitize_request_id("   ") == ""

    def test_newline_injection_is_dropped(self):
        """最核心的一条：换行能把一条日志劈成两条，伪造出 ERROR。"""
        evil = 'abc\n{"level":"ERROR","msg":"磁盘满了"}'
        assert sanitize_request_id(evil) == ""

    def test_json_injection_without_newline_is_dropped(self):
        """就算没有换行，引号本身也能在 JSON 日志里提前闭合字符串。"""
        assert sanitize_request_id('abc","level":"ERROR') == ""

    def test_control_characters_are_dropped(self):
        for evil in ("abc\tdef", "abc\rdef", "abc\x00def", "abc\x1b[31mdef"):
            assert sanitize_request_id(evil) == "", evil

    def test_spaces_inside_are_dropped(self):
        assert sanitize_request_id("abc def") == ""

    def test_unicode_and_slash_are_dropped(self):
        # `/` 也不在白名单里：这个值可能被拼进 URL 做日志检索
        assert sanitize_request_id("abc/../etc") == ""
        assert sanitize_request_id("请求123") == ""

    def test_over_length_is_dropped(self):
        assert sanitize_request_id("a" * 36) == "a" * 36  # 恰好等于上限：留
        assert sanitize_request_id("a" * 37) == ""  # 多一个就丢

    def test_length_is_measured_after_stripping(self):
        assert sanitize_request_id("  " + "a" * 36 + "  ") == "a" * 36


class TestNewRequestId:
    def test_is_sixteen_hex_chars(self):
        value = new_request_id()
        assert len(value) == 16
        assert all(ch in "0123456789abcdef" for ch in value)

    def test_is_unique_enough(self):
        # 64 bit 随机：撞一个的概率是 2^-64，一万次里不该出现重复
        assert len({new_request_id() for _ in range(10_000)}) == 10_000

    def test_passes_its_own_sanitiser(self):
        """自己造的值必须是"外面也肯接受"的形状。

        否则它回传给前端、前端再带回来时会被丢掉，
        表现为"同一个请求在两次日志里有两个 id"—— 比没有关联 id 更难查。
        """
        value = new_request_id()
        assert sanitize_request_id(value) == value


# ==========================================================================
# ContextVar 绑定
# ==========================================================================
class TestBindRequestId:
    def test_default_is_empty_not_a_placeholder(self):
        """空串是"没有上下文"的诚实表示。

        回退成 ``"-"`` 会让日志里出现 ``request_id: "-"``，
        看起来像一个真实存在的请求 —— 于是"这条日志没有上下文"这件事
        就再也看不出来了。
        """
        assert current_request_id() == ""

    def test_generates_when_not_given(self):
        with bind_request_id() as value:
            assert value
            assert current_request_id() == value
        assert current_request_id() == ""

    def test_uses_the_given_value(self):
        with bind_request_id("req-given") as value:
            assert value == "req-given"
            assert current_request_id() == "req-given"

    def test_restores_after_exit(self):
        with bind_request_id("outer"):
            with bind_request_id("inner"):
                assert current_request_id() == "inner"
            # 内层退出后必须**回到外层**，不是回到空
            assert current_request_id() == "outer"
        assert current_request_id() == ""

    def test_restores_on_exception(self):
        with pytest.raises(RuntimeError), bind_request_id("doomed"):
            assert current_request_id() == "doomed"
            raise RuntimeError("boom")
        assert current_request_id() == ""

    def test_empty_string_is_treated_as_missing(self):
        # request_id="" 与 None 同义：都表示"调用方没给"，于是自己生成一个
        with bind_request_id("") as value:
            assert value

    async def test_visible_across_awaits_and_child_tasks(self):
        """async 服务里最重要的性质：同一上下文的 await 链和子任务都能读到。

        这正是用 ``ContextVar`` 而不是 ``threading.local`` 的理由 ——
        一个事件循环线程上并发跑着成百上千个协程，thread-local 会让
        它们互相读到对方的 request_id。
        """
        import asyncio

        async def read() -> str:
            await asyncio.sleep(0)
            return current_request_id()

        with bind_request_id("req-async"):
            assert await read() == "req-async"
            assert await asyncio.create_task(read()) == "req-async"

    async def test_concurrent_requests_do_not_bleed(self):
        """两条并发请求各自的 id 必须互不串味。

        用 ``asyncio.gather`` 真并发地把两个上下文交错执行 ——
        如果绑定实现错了（比如把值 set 回去而不是 reset），
        这条会在某个交错点上读到对方的 id。
        """
        import asyncio

        async def worker(name: str) -> list[str]:
            seen = []
            for _ in range(20):
                await asyncio.sleep(0)
                seen.append(current_request_id())
            return seen

        async def run(name: str) -> list[str]:
            with bind_request_id(name):
                return await worker(name)

        results = await asyncio.gather(run("req-a"), run("req-b"))
        assert set(results[0]) == {"req-a"}
        assert set(results[1]) == {"req-b"}


class TestBindUser:
    def test_defaults_are_empty(self):
        assert current_user_id() is None
        assert current_user_name() == ""

    def test_records_user(self):
        bind_user(7, "张伟")
        assert current_user_id() == 7
        assert current_user_name() == "张伟"

    def test_none_user_is_allowed(self):
        bind_user(None)
        assert current_user_id() is None

    def test_blank_username_is_normalised(self):
        bind_user(7, "")
        assert current_user_name() == ""


# ==========================================================================
# JSON 格式化
# ==========================================================================
class TestJsonFormatter:
    """``probe()`` 造一个自带内存 handler 的 logger。

    **必须显式 ``setLevel``**：裸 ``logging.Logger("x")`` 的级别是 NOTSET，
    有效级别继承自根 —— 而 pytest 环境下根是 WARNING，于是 ``info()``
    在产生处就被丢掉，断言只会看到"日志不见了"，查半天发现是测试自己的问题。
    """

    @staticmethod
    def _one(**fmt_kwargs: Any) -> tuple[logging.Logger, io.StringIO]:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonFormatter(**fmt_kwargs))
        logger = logging.Logger("lagent.probe")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.addHandler(handler)
        return logger, stream

    def _emit(self, msg: str = "hello", **extra: Any) -> dict[str, Any]:
        logger, stream = self._one()
        logger.info(msg, extra=extra or None)
        rows = json_lines(stream)
        assert len(rows) == 1, stream.getvalue()
        return rows[0]

    def test_is_one_line(self):
        logger, stream = self._one()
        logger.info("第一行\n第二行")
        # 换行在 JSON 里被转义，物理上仍然只有一行
        assert len(stream.getvalue().strip().splitlines()) == 1

    def test_core_fields_present(self):
        row = self._emit()
        assert row["level"] == "INFO"
        assert row["logger"] == "lagent.probe"
        assert row["msg"] == "hello"
        assert "ts" in row

    def test_ts_is_iso_with_milliseconds(self):
        row = self._emit()
        # 2026-09-24T14:31:07.123
        assert row["ts"][4] == "-" and row["ts"][10] == "T"
        assert "." in row["ts"]

    def test_chinese_is_not_escaped(self):
        """``ensure_ascii=False``。

        转成 ``\\uXXXX`` 之后 ``grep 登录失败`` 永远匹配不到 ——
        而"用 grep 捞一条日志"是排障时最常用的动作。
        """
        logger, stream = self._one()
        logger.info("登录失败")
        raw = stream.getvalue()
        assert "登录失败" in raw
        assert "\\u" not in raw

    def test_request_id_included_only_when_bound(self):
        logger, stream = self._one()
        logger.info("无上下文")
        assert "request_id" not in json_lines(stream)[0]

        logger2, stream2 = self._one()
        with bind_request_id("req-xyz"):
            logger2.info("有上下文")
        assert json_lines(stream2)[0]["request_id"] == "req-xyz"

    def test_user_included_only_when_bound(self):
        logger, stream = self._one()
        with bind_request_id("req-xyz"):
            bind_user(7, "张伟")
            logger.info("已认证")
        row = json_lines(stream)[0]
        assert row["user_id"] == 7
        assert row["user"] == "张伟"

    def test_extra_fields_pass_through(self):
        """``extra`` 里的字段要变成真字段，能直接按它检索。

        这是"结构化"的实际价值：不用为了捞"某个设备的日志"去正则匹配中文句子。
        """
        row = self._emit(equipment_id=3, lab_id=1)
        assert row["equipment_id"] == 3
        assert row["lab_id"] == 1

    def test_private_extra_is_dropped(self):
        # 下划线开头的键是内部用的（比如 marker），不该漏进日志
        logger, stream = self._one()
        logger.info("x", extra={"_secret": "should-not-appear"})
        assert "_secret" not in json_lines(stream)[0]

    def test_extra_with_datetime_does_not_raise(self):
        """``default=str``。

        日志系统自己抛异常比少一条日志严重得多 —— 而 extra 里塞
        datetime/Decimal 是最容易顺手写出来的写法。
        """
        import datetime as dt

        row = self._emit(when=dt.datetime(2026, 9, 24, 14, 0))
        assert "2026-09-24" in row["when"]

    def test_exception_is_split_into_type_and_message(self):
        logger, stream = self._one()
        try:
            raise ValueError("凭证已过期")
        except ValueError:
            logger.exception("核验失败")
        row = json_lines(stream)[0]
        assert row["exc_type"] == "ValueError"
        assert row["exc_message"] == "凭证已过期"

    def test_traceback_absent_by_default(self):
        """堆栈默认不写。

        整段堆栈塞进一行 JSON 会让日志体积翻好几倍，
        而排查时九成情况只看异常类型和消息。要的时候用 LAB_LOG_TRACEBACK 打开。
        """
        logger, stream = self._one()
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("挂了")
        assert "traceback" not in json_lines(stream)[0]

    def test_traceback_present_when_enabled(self):
        logger, stream = self._one(include_traceback=True)
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("挂了")
        row = json_lines(stream)[0]
        assert "ValueError: boom" in row["traceback"]


class TestTextFormatter:
    @staticmethod
    def _one() -> tuple[logging.Logger, io.StringIO]:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(TextFormatter())
        logger = logging.Logger("lagent.probe")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.addHandler(handler)
        return logger, stream

    def test_carries_request_id_prefix(self):
        logger, stream = self._one()
        with bind_request_id("req-txt"):
            logger.info("启动完成")
        out = stream.getvalue()
        assert "[req-txt]" in out
        assert "启动完成" in out

    def test_extras_appended(self):
        logger, stream = self._one()
        logger.info("请求", extra={"status": 200})
        assert "status=200" in stream.getvalue()

    def test_no_prefix_without_context(self):
        logger, stream = self._one()
        logger.info("裸日志")
        assert "[" not in stream.getvalue()


# ==========================================================================
# 装配
# ==========================================================================
class TestConfigureLogging:
    def test_json_is_the_default_format(self):
        with pristine_root_logger():
            root = configure_logging()
            assert isinstance(root.handlers[-1].formatter, JsonFormatter)

    def test_text_format_can_be_chosen(self):
        with pristine_root_logger():
            root = configure_logging(fmt="text")
            assert isinstance(root.handlers[-1].formatter, TextFormatter)

    def test_level_is_applied(self):
        with pristine_root_logger():
            root = configure_logging(level="WARNING")
            assert root.level == logging.WARNING

    def test_unknown_level_falls_back_instead_of_raising(self):
        """配置写错不该让服务起不来。

        ``logging.getLevelName("VERBOSE")`` 返回字符串 ``"Level VERBOSE"``，
        直接丢给 ``setLevel`` 会抛 ValueError —— 于是打错一个字母就起不来服务。
        """
        with pristine_root_logger():
            root = configure_logging(level="NOT-A-LEVEL")
            assert root.level == logging.INFO

    def test_is_idempotent(self):
        """重复调用不该挂出两条重复日志。

        判断"我配过没"不能用 ``if logger.handlers``：alembic 这类库会自己挂
        NullHandler，于是判断永远为假、每调一次就多一条重复日志（P1-1 踩过）。
        这里用 handler 上的私标记位来认自己那一个。
        """
        with pristine_root_logger():
            configure_logging()
            first = len(logging.getLogger().handlers)
            configure_logging()
            configure_logging()
            assert len(logging.getLogger().handlers) == first

    def test_foreign_handlers_are_preserved(self):
        """别人的 handler 一个都不能动 —— 包括库自己挂的 NullHandler。"""
        with pristine_root_logger():
            root = logging.getLogger()
            foreign = logging.NullHandler()
            root.addHandler(foreign)
            configure_logging()
            assert foreign in root.handlers

    def test_noisy_libraries_are_demoted_not_silenced(self):
        # 设成 CRITICAL 会连它们的 WARNING 一起吞掉，而那是诊断价值最高的一档
        with pristine_root_logger():
            configure_logging(level="DEBUG")
            for name in ("uvicorn.access", "httpx", "httpcore", "aiosqlite"):
                assert logging.getLogger(name).level == logging.WARNING

    def test_output_goes_to_the_given_stream(self):
        with pristine_root_logger():
            stream = io.StringIO()
            configure_logging(stream=stream)
            get_logger("lagent.probe").info("落在指定流上")
            assert "落在指定流上" in stream.getvalue()


class TestConfigureFromSettings:
    def test_reads_level_and_format_from_env(self, monkeypatch):
        from lagent.config import reset_settings_cache

        with pristine_root_logger():
            monkeypatch.setenv("LAB_LOG_LEVEL", "ERROR")
            monkeypatch.setenv("LAB_LOG_FORMAT", "text")
            monkeypatch.setenv("LAB_LOG_TRACEBACK", "true")
            reset_settings_cache()
            configure_from_settings(force=True)

            root = logging.getLogger()
            assert root.level == logging.ERROR
            formatter = root.handlers[-1].formatter
            assert isinstance(formatter, TextFormatter)
            reset_settings_cache()

    def test_second_call_without_force_is_a_noop(self, monkeypatch):
        """入口各调一次，第二次必须什么都不做。

        否则 CLI 里"模块导入时配一次、main 里再配一次"会重复接管根 logger。
        """
        from lagent.config import reset_settings_cache

        with pristine_root_logger():
            monkeypatch.setenv("LAB_LOG_LEVEL", "ERROR")
            reset_settings_cache()
            configure_from_settings(force=True)
            monkeypatch.setenv("LAB_LOG_LEVEL", "DEBUG")
            reset_settings_cache()
            configure_from_settings()  # 没有 force
            assert logging.getLogger().level == logging.ERROR
            reset_settings_cache()


# ==========================================================================
# HTTP 层：关联 id 的贯穿
# ==========================================================================
class TestResponseRequestId:
    """响应头要能让用户把 id 报出来。"""

    async def test_header_present_and_well_formed(self, http):
        resp = await http.get("/api/health")
        assert resp.status_code == 200
        value = resp.headers.get(REQUEST_ID_HEADER)
        assert value
        assert len(value) == 16
        assert all(ch in "0123456789abcdef" for ch in value)

    async def test_valid_inbound_id_is_echoed(self, http):
        """网关给的 id 要沿用 —— 否则和网关日志对不上。"""
        resp = await http.get(
            "/api/health", headers={REQUEST_ID_HEADER: "gateway-0f8e5b1c"}
        )
        assert resp.headers[REQUEST_ID_HEADER] == "gateway-0f8e5b1c"

    async def test_injection_attempt_is_replaced_not_echoed(self, http):
        """注入值**不能**回传，也不能落进日志。

        ⚠️ 这条走的是**直接驱动 ASGI**（见文末的 ``_asgi_get``），不是 httpx：
        httpx 在客户端就会拒绝带 ``\\n`` 的头（``LocalProtocolError``）。
        一个规规矩矩的 HTTP 客户端没法扮演攻击者 —— 拿它测注入，
        测到的只是"httpx 不让我发"，而不是"我的服务挡住了"。
        """
        status, headers = await _asgi_get(
            http, "/api/health", {REQUEST_ID_HEADER: INJECTION}
        )
        assert status == 200
        got = headers[REQUEST_ID_HEADER.lower()]
        assert got != INJECTION
        assert "ERROR" not in got
        assert "\n" not in got
        assert len(got) == 16

    async def test_injection_is_not_written_into_the_log(self, http):
        """换个角度再钉一次：**日志里**不能出现伪造的那条 ERROR。

        只断言"响应头被换掉了"是不够的 —— 真正被攻击的是日志。
        这里读的是这个请求写出的日志行，确认注入串没有变成一条记录。
        """
        with captured_logs() as stream:
            await _asgi_get(http, "/api/health", {REQUEST_ID_HEADER: INJECTION})
        rows = json_lines(stream)
        assert rows, "应当至少有一条访问日志"
        assert all(row["level"] != "ERROR" for row in rows)
        assert "磁盘满了" not in stream.getvalue()

    async def test_over_length_inbound_id_is_replaced(self, http):
        resp = await http.get(
            "/api/health", headers={REQUEST_ID_HEADER: "a" * 200}
        )
        assert resp.headers[REQUEST_ID_HEADER] != "a" * 200

    async def test_two_requests_get_different_ids(self, http):
        first = (await http.get("/api/health")).headers[REQUEST_ID_HEADER]
        second = (await http.get("/api/health")).headers[REQUEST_ID_HEADER]
        assert first != second

    async def test_rejected_request_still_carries_an_id(self, http, as_user):
        """413 是被 BodySizeLimitMiddleware 在读 body **之前**挡下的。

        它根本没进路由，所以这一条钉的是中间件的**添加顺序**：
        ``RequestContextMiddleware`` 必须比体积校验更靠外，
        否则"被边界拦下"这类最需要排查的请求一个关联 id 都没有。
        """
        resp = await http.post(
            "/api/agent/chat",
            content=json.dumps({"message": "x" * 200_000}).encode("utf-8"),
            headers={"Content-Type": "application/json", **(await as_user("李娜"))},
        )
        assert resp.status_code == 413
        assert resp.headers.get(REQUEST_ID_HEADER)

    async def test_401_still_carries_an_id(self, http):
        resp = await http.get("/api/auth/me")
        assert resp.status_code == 401
        assert resp.headers.get(REQUEST_ID_HEADER)

    async def test_404_still_carries_an_id(self, http):
        resp = await http.get("/api/no-such-endpoint")
        assert resp.status_code == 404
        assert resp.headers.get(REQUEST_ID_HEADER)

    async def test_unknown_header_name_is_ignored(self, http):
        """不认识的关联网关头不当成 request_id，自己生成一个。"""
        resp = await http.get("/api/health", headers={"X-Trace-Id": "gateway-abc"})
        assert resp.headers[REQUEST_ID_HEADER] != "gateway-abc"


class TestRequestIdCrossesLayers:
    """「贯穿」的实际验收：同一个 id 在三个地方都查得到。"""

    async def test_audit_row_carries_the_response_id(self, http):
        resp = await http.post(
            "/api/auth/login",
            json={"username": "张伟", "password": "zhangwei@123"},
        )
        assert resp.status_code == 200
        request_id = resp.headers[REQUEST_ID_HEADER]

        async with session_scope() as session:
            row = await session.scalar(
                select(AuditLog)
                .where(AuditLog.action == ACTION_LOGIN)
                .order_by(AuditLog.id.desc())
            )
        assert row is not None
        # 这就是"从用户报的一个 id 直接定位到这次请求做了什么"
        assert row.request_id == request_id

    async def test_failed_login_audit_also_carries_it(self, http):
        """失败路径同样要能关联。

        审计最容易做成"只记成功"，于是「谁在反复试别人的账号」这种
        最该被发现的模式恰好一片空白 —— 而那条记录也必须挂在同一个 id 上。
        """
        resp = await http.post(
            "/api/auth/login",
            json={"username": "张伟", "password": "wrong-password"},
        )
        assert resp.status_code == 401
        request_id = resp.headers[REQUEST_ID_HEADER]

        async with session_scope() as session:
            row = await session.scalar(
                select(AuditLog).order_by(AuditLog.id.desc())
            )
        assert row is not None
        assert row.request_id == request_id

    async def test_access_event_carries_the_response_id(self, http, as_user):
        """门禁流水也挂同一个 id。

        这是「一条 SQL 把'谁刷的卡'和'哪个请求触发的'对上」的验证 ——
        门禁流水和审计是两张表，出问题时你无法从审计反查到刷卡记录，
        除非它们共用一个关联 id。
        """
        resp = await http.post(
            "/api/access/verify",
            json={
                "lab_id": 1,
                "gate_id": "gate-01",
                "direction": "in",
                "credential": "not-a-real-credential",
                "user_id": 1,
            },
            headers=await as_user("管理员"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["granted"] is False  # 假凭证必须被拒
        request_id = resp.headers[REQUEST_ID_HEADER]

        async with session_scope() as session:
            row = await session.scalar(
                select(AccessEvent).order_by(AccessEvent.id.desc())
            )
        assert row is not None
        assert row.request_id == request_id

    async def test_audit_outside_a_request_uses_empty_string(self, isolated_db):
        """请求之外写的审计行 request_id 是空串，**不是 NULL**。

        用 NULL 的话 ``WHERE request_id = ''`` 查不到这些行，
        于是"清扫任务/CLI 写的审计"会在按 request_id 检索时凭空消失。
        空串让"没有关联请求"这件事本身可被查询。
        """
        from lagent.audit import record

        await record(action="probe.no-request")
        async with session_scope() as session:
            row = await session.scalar(
                select(AuditLog).where(AuditLog.action == "probe.no-request")
            )
        assert row is not None
        assert row.request_id == ""


class TestAccessLog:
    """访问日志本身的内容。"""

    async def test_normal_request_logs_one_line(self, http):
        # 刻意不带令牌：401 也要有访问日志 —— 拒绝路径同样是排障对象
        with captured_logs() as stream:
            resp = await http.get("/api/labs")
        assert resp.status_code == 401  # /api/labs 要令牌
        row = find_request(json_lines(stream), path="/api/labs")
        assert row is not None, stream.getvalue()
        assert row["method"] == "GET"
        assert row["path"] == "/api/labs"
        assert row["status"] == 401
        assert isinstance(row["duration_ms"], (int, float))

    async def test_request_id_in_log_matches_the_response_header(self, http):
        with captured_logs() as stream:
            resp = await http.get("/api/health")
        request_id = resp.headers[REQUEST_ID_HEADER]
        row = find(json_lines(stream), msg="探针")
        assert row is not None, stream.getvalue()
        assert row["request_id"] == request_id

    async def test_inbound_id_shows_up_in_the_log(self, http):
        with captured_logs() as stream:
            await http.get(
                "/api/health", headers={REQUEST_ID_HEADER: "gateway-abc123"}
            )
        row = find(json_lines(stream), msg="探针")
        assert row is not None
        assert row["request_id"] == "gateway-abc123"

    async def test_query_string_is_not_logged(self, http, as_user):
        """只记路径。

        查询串里可能带用户输入（甚至参数化的令牌），而"哪个接口、多慢、
        什么结果"这三件事不需要它 —— 少记一样就少一样泄进日志的输入。

        ⚠️ 断言必须**只看我们自己那几行**（``msg == "请求"``），
        不能笼统地 ``"secret-value" not in stream``：``httpx`` 客户端自己
        会按 INFO 打一行 ``HTTP Request: GET /api/labs?token=…``，
        那条带着完整 URL。它在生产里由 ``configure_logging`` 的
        降噪表压到 WARNING（这正是那张表存在的理由之一），
        但在测试里级别被本用例临时抬到 DEBUG，于是它会露出来。
        """
        with captured_logs() as stream:
            resp = await http.get(
                "/api/labs",
                params={"token": "secret-value"},
                headers=await as_user("李娜"),
            )
        assert resp.status_code == 200
        rows = [row for row in json_lines(stream) if row.get("msg") == "请求"]
        assert rows, stream.getvalue()
        assert "secret-value" not in json.dumps(rows, ensure_ascii=False)
    async def test_health_probe_is_demoted_to_debug(self, http):
        """探针可能每几秒一次，按 INFO 记会把业务日志冲掉。

        降成 DEBUG 而不是丢掉：要查探针本身是否正常时仍然拿得到。
        """
        with captured_logs(level=logging.INFO) as stream:
            await http.get("/api/health")
        assert find(json_lines(stream), msg="探针") is None

    async def test_health_probe_is_visible_at_debug_level(self, http):
        with captured_logs(level=logging.DEBUG) as stream:
            await http.get("/api/health")
        assert find(json_lines(stream), msg="探针") is not None

    async def test_authenticated_request_logs_the_user(self, http, as_user):
        """``bind_user`` 的效果：访问日志里带上 user_id。

        验证的是"在认证依赖里写一次，外面这一层读得到"——
        如果认证依赖运行在**复制出来的上下文**里（同步端点会被丢进线程池），
        这个值就传不回来。本项目所有端点都是 async def，所以成立；
        哪天新增同步端点，这条会立刻红。
        """
        headers = await as_user("张伟")
        with captured_logs() as stream:
            resp = await http.get("/api/auth/me", headers=headers)
        assert resp.status_code == 200
        row = find_request(json_lines(stream), path="/api/auth/me")
        assert row is not None, stream.getvalue()
        assert row["user_id"] == 1  # 张伟
        assert row["user"] == "张伟"

    async def test_anonymous_request_has_no_user_id(self, http):
        with captured_logs() as stream:
            await http.get("/api/health")
        row = find(json_lines(stream), msg="探针")
        assert row is not None
        assert "user_id" not in row

    async def test_identity_does_not_leak_into_the_next_request(self, http, as_user):
        """身份用完必须清掉 —— 否则下一条**匿名**请求的日志会带着别人的 user_id。

        这是本文件里唯一一条"先在实现里发现、再补测试"的用例：
        ``request_id`` 靠 ContextVar 的 token 还原，而 ``bind_user``
        只 set 不还原。只要上下文被复用，身份就会串到下一次请求上。

        它比"没有 user_id"更坏 —— 没有字段只说明查不到是谁，
        而一个错的 user_id 会让人得出"某个已登录用户在打健康探针"这种
        完全错误的结论，还会把排查方向带偏。
        """
        zhangwei = await as_user("张伟")
        await http.get("/api/auth/me", headers=zhangwei)
        with captured_logs() as stream:
            await http.get("/api/health")  # 完全匿名
        row = find(json_lines(stream), msg="探针")
        assert row is not None
        assert "user_id" not in row, f"上一条请求的身份漏过来了：{row}"

    async def test_identity_does_not_leak_across_users(self, http, as_user):
        """换个人登录，日志里的 user_id 必须跟着换，不能留在上一个人身上。

        两条令牌都**先在捕获之外**取好：``as_user`` 会打一次登录请求，
        它也会被记进访问日志（路径是 ``/api/auth/login``）。
        把登录留在捕获里，``find_request(path="/api/auth/me")`` 虽然仍能挑对，
        但日志流里多一条无关请求，断言的可读性会变差。
        """
        zhangwei = await as_user("张伟")
        lina = await as_user("李娜")
        await http.get("/api/auth/me", headers=zhangwei)

        with captured_logs() as stream:
            await http.get("/api/auth/me", headers=lina)
        row = find_request(json_lines(stream), path="/api/auth/me")
        assert row is not None
        assert row["user"] == "李娜"
        assert row["user_id"] == 2


# ==========================================================================
# 直接驱动 ASGI：httpx 造不出来的"恶意客户端"
# ==========================================================================
INJECTION = 'abc\n{"level":"ERROR","msg":"磁盘满了"}'


def _as_bytes(value: str | bytes) -> bytes:
    """头值 → 原始字节。str 一律按 UTF-8（见调用处的说明）。"""
    return value if isinstance(value, bytes) else value.encode("utf-8")


async def _asgi_request(
    app: Any, method: str, path: str, headers: dict[str, str | bytes]
) -> tuple[int, dict[str, str]]:
    """手工构造 ASGI scope 调一次请求，返回 (status, 小写响应头)。

    存在的理由只有一个：**httpx 会替我们拒绝非法头**。
    要验证"服务端挡住了 ``\\n`` 注入"，就必须让那个字节真的到达服务端 ——
    而规范的客户端不让你发。这一层绕开客户端校验，是唯一能测到
    ``sanitize_request_id`` 在真实链路上生效的方式。
    """
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        # ASGI 规定 header 名必须小写；值按**原始字节**给。
        #
        # 值用 UTF-8 编码而不是 latin-1：HTTP 头的值本来是任意字节，
        # 服务端按 latin-1 逐字节还原，于是非 ASCII 字节会变成 U+0080~U+00FF
        # 的字符 —— 正好落在 ``_ALLOWED_CHARS`` 之外，被丢弃。
        # 这正是真实链路上的行为：注入载荷里可以塞中文，
        # 而 latin-1 编码它反而会在**测试自己**这里先抛 UnicodeEncodeError。
        "headers": [
            (k.lower().encode("latin-1"), _as_bytes(v))
            for k, v in headers.items()
        ],
        "client": ("203.0.113.7", 54321),
        "server": ("test", 80),
    }
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)

    start = next(m for m in sent if m["type"] == "http.response.start")
    out = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in start["headers"]
    }
    return int(start["status"]), out


async def _asgi_get(
    client: Any, path: str, headers: dict[str, str | bytes]
) -> tuple[int, dict[str, str]]:
    """对 ``http`` 夹具里那个应用发一次 GET（``client.app`` 由 conftest 挂上）。"""
    return await _asgi_request(client.app, "GET", path, headers)
