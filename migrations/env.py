"""Alembic 迁移环境。

两个刻意的设计决定，都是为了消除「迁移能跑、服务跑不起来」这类不一致：

1. **连哪个库只由 app 配置决定**（``LAB_DATABASE_URL``），不认 alembic.ini。
   迁移升的库必须就是服务连的库，两处各配一份 URL 迟早就对不上。

2. **用异步驱动直接跑迁移**，不为迁移单独引入同步驱动（psycopg2）。
   本项目跑 aiosqlite / asyncpg；如果迁移走同步驱动，
   就会出现「迁移说成功、服务连不上」这种由两种驱动差异造成的问题 ——
   而这类问题最容易在「自以为验证过了」的时候出现。
   alembic 官方的 async 模板正是为此提供的，这里照做。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# 让 env.py 在「被 lagent 程序化调用」和「仓库根目录直接跑 alembic」两种
# 情况下都能 import 到 lagent。（alembic.ini 里也有 prepend_sys_path，
# 但那条依赖 cwd；这一条依赖文件位置，更稳。）
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:  # pragma: no cover - 取决于调用方式
    sys.path.insert(0, str(_SRC))

# 这两条必须在 sys.path 处理**之后**才能 import（E402），
# 而且 noqa 不能换成 pyproject 里的 per-file-ignores：
# 一旦 E402 被配置忽略，这里的 noqa 就变成「多余的抑制指令」，
# 反过来会被 RUF100 抓住 —— 两种写法互为陷阱，只能用 noqa。
from lagent.config import get_settings  # noqa: E402
from lagent.models import Base  # noqa: E402

config = context.config

# 日志：不调用 logging.config.fileConfig()。
#
# alembic.ini 的日志段里 `%(levelname)-5.5s` 这类格式串与 configparser 的插值
# 规则相互作用，是个容易踩的坑；这里改成只给 alembic 这一个 logger 挂 handler，
# 精确且不影响别的库。否则 `alembic upgrade head` 会**静默成功** ——
# 运维时看不到「正在升到哪个 revision」，这比日志难看严重得多。
#
# 程序化调用（lagent.db.migrate → command.upgrade）时不挂：
# 那条路径的输出由 app 自己的日志配置管（P1-3 起是 JSON），
# 在这里塞一行纯文本会把结构化日志流打断。
#
# 注意守卫条件不能用 `if not _logger.handlers`：alembic 自己给这个 logger
# 挂了一个 NullHandler（库的常规做法，避免「no handler」告警），
# 于是那个判断永远为假，handler 挂不上去、升级全程静默。
# 用一个自定义标记位表达「我们挂过了」，与库里已有的 handler 无关。
if not config.attributes.get("lagent_managed") and not getattr(
    logging.getLogger("alembic"), "_lagent_cli_handler", False
):
    _logger = logging.getLogger("alembic")
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)-5.5s [%(name)s] %(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)
    _logger._lagent_cli_handler = True  # type: ignore[attr-defined]

target_metadata = Base.metadata


def _database_url() -> str:
    """迁移连的库 = 服务连的库。"""
    return get_settings().database_url


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # 开了它，`alembic check` / autogenerate 才认得「列类型被改了」。
        # 关着的话改类型不会生成迁移，问题只会在生产上才暴露。
        compare_type=True,
        # SQLite 没有 ALTER COLUMN，每次结构变更必须走「建新表 + 拷数据」的
        # batch 模式，否则任何一次 alter 都会在 SQLite 上直接失败。
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    # NullPool：迁移是一次性短命连接，用连接池只是徒增一个要释放的东西。
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql`：只吐 SQL，不连库（发给 DBA 复核用）。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
