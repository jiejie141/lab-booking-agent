"""库结构漂移：检测、报错可读性，以及「恢复命令真的能恢复」。

## 背景（真实踩过，不是假想场景）

P0-2 给 ``users`` 加了 ``password_hash`` 列，P0-3 加了 ``audit_logs`` 表。
``create_all()`` 只创建**缺失的表**，从不演进**已有的表** —— 于是一个升级前
建立的开发库会停在「半新半旧」状态：``audit_logs`` 建出来了，``users`` 却缺列。

坑不在「缺列」本身（那是已知限制，README 写了），而在**三个失败形态**：

1. 报错是 ``no such column: users.password_hash`` 外加五十行 SQLAlchemy 堆栈，
   看不出「你该重建库」。
2. **只有查 users 的路径才失败** —— 「你好」这种寒暄根本不查 users 表，
   于是服务看起来是好的，一到真下单才炸。这种「部分可用」比直接报错更难查。
3. 最糟的一条：README 提示的修法 ``python main.py seed --force`` **自己也会炸**，
   因为当时它只是「把行删空」，不重建表，所以修不了缺列。用户按文档操作，
   拿到的是**另一个同样看不懂的报错**。

本文件把这三件事一起钉住，尤其是第 3 条 —— 文档给出的恢复路径必须真的可用。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

# P0-2 之前的 users 结构：没有 password_hash 列。
# 与「升级前建的库」等价，用来稳定复现漂移。
OLD_USERS_DDL = """
CREATE TABLE users (
    id INTEGER NOT NULL PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    email VARCHAR(128) NOT NULL,
    role VARCHAR(16) NOT NULL,
    certs JSON
)
"""


@pytest.fixture
async def stale_db(tmp_path, monkeypatch):
    """一个「表都在、但 users 缺 password_hash」的旧库。

    刻意先把全部表建出来、**只**把 users 换成旧结构：
    这样漂移就是精确的「缺列」，不会混进「缺表」，断言才能说明问题。
    """
    url = f"sqlite+aiosqlite:///{(tmp_path / 'stale.db').as_posix()}"
    monkeypatch.setenv("LAB_DATABASE_URL", url)
    monkeypatch.setenv("LAB_APP_MODE", "mock")
    # scrypt 默认 n=2**14 ≈ 140ms/次，测试里降到 2**10（成本参数不是被测对象）
    monkeypatch.setenv("LAB_PASSWORD_KDF_N", "1024")

    from lagent import db as db_module
    from lagent.config import reset_settings_cache

    reset_settings_cache()
    await db_module.dispose_engine()
    await db_module.init_db()

    engine = db_module.get_engine()
    async with engine.begin() as conn:
        # 库里还没有任何行，所以直接删父表不会触发外键（PRAGMA foreign_keys=ON）
        await conn.execute(text("DROP TABLE users"))
        await conn.execute(text(OLD_USERS_DDL))

    yield db_module

    await db_module.dispose_engine()
    reset_settings_cache()


@pytest.fixture
async def fresh_db(tmp_path, monkeypatch):
    """干净库，用来确认校验本身不会误报。"""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'fresh.db').as_posix()}"
    monkeypatch.setenv("LAB_DATABASE_URL", url)
    monkeypatch.setenv("LAB_PASSWORD_KDF_N", "1024")

    from lagent import db as db_module
    from lagent.config import reset_settings_cache

    reset_settings_cache()
    await db_module.dispose_engine()
    await db_module.init_db()

    yield db_module

    await db_module.dispose_engine()
    reset_settings_cache()


class TestDriftDetection:
    async def test_detects_the_missing_column(self, stale_db):
        drift = await stale_db.schema_drift()
        assert len(drift) == 1, f"应只报一处缺列，实得 {drift}"
        assert "users" in drift[0] and "password_hash" in drift[0]

    async def test_fresh_schema_has_no_drift(self, fresh_db):
        """反面对照：校验不能误报 —— 否则每次启动都会拦下正常用户。"""
        assert await fresh_db.schema_drift() == []

    async def test_ensure_schema_raises_a_readable_error(self, stale_db):
        from lagent.db import SchemaDriftError

        with pytest.raises(SchemaDriftError) as caught:
            await stale_db.ensure_schema()

        message = str(caught.value)
        # 缺哪一列要说清
        assert "password_hash" in message
        # 而且必须给出**能照着做**的修法，不能只说「不一致」
        assert "seed --force" in message
        assert "alembic" in message, "要解释结构由谁维护，否则用户会以为是 bug"

    async def test_drift_is_exposed_as_data_not_only_an_exception(self, stale_db):
        """doctor 这类诊断工具需要的是「数据」而不是异常，否则它自己会崩。"""
        from lagent.db import SchemaDriftError

        with pytest.raises(SchemaDriftError) as caught:
            await stale_db.ensure_schema()
        assert caught.value.drift == await stale_db.schema_drift()


class TestRecovery:
    """README 给出的恢复路径必须真的能用 —— 这一组就是它曾经的回归点。"""

    async def test_seed_force_rebuilds_tables_not_only_rows(self, stale_db):
        from lagent.seed import seed

        assert await stale_db.schema_drift() != []
        await seed(force=True)
        # 关键断言：修完之后结构必须与代码一致（原来「只删行」的实现在这里会红）
        assert await stale_db.schema_drift() == []

    async def test_recovered_database_is_actually_usable(self, stale_db):
        """不只结构对齐，还要真的能登录 —— 证明补出来的列里有正确数据。"""
        from sqlalchemy import select

        from lagent.db import session_scope
        from lagent.models import User
        from lagent.security import verify_password
        from lagent.seed import seed

        await seed(force=True)
        async with session_scope() as session:
            user = (await session.execute(select(User).where(User.username == "张伟"))).scalar_one()
        assert user.password_hash, "重建后 password_hash 不能为空"
        assert verify_password("zhangwei@123", user.password_hash)

    async def test_seed_without_force_refuses_instead_of_half_working(self, stale_db):
        """不带 --force 时也要明确报错，而不是「跳过」了事。

        原来它会打印「已有 3 个实验室，跳过」然后一切照常返回 0 ——
        库仍然是坏的，用户以为已经好了。这是最坏的一种失败。
        """
        from lagent.db import SchemaDriftError
        from lagent.seed import seed

        with pytest.raises(SchemaDriftError):
            await seed(force=False)


class TestDoctorDiagnosesInsteadOfCrashing:
    async def test_doctor_reports_drift_and_exits_nonzero(self, stale_db, capsys):
        """自检命令的职责是把这种情况翻译成人话，它自己崩掉是最没用的失败。"""
        from lagent.cli import _doctor

        code = await _doctor()
        out = capsys.readouterr().out

        assert code == 1, "库坏了自检就不能报成功"
        assert "数据库结构" in out
        assert "password_hash" in out, "要说清缺的是哪一列"
        assert "seed --force" in out, "要给出修法"
        assert "Traceback" not in out, "不该是异常堆栈"
