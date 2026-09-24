"""结构化日志与请求关联（P1-3）。

## 这个模块解决的是「量上来之后查不了」

在此之前，排障的唯一手段是 ``print`` 出来的自由文本。那套东西在单机演示没问题，
但它有三个硬伤，而且都是**量一大就暴露**：

1. **一行日志串不起一条链路。** 一次 ``/api/agent/chat`` 会写审计、可能写门禁流水、
   可能触发下单。出问题时你想问的是「这一个请求到底发生了什么」，
   而自由文本里只有一堆互不相干的句子，没有任何共同字段能把它们挑出来。
2. **没有机器可读的字段。** 「最近 5 分钟 5xx 有多少」这种问题，
   对着 ``print`` 的输出只能靠正则，而且每改一次日志文案就要改一次正则。
3. **没有耗时。** 慢请求不会自己喊出来，你只知道"有时候慢"。

所以这里做两件事：**日志变成一行一个 JSON**，**每个请求有一个 request_id 贯穿全链路**。

## request_id 用 ContextVar，不用线程局部变量

这两个选型差别很大，而且错了会静默串味：

* ``threading.local`` 假设「一个线程只服务一个请求」—— 这在 async 服务里是**错的**。
  一个事件循环线程上会并发跑成百上千个协程，thread-local 会让同线程的请求
  读到彼此的 request_id。
* ``ContextVar`` 跟着**当前的执行上下文**走。``asyncio`` 在创建 Task 时会复制
  当前上下文，所以 ``asyncio.create_task(...)`` 起来的子任务、``await`` 链上
  的任何一层，都能读到同一个值 —— 这正是我们要的「贯穿」。

代价也是真的，必须写清楚：**``ContextVar`` 不会自动跨线程**。
``asyncio.to_thread()`` 里读不到调用方的 request_id（本项目用它跑 alembic 迁移，
那条路径上没有请求上下文，本来也不需要）。真要带过去就用
``contextvars.copy_context().run(...)``。

## 别用 logging.Filter 逐个 logger 挂

常见写法是写一个 Filter 往 ``record`` 上补字段，然后挂到每个 logger 上。
问题是"每个 logger"这个前提很难成立 —— 漏挂一个，那条链路就断了上下文，
而且断得静默。这里改成**在 Formatter 里读上下文**：格式化发生在最后一步，
只要日志最终经过这一个 handler，就一定有上下文。少一个需要维护的清单。

## 客户端给的 request_id 是**不可信输入**

``X-Request-Id`` 由调用方提供，用来把我们这条日志和网关/前端的日志对上。
但它同时是一条**日志注入**通道：

    X-Request-Id: abc\\n{"level":"ERROR","msg":"磁盘满了"}

原样写进日志，攻击者就能伪造出一条 ERROR；把 JSON 日志当数据源做告警的系统
会直接被打穿。所以入站值必须**白名单字符集 + 长度上限**，不合格就换一个新的，
而不是"清洗一下接着用"——丢弃比修补更难被绕过。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import secrets
import sys
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any

from .clock import now_local

__all__ = [
    "REQUEST_ID_HEADER",
    "JsonFormatter",
    "bind_request_id",
    "bind_user",
    "clear_user_context",
    "configure_from_settings",
    "configure_logging",
    "current_request_id",
    "current_user_id",
    "current_user_name",
    "get_logger",
    "new_request_id",
    "sanitize_request_id",
]

# 回传给调用方的响应头。用连字符式命名与外部的网关/前端约定一致。
REQUEST_ID_HEADER = "X-Request-Id"

# 入站 request_id 的合法字符集与长度上限。
#
# 字符集刻意**不含引号、空格、控制字符**：JSON 日志里这些都是注入点。
# 长度上限 36 是 UUID 的长度 —— 比自己生成的 16 位宽松，好接网关的 UUID。
_ALLOWED_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
_MAX_INBOUND_LEN = 36

_request_id: ContextVar[str] = ContextVar("lagent_request_id", default="")
# 当前请求的身份。由认证依赖在解出令牌后写入（见 bind_user），
# 于是**这次请求后续打的所有日志**自动带上 user_id —— 包括
# "审计写入失败"这种发生在业务逻辑深处的日志。
_user_id: ContextVar[int | None] = ContextVar("lagent_user_id", default=None)
_user_name: ContextVar[str] = ContextVar("lagent_user_name", default="")

# 结构化日志里固定出现的字段。剩下的当作 extra 透传。
_RESERVED = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
        # 我们自己会重新命名成 ts / level / logger / msg
        "ts", "level", "logger",
    }
)


# ---------------------------------------------------------------------------
# 请求上下文
# ---------------------------------------------------------------------------
def new_request_id() -> str:
    """生成一个新的 request_id。

    16 个十六进制字符（64 bit 随机）而不是完整 UUID：它的唯一职责是
    「在一段时间的日志里能唯一指认一个请求」，64 bit 足够，而且短一半更好读。
    """
    return secrets.token_hex(8)


def sanitize_request_id(value: str | None) -> str:
    """校验入站 ``X-Request-Id``；不合格就返回空串（由调用方换成新的）。

    这里是**丢弃**而不是"清洗"：把 ``\\n`` 换成 ``_`` 之类的修补看起来很稳，
    但要保证覆盖所有注入手法，等于要穷举所有编码怪癖。
    字符白名单反过来只允许我们认识的东西，绕过面小得多。
    """
    if not value:
        return ""
    candidate = value.strip()
    if not candidate or len(candidate) > _MAX_INBOUND_LEN:
        return ""
    if not all(ch in _ALLOWED_CHARS for ch in candidate):
        return ""
    return candidate


def current_request_id() -> str:
    """当前上下文的 request_id；不在请求里时是空串。

    **不回退成 "-" 或 "unknown"**：空串是"没有上下文"的诚实的表示，
    而 `"-"` 会让日志里的 `request_id: "-"` 看起来像一个真实存在的请求。
    """
    return _request_id.get()


@contextlib.contextmanager
def bind_request_id(request_id: str | None = None) -> Iterator[str]:
    """在这段代码里绑定一个 request_id，退出时还原。

    用 ``ContextVar.set`` 返回的 token 做 ``reset``，而不是把旧值再 set 回去 ——
    后者在嵌套时会把中间层的值写错（A→B→A 的还原顺序反了）。
    """
    value = request_id or new_request_id()
    token = _request_id.set(value)
    try:
        yield value
    finally:
        # reset 而不是 set 回去：嵌套场景下只有 token 知道该还原成什么。
        _request_id.reset(token)


def bind_user(user_id: int | None, username: str = "") -> None:
    """把「这次请求是谁」写进上下文。

    **刻意不做成 contextmanager**：它由认证依赖调用，作用域就是这次请求剩下的
    全部时间，而依赖没法把"请求结束"这个时点表达成一个 with 块。
    上下文本来就随这次请求（Task）一起销毁，不需要显式还原 ——
    但这也意味着**同一次请求里换了身份不会自动清掉旧值**，
    而本服务里不存在"一个请求用两个身份"的场景。

    ⚠️ 一个必须说清的限制：同步端点（``def`` 而不是 ``async def``）会被
    FastAPI 丢进线程池，那里的 ``set`` 发生在一个**复制出来的上下文**里，
    回不到中间件。本项目所有端点都是 ``async def``，所以成立；
    哪天新增同步端点，访问日志里就会少一个 user_id —— 这一点由
    ``tests/test_obs.py`` 里那条"访问日志带 user_id"的用例钉着。
    """
    _user_id.set(user_id)
    _user_name.set(username or "")


def clear_user_context() -> None:
    """清掉身份上下文。由请求中间件在**写完访问日志之后**调用。

    为什么必须有这个函数：``bind_user`` 只 ``set`` 不还原（依赖没法表达
    "请求结束"这个时点），而 request_id 是靠 token 还原的 ——
    于是"上下文被复用"时会出现一种很难查的串味：
    上一条请求的身份留在上下文里，下一条**匿名**请求的日志却带着
    ``user_id``。这种日志比没有 user_id 更坏，它会让人得出
    "某个已登录用户在访问健康探针"这种完全错误的结论。

    上下文会不会被复用，取决于跑在谁身上：uvicorn 每条请求起一个新 task
    （各有各的上下文副本），而**测试用的 ASGI 传输层就是复用的** ——
    ``tests/test_obs.py`` 里那条"身份不会漏给下一条请求"的用例正是这样
    红过一次才补上这个函数的。不依赖运行环境的行为，才是能带上去的行为。

    刻意用直接 set 而不是返回 token：调用点只有中间件一处，
    而它本来就是"这一层最外面"，没有需要还原的外层。
    """
    _user_id.set(None)
    _user_name.set("")


def current_user_id() -> int | None:
    return _user_id.get()


def current_user_name() -> str:
    return _user_name.get()


# ---------------------------------------------------------------------------
# JSON 格式化
# ---------------------------------------------------------------------------
class JsonFormatter(logging.Formatter):
    """一行一个 JSON 对象。

    几个刻意的选择：

    * ``ensure_ascii=False`` —— 日志文件是 UTF-8，中文直接可读。
      转成 ``\\uXXXX`` 只会让 ``grep 登录失败`` 永远匹配不到。
    * 异常单独拆成 ``exc_type`` / ``exc_message``，``traceback`` 只在
      ``LAB_LOG_TRACEBACK=true`` 时才带。堆栈动辄几十行，整段塞进一行 JSON
      会让日志体积翻好几倍，而排查时 90% 的情况只看异常类型和消息。
    * 未知的 ``extra`` 字段原样透传 —— ``logger.info("下单", extra={"equipment_id": 3})``
      里的 ``equipment_id`` 会成为一个真实字段，可以直接按它检索，
      不用去正则匹配那句话。
    """

    def __init__(self, *, include_traceback: bool = False) -> None:
        super().__init__()
        self.include_traceback = include_traceback

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _iso(now_local()),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        request_id = current_request_id()
        if request_id:
            payload["request_id"] = request_id
        user_id = current_user_id()
        if user_id is not None:
            payload["user_id"] = user_id
        user_name = current_user_name()
        if user_name:
            payload["user"] = user_name

        for key, value in record.__dict__.items():
            if key in _RESERVED or key in payload or key.startswith("_"):
                continue
            payload[key] = value

        if record.exc_info:
            exc_type, exc_value = record.exc_info[0], record.exc_info[1]
            payload["exc_type"] = getattr(exc_type, "__name__", str(exc_type))
            payload["exc_message"] = str(exc_value)
            if self.include_traceback:
                payload["traceback"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = record.stack_info

        # default=str：extra 里塞了 datetime / Decimal 之类不该让日志抛异常。
        # 日志系统自己崩掉比少一条日志严重得多。
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """人类可读的单行格式（本地开发用）。

    保留它是因为在终端里看 JSON 很痛苦；但**默认不是它** ——
    生产的默认值应当是可被机器消费的那一个，而不是好看的那一个。
    """

    def format(self, record: logging.LogRecord) -> str:
        request_id = current_request_id()
        prefix = f"[{request_id}] " if request_id else ""
        base = f"{_iso(now_local())} {record.levelname:<7} {record.name:<28} {prefix}{record.getMessage()}"
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        if extras:
            base += "  " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def _iso(moment: dt.datetime) -> str:
    return moment.isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------
# 我们挂上去的那一个 handler 的标记位。
# 不能用 `if logger.handlers` 判断「我配过没」—— alembic 那种库会自己挂
# NullHandler，于是判断永远为假、重复挂出多条重复日志（P1-1 踩过同一个坑）。
_HANDLER_MARKER = "_lagent_obs_handler"


def configure_logging(
    *,
    level: str = "INFO",
    fmt: str = "json",
    stream: Any = None,
    include_traceback: bool = False,
) -> logging.Logger:
    """把根 logger 配成「一个 handler + 结构化格式」。**幂等**。

    为什么接管**根** logger 而不是只配 ``lagent``：第三方库（uvicorn、httpx、
    sqlalchemy）的日志同样需要落进同一套格式，否则你会得到「一半 JSON、
    一半自由文本」的日志流 —— 而解析器遇到第一行非 JSON 就放弃了。
    它们的级别通过下面的降噪表单独压，而不是靠"不给它们 handler"。

    返回根 logger，方便测试直接断言 handler 上的 formatter 类型。
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(stream or sys.stderr)
    setattr(handler, _HANDLER_MARKER, True)
    if fmt == "text":
        handler.setFormatter(TextFormatter())
    else:
        handler.setFormatter(JsonFormatter(include_traceback=include_traceback))

    root.addHandler(handler)
    root.setLevel(_level_of(level))

    # 降噪：这几个库在 INFO 上会按请求打一行，把业务日志挤没了。
    # 刻意**不是** silence（设成 CRITICAL）—— 它们的 WARNING 有诊断价值。
    for name in ("uvicorn.access", "httpx", "httpcore", "aiosqlite"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return root


def _level_of(level: str) -> int:
    value = logging.getLevelName((level or "INFO").upper().strip())
    # getLevelName 对无法识别的名字会返回 "Level XXX" 这样的字符串，
    # 直接拿来 setLevel 会抛 ValueError —— 配置写错不该让服务起不来。
    return value if isinstance(value, int) else logging.INFO


_LOG_CONFIGURED = False


def configure_from_settings(*, force: bool = False) -> None:
    """按配置装配日志。入口（服务 / CLI）各调一次。

    ``force`` 给测试用：测试会反复改环境变量，需要能重新装配。
    """
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED and not force:
        return
    from .config import get_settings

    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        fmt=settings.log_format,
        include_traceback=settings.log_traceback,
    )
    _LOG_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """取一个 logger。统一入口便于以后加统一的处理。"""
    return logging.getLogger(name)
