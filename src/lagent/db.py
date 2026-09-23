"""异步数据库接入层。

引擎与会话工厂都是模块级单例，但**支持按测试重置**：
    单元测试需要在每个用例里换一个独立的 SQLite 文件，
    如果引擎是硬编码的进程级单例，测试之间就会互相看到对方的数据。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import get_settings, reset_settings_cache
from .models import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# 结构与代码不一致时的修法提示。写在一处，是因为
# 「库坏了」这件事会从 doctor / seed / 服务启动三条路径报出来，
# 三处各写一份文案必然会漂移（本项目在振荡提示上已经吃过一次这个亏）。
_REBUILD_HINT = (
    "create_all() 只建缺失的**表**，不会给已有表补列 —— 真正的迁移能力是 P1 的 Alembic。\n"
    "两种修法（都会丢掉现有数据，演示库无所谓）：\n"
    "  A. python main.py seed --force   重建表并重灌种子数据\n"
    "  B. 把 LAB_DATABASE_URL 指到一个新的 sqlite 文件"
)


class SchemaDriftError(RuntimeError):
    """库的实际结构与代码里的模型不一致。

    单独定义异常类型而不是直接抛 RuntimeError，是为了让 ``doctor``
    这种「诊断工具」能把它识别成一条检查项来展示 —— 一个自称诊断环境的
    命令自己崩在 SQLAlchemy 堆栈里，是最没用的失败形态。
    """

    def __init__(self, drift: list[str]) -> None:
        self.drift = drift
        lines = "\n".join(f"  · {item}" for item in drift)
        super().__init__(f"库结构与代码不一致：\n{lines}\n{_REBUILD_HINT}")


def _engine_kwargs(url: str) -> dict:
    """SQLite 与 PostgreSQL 的建连参数完全不同，分开处理。

    SQLite 走 aiosqlite，不支持连接池大小参数；且必须显式设置
    ``check_same_thread=False`` 才能让异步驱动跨线程复用连接。
    """
    settings = get_settings()
    if url.startswith("sqlite"):
        return {
            "echo": settings.db_echo,
            # check_same_thread=False：异步驱动会跨线程复用连接。
            # timeout=30：sqlite3 驱动的忙等待上限（与下面 busy_timeout pragma 呼应）。
            "connect_args": {"check_same_thread": False, "timeout": 30},
        }
    return {
        "echo": settings.db_echo,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_pre_ping": True,
    }


def _install_sqlite_pragmas(engine: AsyncEngine) -> None:
    """SQLite 的三个关键 pragma —— 直接决定并发行为是否可解释：

    busy_timeout  写锁被别人占着时等待，而不是立刻抛 SQLITE_BUSY。
                  没有它，并发压测会把「正常的排队等待」误报成写入失败。
    journal_mode  WAL 模式允许「一写多读」，读请求不会被长写事务饿死。
    foreign_keys  打开外键约束，避免删掉设备后预约行变成孤儿。
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = get_settings().database_url
        _engine = create_async_engine(url, **_engine_kwargs(url))
        if url.startswith("sqlite"):
            _install_sqlite_pragmas(_engine)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _session_factory


async def dispose_engine() -> None:
    """释放引擎并清空单例。测试与进程退出时调用。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


async def init_db(*, drop_first: bool = False) -> None:
    """建表。``drop_first=True`` 时先删表再建 ——「重建」的真正含义。

    SQLite 本地开发够用；生产用 alembic（见 docker-compose）。

    注意 ``create_all`` 的确切语义：**只创建缺失的表，从不演进已有的表**。
    所以库一旦建立，之后再改 schema（加列/改类型）它一点都不会生效，
    旧库会一直停在老结构上。这一点由 :func:`ensure_schema` 负责检测并报错，
    修法是 ``drop_first=True`` 重建（或换一个库文件）。
    """
    engine = get_engine()
    async with engine.begin() as conn:
        if drop_first:
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


def _drift_sync(conn) -> list[str]:
    """同步实现，交给 ``run_sync`` 调用（inspect 只有同步版）。"""
    inspector = inspect(conn)
    existing = set(inspector.get_table_names())
    drift: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            drift.append(f"缺表 {table.name}")
            continue
        have = {column["name"] for column in inspector.get_columns(table.name)}
        missing = [column.name for column in table.columns if column.name not in have]
        if missing:
            drift.append(f"表 {table.name} 缺列：{', '.join(missing)}")
    return drift


async def schema_drift() -> list[str]:
    """列出「模型里有、库里没有」的表与列。空列表 = 结构与代码一致。"""
    async with get_engine().connect() as conn:
        return await conn.run_sync(_drift_sync)


async def ensure_schema(*, rebuild: bool = False) -> None:
    """建表、校验结构与代码一致，不一致就抛 :class:`SchemaDriftError`。

    为什么值得单独做一次校验，而不是等业务代码崩了再说：
    漂移的失败形态**又难懂又不一致**。库里缺 ``users.password_hash`` 时，

      * 报错是 ``no such column: users.password_hash`` 外加五十行 SQLAlchemy 堆栈，
        看不出「你该重建库」；
      * 更糟的是**只有走到那一步的路径才会失败** —— 「你好」这种寒暄根本不查
        users 表，于是服务看起来是好的，一到真下单才炸。

    这种「部分可用」比直接报错更难排查，所以在边界上一次性检查掉。
    """
    await init_db(drop_first=rebuild)
    drift = await schema_drift()
    if drift:
        raise SchemaDriftError(drift)


@asynccontextmanager
async def isolated_database(url: str) -> AsyncIterator[str]:
    """临时把数据库切到**另一个库**，退出时还原。

    评测与压测必须跑在干净且专属的库上，原因不是洁癖：
    这两条命令自己会下单（评测用例期望 booked、压测要写入 40 条并发请求），
    跑在开发库上就会「第一次跑通、第二次结论变了」——
    同一份代码两次给出不同通过率，那这个数字就完全不能作为证据。

    实测踩过：手工在控制台点了一单，占用了评测 c02 需要的那个时段，
    于是评测从 14/14 掉到 13/14，看起来像代码回归，其实只是脏数据。
    """
    previous = os.environ.get("LAB_DATABASE_URL")
    os.environ["LAB_DATABASE_URL"] = url
    reset_settings_cache()
    await dispose_engine()
    try:
        yield url
    finally:
        await dispose_engine()
        if previous is None:
            os.environ.pop("LAB_DATABASE_URL", None)
        else:
            os.environ["LAB_DATABASE_URL"] = previous
        reset_settings_cache()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """在 HTTP 请求生命周期之外开一个独立会话。

    这一点很关键：Agent 的执行链路可能长于一次请求（多轮追问、重试），
    而 FastAPI 注入的 ``Depends(get_db)`` 会在请求返回时关闭会话，
    工具函数里再去用就会撞上「session is closed」。
    所以工具函数一律用这个上下文自己开新会话。
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖注入用（只读路径）。"""
    async with session_scope() as session:
        yield session
