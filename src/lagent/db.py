"""异步数据库接入层。

引擎与会话工厂都是模块级单例，但**支持按测试重置**：
    单元测试需要在每个用例里换一个独立的 SQLite 文件，
    如果引擎是硬编码的进程级单例，测试之间就会互相看到对方的数据。

结构变更走 alembic 迁移（``migrations/versions/``），不再靠 ``create_all``：
    ``create_all`` 只创建**缺失的表**、从不演进**已有的表**，所以「给 users
    加一列」这种最常见的改动在老库上永远不生效，库会停在半新半旧的状态。
    迁移之后，「这个库是什么结构」有了唯一答案 —— 它的 revision。

    这里的职责是把库带到 head，并**额外**校验「库结构 == 代码里的模型」。
    两者不是一回事：迁移保证「按 revision 升上来是对的」，保证不了
    「有人改了 models.py 却没生成迁移」。后者由 tests/test_migrations.py
    在 CI 里守住（比对迁移产物与模型的 diff 必须为空）。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import event, inspect, text
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
    "修法与原因：\n"
    "  A. python main.py seed --force     重建演示库（会清空现有数据）\n"
    "  B. 把 LAB_DATABASE_URL 指到一个新的库文件\n"
    "为什么会不一致：结构变更现在由 alembic 迁移维护（migrations/versions/）。\n"
    "  迁移能保证「按 revision 升上来的库是对的」，但它管不住\n"
    "  「有人改了 models.py 却没生成迁移」—— 那种情况下代码与库就会分叉。\n"
    "  这一条由 tests/test_migrations.py 在 CI 里守住：\n"
    "  它直接比对「迁移建出来的库」与「模型定义」，diff 必须为空。"
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


# ===========================================================================
# 迁移（alembic）
# ===========================================================================
_ALEMBIC_VERSION_TABLE = "alembic_version"


def _project_root() -> Path:
    """仓库根目录（``src/lagent/db.py`` 上溯三级）。"""
    return Path(__file__).resolve().parents[2]


def _alembic_config() -> Config:
    """构造迁移用的 alembic 配置。

    ``script_location`` 显式设置而不只依赖 alembic.ini：alembic 读 ini 用的是
    **locale 编码**（Windows 中文环境下是 GBK），所以那个文件必须保持纯 ASCII；
    万一它不存在或被改名，这条路径也要能跑。

    ``attributes["lagent_managed"]`` 是给 env.py 看的标记：程序化调用时
    不要自己往 ``alembic`` logger 挂 StreamHandler，否则迁移的纯文本日志
    会插进 app 的结构化日志流里。
    """
    cfg = Config(str(_project_root() / "alembic.ini"))
    cfg.set_main_option("script_location", str(_project_root() / "migrations"))
    cfg.attributes["lagent_managed"] = True
    return cfg


def head_revision() -> str:
    """代码里最新的 revision（记作 head）。"""
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    if head is None:  # pragma: no cover - 只会在 migrations/versions 被清空时发生
        raise RuntimeError("migrations/versions 下没有任何 revision，迁移无法进行")
    return head


def _current_revision_sync(connection) -> str | None:
    """同步实现（inspect 只有同步版），交给 ``run_sync`` 调用。"""
    if _ALEMBIC_VERSION_TABLE not in inspect(connection).get_table_names():
        return None
    row = connection.exec_driver_sql(
        f"SELECT version_num FROM {_ALEMBIC_VERSION_TABLE}"
    ).first()
    return None if row is None else str(row[0])


async def current_revision() -> str | None:
    """库当前的 revision。``None`` = 这个库还不归 alembic 管。"""
    async with get_engine().connect() as conn:
        return await conn.run_sync(_current_revision_sync)


async def revision_status() -> tuple[str | None, str]:
    """``(库当前 revision, 代码 head)``。doctor 与服务启动日志用。"""
    return await current_revision(), head_revision()


async def migrate(to: str = "head") -> str | None:
    """**前向**迁移到目标 revision，返回迁移后的 revision。

    进去之前先处理「老库」这一种形态。三种形态必须分开判，混在一起就会出现
    「看起来迁移了、其实没有」：

    * **空库**（新装 / 换了库文件）→ 直接 upgrade，建出全部表；
    * **有表但没有 alembic_version**（迁移引入之前建的库）：
        - 结构与模型一致 → ``stamp head``：只登记版本，一行 DDL 都不执行；
        - 结构不一致 → 抛 :class:`SchemaDriftError`。
          迁移的语义是「从一个已知结构往前走」，而一个不知道自己是哪个结构的库
          没有这个起点。硬升上去的报错是 ``table xxx already exists`` 加一屏
          SQLAlchemy 堆栈 —— 从那里看不出「你该重建库」。
    * **有 alembic_version** → 直接 upgrade，应用尚未执行的迁移。

    ⚠️ 这个判定必须在这一个函数里做，不能只在 ``init_db`` 里做：
    早先 ``main.py migrate`` 直接调 ``command.upgrade`` 绕过了它，
    于是老库上这条命令甩出的正是上面那段看不懂的堆栈。

    另外刻意只做 upgrade：``alembic upgrade base`` 是**静默无效**的
    （base 不是前向目的地），而「命令跑成功了、库却一点没变」正是本项目
    最不想留的失败形态。回退请显式用 :func:`downgrade`。
    """
    if await current_revision() is None and await _has_business_tables():
        drift = await schema_drift()
        if drift:
            raise SchemaDriftError(drift)
        await stamp("head")
        if to == "head":
            return await current_revision()

    # 必须丢到线程里跑：alembic 的 ``command.*`` 是同步 API，而 env.py 内部
    # 自己会 ``asyncio.run()`` 起事件循环 —— 在**已经在跑**的事件循环里直接
    # 调用会抛「asyncio.run() cannot be called from a running event loop」。
    cfg = _alembic_config()
    await asyncio.to_thread(command.upgrade, cfg, to)
    return await current_revision()


async def downgrade(to: str = "base") -> str | None:
    """**回退**到目标 revision（``base`` = 倒空，只留 alembic_version）。"""
    cfg = _alembic_config()
    await asyncio.to_thread(command.downgrade, cfg, to)
    return await current_revision()


async def stamp(revision: str = "head") -> None:
    """只登记版本号，**一行 DDL 都不执行**（用于接管升级前建好的老库）。"""
    cfg = _alembic_config()
    await asyncio.to_thread(command.stamp, cfg, revision)


async def _forget_version() -> None:
    """删掉 alembic_version 表。

    重建时必须做。``Base.metadata.drop_all()`` 不会碰这张表 ——
    它不在 metadata 里。版本号留着的话，随后的 ``upgrade head`` 会认为
    「已经是最新」而**什么都不做**，于是表真的没了而且**不报任何错**。
    这是「重建」这条路径上最容易漏掉的一步。
    """
    async with get_engine().begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {_ALEMBIC_VERSION_TABLE}"))


def _table_names_sync(connection) -> set[str]:
    return set(inspect(connection).get_table_names())


async def _has_business_tables() -> bool:
    async with get_engine().connect() as conn:
        names = await conn.run_sync(_table_names_sync)
    return bool(names & {table.name for table in Base.metadata.sorted_tables})


async def init_db(*, drop_first: bool = False) -> None:
    """把库带到「结构 = 代码 head」的状态。

    ``drop_first=True`` 是**真正的重建**：清掉表与版本号，再从 0001 升上来。

    注意 ``create_all`` 的确切语义（迁移之前本项目踩过的坑）：
    **只创建缺失的表，从不演进已有的表**。所以库一旦建立，之后再改 schema
    （加列 / 改类型）它一点都不会生效，旧库会一直停在老结构上。
    这正是引入 alembic 的原因 —— 迁移能「演进」，``create_all`` 不能。
    """
    if drop_first:
        async with get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await _forget_version()
    await migrate()


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
    """迁移到 head、校验结构与代码一致，不一致就抛 :class:`SchemaDriftError`。

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
