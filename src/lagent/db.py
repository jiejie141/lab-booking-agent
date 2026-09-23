"""异步数据库接入层。

引擎与会话工厂都是模块级单例，但**支持按测试重置**：
    单元测试需要在每个用例里换一个独立的 SQLite 文件，
    如果引擎是硬编码的进程级单例，测试之间就会互相看到对方的数据。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
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


async def init_db() -> None:
    """建表。SQLite 本地开发够用；生产用 alembic（见 docker-compose）。"""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


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
