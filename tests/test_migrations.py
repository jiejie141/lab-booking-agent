"""迁移（alembic）：让「库结构」这件事有唯一答案。

## 为什么这个文件比它看起来重要

功能测试**只能覆盖它走到的那几列**。迁移漏掉一列、丢了一个索引的 WHERE 子句，
功能测试很可能全绿 —— 尤其是部分唯一索引：少了 WHERE，索引退化成「无条件唯一」，
单线程下一切正常，只有并发压测或「取消后重订」才会暴露。

所以这里不测业务，只测**迁移产物本身**：

1. 「迁移建出来的库」与 `models.py` 的 diff 必须为**空**（地基）；
2. 三条靠唯一索引兜住的不变式，其 DDL 必须与模型声明逐字一致；
3. 重建路径（`seed --force`）真的把表和版本号都重建了；
4. 老库（有表、无版本号）走「接管」而不是「猜」；
5. `alembic.ini` 保持纯 ASCII —— 这条不是洁癖，是 Windows 上的启动崩溃点。

第 1 条是整套机制的地基：**如果它红了，说明有人改了 models.py 却没生成迁移**，
此时其余所有测试的通过都不可信。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _sync_url(async_url: str) -> str:
    """``sqlite+aiosqlite:///x`` → ``sqlite:///x``。

    比对结构与核对 DDL 都不需要异步：SQLite 的同步驱动是内建的，
    用同步连接可以把 alembic 的 `compare_metadata` 直接用起来（它只有同步版）。
    """
    return async_url.replace("sqlite+aiosqlite", "sqlite", 1)


def _sqlite_path(async_url: str) -> Path:
    return Path(_sync_url(async_url).split("///", 1)[1])


def _metadata_diff(async_url: str) -> list:
    """库 vs 模型的差异清单（空 = 一致）。"""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from lagent.models import Base

    engine = create_engine(_sync_url(async_url))
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(conn, opts={"compare_type": True})
            return list(compare_metadata(context, Base.metadata))
    finally:
        engine.dispose()


def _index_sql(async_url: str, name: str) -> str:
    """取某个索引的真实 DDL（空白已归一化，便于逐字断言）。"""
    conn = sqlite3.connect(_sqlite_path(async_url).as_posix())
    try:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    finally:
        conn.close()
    assert row is not None, f"库里根本没有索引 {name}"
    return " ".join(str(row[0]).split())


def _table_names(async_url: str) -> set[str]:
    conn = sqlite3.connect(_sqlite_path(async_url).as_posix())
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        conn.close()
    return {str(r[0]) for r in rows}


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------
def _prepare(monkeypatch, tmp_path, name: str) -> str:
    """换一个独立的库文件并重置单例。返回 async URL。"""
    url = f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}"
    monkeypatch.setenv("LAB_DATABASE_URL", url)
    monkeypatch.setenv("LAB_APP_MODE", "mock")
    monkeypatch.setenv("LAB_PASSWORD_KDF_N", "1024")
    return url


@pytest.fixture
async def migrated_db(tmp_path, monkeypatch):
    """已迁移到 head 的空库（无种子数据）。"""
    url = _prepare(monkeypatch, tmp_path, "migrated.db")

    from lagent import db as db_module
    from lagent.config import reset_settings_cache

    reset_settings_cache()
    await db_module.dispose_engine()
    await db_module.init_db()

    yield db_module, url

    await db_module.dispose_engine()
    reset_settings_cache()


@pytest.fixture
async def empty_db(tmp_path, monkeypatch):
    """连表都还没有的库（文件可能都不存在）。"""
    url = _prepare(monkeypatch, tmp_path, "empty.db")

    from lagent import db as db_module
    from lagent.config import reset_settings_cache

    reset_settings_cache()
    await db_module.dispose_engine()

    yield db_module, url

    await db_module.dispose_engine()
    reset_settings_cache()


@pytest.fixture
async def legacy_db(migrated_db):
    """模拟「迁移引入之前建的库」：表齐全，但没有 alembic_version。

    做法的等价性：先正常升到 head，再把版本表删掉 —— 结构于是与迁移产物
    完全一致，但库自己不知道自己是哪个 revision。这正是老库的真实形态。
    """
    db_module, url = migrated_db
    async with db_module.get_engine().begin() as conn:
        await conn.execute(text("DROP TABLE alembic_version"))
    assert await db_module.current_revision() is None
    yield db_module, url


# ==========================================================================
# 1. 地基：迁移产物 == 模型
# ==========================================================================
class TestMigrationsMatchModels:
    async def test_migrated_schema_is_identical_to_models(self, migrated_db):
        """★ 核心断言：跑完迁移后，库与 models.py 的 diff 必须为空。

        这一条红了，说明「有人改了 models.py 却没生成迁移」。
        此时**其余测试的通过都不可信** —— 功能测试只会走到它用到的那几列。
        """
        _, url = migrated_db
        diff = _metadata_diff(url)
        assert diff == [], f"迁移产物与模型不一致，请补一条 revision：{diff}"

    async def test_all_business_tables_are_created(self, migrated_db):
        """十张表一张不少 —— 防的是「迁移只写了一半」。"""
        from lagent.models import Base

        _, url = migrated_db
        expected = {table.name for table in Base.metadata.sorted_tables}
        assert len(expected) == 10, f"模型里的表数变了（{sorted(expected)}），本断言要同步更新"
        assert expected <= _table_names(url)

    async def test_compare_type_is_actually_enabled(self, migrated_db):
        """反面对照：把某一列的类型改错，diff 必须能看出来。

        没有这条，「无差异」有可能只是因为比对根本没开 —— 断言恒真。
        """
        _, url = migrated_db
        engine = create_engine(_sync_url(url))
        try:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE laboratories RENAME TO laboratories_backup")
                )
                conn.execute(
                    text(
                        "CREATE TABLE laboratories ("
                        " id INTEGER NOT NULL PRIMARY KEY,"
                        " building VARCHAR(32) NOT NULL,"
                        " floor INTEGER NOT NULL,"
                        " room VARCHAR(32) NOT NULL,"
                        # 故意改成比模型窄的类型
                        " capacity VARCHAR(4) NOT NULL,"
                        " open_hours JSON NOT NULL,"
                        " note TEXT NOT NULL)"
                    )
                )
        finally:
            engine.dispose()

        diff = _metadata_diff(url)
        assert diff, "改了列类型却没有产生 diff —— 说明 compare_type 没生效"


# ==========================================================================
# 2. 部分唯一索引的 WHERE 子句必须活着
# ==========================================================================
class TestPartialIndexesSurviveMigration:
    """★ 这三条索引是并发正确性的**唯一**保证，且退化后功能测试看不出来。

    * ``uq_res_active_slot``  少了 WHERE → 索引变成无条件唯一，
      「取消之后的时段永远订不回来」；
    * ``uq_permit_one_inside`` 少了 WHERE → 一个人能凭多条凭证同时在馆，
      在馆人数把他算两遍，容量约束随之失真；
    * ``uq_lab_slot_seat`` 少了唯一 → 房间被塞爆。
    """

    async def test_active_reservation_index_stays_partial(self, migrated_db):
        _, url = migrated_db
        sql = _index_sql(url, "uq_res_active_slot")
        assert "UNIQUE" in sql
        assert "WHERE status IN ('pending','confirmed')" in sql, sql

    async def test_one_inside_index_stays_partial(self, migrated_db):
        _, url = migrated_db
        sql = _index_sql(url, "uq_permit_one_inside")
        assert "UNIQUE" in sql
        assert "WHERE status = 'checked_in'" in sql, sql

    async def test_slot_and_seat_indexes_are_unconditional(self, migrated_db):
        """反面对照：这两条**不该**有 WHERE（它们要覆盖全部状态）。"""
        _, url = migrated_db
        slot = _index_sql(url, "uq_equipment_slot")
        seat = _index_sql(url, "uq_lab_slot_seat")
        assert "UNIQUE" in slot and "WHERE" not in slot
        assert "UNIQUE" in seat and "WHERE" not in seat

    async def test_partial_index_still_lets_a_cancelled_slot_be_rebooked(self, migrated_db):
        """把 DDL 断言翻译成**行为**：取消后那个时段必须能再订。

        只断言 DDL 文本有可能过拟合（比如把 WHERE 写成等价的另一种形式），
        所以补一条真正走数据库的行为验证。
        """
        import datetime as dt

        from sqlalchemy import update
        from sqlalchemy.exc import IntegrityError

        from lagent.db import session_scope
        from lagent.models import STATUS_CANCELLED, Equipment, Laboratory, Reservation, User

        _, _ = migrated_db
        async with session_scope() as session:
            user = User(username="t", email="t@example.com", role="user")
            lab = Laboratory(building="测试楼", floor=1, room="101", capacity=1, open_hours={})
            session.add_all([user, lab])
            await session.flush()
            equip = Equipment(lab_id=lab.id, name="测试仪", code="T-1", category="测试")
            session.add(equip)
            await session.flush()
            user_id, equip_id = user.id, equip.id

        day = dt.date(2030, 1, 1)

        async def book() -> int:
            async with session_scope() as session:
                res = Reservation(
                    user_id=user_id,
                    equipment_id=equip_id,
                    date=day,
                    start_time=dt.time(10, 0),
                    end_time=dt.time(11, 0),
                    status="confirmed",
                )
                session.add(res)
                await session.flush()
                return res.id

        first_id = await book()

        # 同一个坑、同一个开始时间再来一次：必须被部分唯一索引拦下
        with pytest.raises(IntegrityError):
            await book()

        # 取消之后必须能订回来 —— 这正是「部分」的唯一性要保住的东西。
        # 如果索引退化成无条件唯一，这里就会再次 IntegrityError。
        async with session_scope() as session:
            await session.execute(
                update(Reservation)
                .where(Reservation.id == first_id)
                .values(status=STATUS_CANCELLED)
            )

        assert await book() != first_id


# ==========================================================================
# 3. 重建路径
# ==========================================================================
class TestRebuildPath:
    async def test_rebuild_brings_tables_back_and_sets_the_revision(self, migrated_db):
        """★ 重建：drop 之后再迁移，表要真的回来、版本号也要对。

        针对一个特别容易漏的步骤：``Base.metadata.drop_all()`` **不会**删
        ``alembic_version``（它不在 metadata 里）。漏了那一步，随后的
        ``upgrade head`` 会认为「已经是最新」而什么都不做 ——
        结果是表全没了，而且**不报任何错**。
        """
        db_module, _ = migrated_db
        await db_module.init_db(drop_first=True)

        assert await db_module.schema_drift() == []
        assert await db_module.current_revision() == db_module.head_revision()

    async def test_rebuild_twice_still_lands_on_head(self, migrated_db):
        """连做两次重建也要稳定 —— 防「第一次对、第二次空白」。"""
        db_module, url = migrated_db
        await db_module.init_db(drop_first=True)
        await db_module.init_db(drop_first=True)
        from lagent.models import Base

        assert {t.name for t in Base.metadata.sorted_tables} <= _table_names(url)
        assert await db_module.current_revision() == db_module.head_revision()


# ==========================================================================
# 4. 老库的两种命运
# ==========================================================================
class TestLegacyAdoption:
    async def test_legacy_database_is_adopted_without_losing_data(self, legacy_db):
        """有表、无版本号、结构一致 → ``stamp head`` 接管：不动结构、不动数据。

        「不动数据」不能靠读代码相信，这里塞一行再读回来。
        """
        db_module, url = legacy_db
        from lagent.db import session_scope
        from lagent.models import Laboratory

        async with session_scope() as session:
            session.add(Laboratory(building="老楼", floor=9, room="999", capacity=3, open_hours={}))

        await db_module.ensure_schema()

        assert await db_module.current_revision() == db_module.head_revision()
        async with session_scope() as session:
            rows = (await session.execute(Laboratory.__table__.select())).fetchall()
        assert len(rows) == 1, "接管不该动数据"
        assert _table_names(url) >= {"users", "reservations", "access_events"}

    async def test_legacy_database_with_drift_refuses_instead_of_guessing(self, legacy_db):
        """结构不一致的老库：**明确报错**，而不是硬猜一个起点。

        迁移的语义是「从一个已知结构往前走」。一个不知道自己是哪个结构的库
        没有这个起点，硬 stamp 只会得到一个更难查的坏库。
        """
        db_module, _ = legacy_db
        async with db_module.get_engine().begin() as conn:
            await conn.execute(text("DROP TABLE access_events"))

        from lagent.db import SchemaDriftError

        with pytest.raises(SchemaDriftError) as caught:
            await db_module.ensure_schema()
        assert any("access_events" in item for item in caught.value.drift)

    async def test_migrate_on_a_drifted_legacy_database_also_refuses(self, legacy_db):
        """★ ``migrate()`` 也必须走接管判定，不能直接甩 SQLAlchemy 堆栈。

        早先 ``main.py migrate`` 直接调 ``command.upgrade`` 绕过了这个判定，
        于是老库上抛出来的是 ``table audit_logs already exists`` 加一屏堆栈 ——
        从那里完全看不出「你该重建库」。**这是真实在开发库上踩到的**，
        所以两个入口（``ensure_schema`` / ``migrate``）都要在断言里各钉一次。
        """
        db_module, _ = legacy_db
        async with db_module.get_engine().begin() as conn:
            await conn.execute(text("DROP TABLE access_events"))

        from lagent.db import SchemaDriftError

        with pytest.raises(SchemaDriftError):
            await db_module.migrate("head")

    async def test_migrate_on_a_healthy_legacy_database_adopts_it(self, legacy_db):
        """反面对照：结构一致的老库，``migrate()`` 应当接管而不是报错。"""
        db_module, _ = legacy_db
        assert await db_module.migrate("head") == db_module.head_revision()
        assert await db_module.schema_drift() == []


# ==========================================================================
# 5. alembic.ini 必须纯 ASCII（Windows 上的启动崩溃点）
# ==========================================================================
class TestAlembicIniEncoding:
    def test_ini_is_pure_ascii(self):
        """★ alembic 读 ini 用的是 **locale 编码**。

        `alembic/util/compat.py` 里是 ``read_config_parser(..., encoding="locale")``。
        Windows 中文环境的 locale 是 GBK，ini 里出现任何非 ASCII 字节，
        alembic 会在**读到配置之前**就抛 ``UnicodeDecodeError`` ——
        报错位置（configparser）离真正的原因（文件里有个中文字）很远，很难查。

        所以中文说明只能放在 migrations/README.md 里。
        """
        raw = (ROOT / "alembic.ini").read_bytes()
        try:
            raw.decode("ascii")
        except UnicodeDecodeError as exc:
            pytest.fail(f"alembic.ini 含非 ASCII 字节：{exc}；中文说明请写进 migrations/README.md")

    def test_no_sqlalchemy_url_is_pinned_in_the_ini(self):
        """ini 里不该有第二个数据库 URL。

        迁移升的库必须就是服务连的库。两处各配一份，迟早就出现
        「迁移升了 A 库、服务连着 B 库」—— 而且这种错不报错，只是行为诡异。
        """
        for line in (ROOT / "alembic.ini").read_text(encoding="ascii").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("["):
                continue
            assert not stripped.startswith("sqlalchemy.url"), "URL 只允许在 env.py 里读一处"


# ==========================================================================
# 6. 命令行入口
# ==========================================================================
class TestMigrateCommand:
    async def test_migrate_from_empty_reports_the_transition(self, empty_db, capsys):
        from lagent.cli import _migrate

        db_module, _ = empty_db
        assert await _migrate("head") == 0
        out = capsys.readouterr().out
        assert "代码 head" in out
        assert "→" in out, f"要显示 before → after，便于运维确认：{out}"
        assert await db_module.schema_drift() == []

    async def test_migrate_twice_is_idempotent(self, migrated_db, capsys):
        """已经是 head 时要明确说「无需迁移」，而不是静默成功。"""
        from lagent.cli import _migrate

        capsys.readouterr()
        assert await _migrate("head") == 0
        out = capsys.readouterr().out
        assert "已是最新" in out, out

    async def test_upgrade_base_is_rejected_instead_of_silently_doing_nothing(
        self, migrated_db, capsys
    ):
        """★ 方向搞反时必须**明确报错**。

        `alembic upgrade base` 是静默无效的（base 不是前向目的地）：
        命令返回成功、库一点没变。这种「跑了但什么也没发生」是最难查的失败形态，
        所以在 CLI 层直接拦下来。
        """
        from lagent.cli import _migrate

        db_module, url = migrated_db
        capsys.readouterr()
        assert await _migrate("base") == 2
        out = capsys.readouterr().out
        assert "--down" in out
        # 库必须原样不动
        assert await db_module.current_revision() == db_module.head_revision()
        assert len(_table_names(url)) > 5

    async def test_downgrade_to_base_removes_every_business_table(self, migrated_db):
        """downgrade 必须真的能把库倒空 —— 否则「可回滚」只是说法。"""
        db_module, url = migrated_db
        await db_module.downgrade("base")
        assert _table_names(url) == {"alembic_version"}
        assert await db_module.current_revision() is None

    async def test_downgrade_then_upgrade_round_trips(self, migrated_db):
        """回滚再升回来，结构与模型仍须一致（防 downgrade 写漏了东西）。"""
        db_module, url = migrated_db
        await db_module.downgrade("base")
        await db_module.migrate("head")
        assert _metadata_diff(url) == []


# ==========================================================================
# 7. 第二条 revision：给**已经有数据**的表加列
# ==========================================================================
def _columns(url: str, table: str) -> set[str]:
    conn = sqlite3.connect(_sqlite_path(url).as_posix())
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _index_names(url: str, table: str) -> set[str]:
    conn = sqlite3.connect(_sqlite_path(url).as_posix())
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA index_list({table})")}
    finally:
        conn.close()


class TestSecondRevisionOnPopulatedTables:
    """0002 的真实考验：给**已经有行**的表加列 + 建索引。

    为什么这一组必须单独存在：``0001`` 是从空库 autogenerate 出来的，
    而**空表上"加一列"永远不会失败** —— 它证明不了任何事。
    真正会出事的是这三件事凑在一起：

    1. 表里已经有行（ALTER TABLE 要在保留数据的前提下改结构）；
    2. 新列是 ``NOT NULL``（老行必须拿到一个确定的默认值，不能是 NULL）；
    3. 同时要建索引（顺序错了会先在缺列的表上建索引而失败）。

    所以这里的做法是：**先退到 0001**（那时还没有 request_id 列），
    灌进几行"历史数据"，再升到 head，然后逐项核对
    「数据还在吗 / 老行的新列是什么值 / 索引建出来了吗」。

    最后再退一次、升一次，确认这个来回**不会吃掉数据** ——
    "能回滚"只有在这种情况下才算数。
    """

    async def _seed_rows_at_0001(self, url: str) -> None:
        """在 0001 的结构上插两行历史数据。

        **刻意用裸 sqlite3，不走 ORM，也不走应用的引擎**：

        1. ORM 已经认识了 ``request_id``，用它插入等于让今天的代码去写昨天的结构，
           那样测的就不是迁移；
        2. 应用的引擎在 connect 时打开了 ``PRAGMA foreign_keys=ON``，
           要插一条 ``access_events`` 就得先把 laboratory / entry_permit
           整条依赖链造出来 —— 而这条用例要验的是"表里有行"，
           不是外键约束。裸连接的 SQLite 默认**不开**外键，正合适。
        """
        conn = sqlite3.connect(_sqlite_path(url).as_posix(), timeout=10)
        try:
            conn.execute(
                "INSERT INTO audit_logs "
                "(created_at, actor_id, actor_name, action, target_type, "
                " target_id, outcome, detail, client_host) "
                "VALUES (?, ?, ?, ?, '', '', 'ok', ?, '')",
                ("2026-09-01 09:00:00", 1, "张伟", "auth.login", "迁移前写下的历史审计"),
            )
            conn.execute(
                "INSERT INTO access_events "
                "(occurred_at, user_id, lab_id, gate_id, direction, result, "
                " reason_code, permit_id, credential_fingerprint, detail) "
                "VALUES (?, 1, 1, 'gate-01', 'in', 'granted', '', 1, 'fp', '')",
                ("2026-09-01 09:05:00",),
            )
            conn.commit()
        finally:
            conn.close()

    async def _counts(self, db_module) -> tuple[int, int]:
        from lagent.db import session_scope

        async with session_scope() as session:
            audits = (await session.execute(text("SELECT count(*) FROM audit_logs"))).scalar()
            events = (await session.execute(text("SELECT count(*) FROM access_events"))).scalar()
        return int(audits or 0), int(events or 0)

    async def test_adding_request_id_keeps_existing_rows(self, migrated_db):
        db_module, url = migrated_db

        # --- 退到 0001：确认那时确实没有这两列（否则下面的断言就是空的）---
        await db_module.downgrade("0001")
        assert await db_module.current_revision() == "0001"
        assert "request_id" not in _columns(url, "audit_logs")
        assert "request_id" not in _columns(url, "access_events")

        # --- 灌历史数据 ---
        await self._seed_rows_at_0001(url)
        assert await self._counts(db_module) == (1, 1)
        assert "ix_audit_request" not in _index_names(url, "audit_logs")

        # --- 升到 head（这一步就是"给有数据的表加列"）---
        await db_module.migrate("head")

        assert await db_module.current_revision() == db_module.head_revision()
        # ① 数据一行都不能少
        assert await self._counts(db_module) == (1, 1)
        # ② 老行的新列拿到的是**空串**而不是 NULL
        from lagent.db import session_scope

        async with session_scope() as session:
            row = (
                await session.execute(
                    text("SELECT action, actor_name, detail, request_id FROM audit_logs")
                )
            ).one()
        assert row[0] == "auth.login"  # 老数据原样保留
        assert row[1] == "张伟"
        assert row[2] == "迁移前写下的历史审计"
        assert row[3] == "", f"老行的 request_id 应当是空串，实际是 {row[3]!r}"

        # ③ 索引也建出来了（两列都建）
        assert "ix_audit_request" in _index_names(url, "audit_logs")
        assert "ix_access_request" in _index_names(url, "access_events")
        # ④ 结构与模型完全一致 —— 手写的迁移最容易在这里露馅
        assert _metadata_diff(url) == []

    async def test_round_trip_through_0002_does_not_eat_data(self, migrated_db):
        """退回去再升上来，数据仍在。**"能回滚"只有在这条通过时才算数。**"""
        db_module, url = migrated_db

        await db_module.downgrade("0001")
        await self._seed_rows_at_0001(url)
        await db_module.migrate("head")

        # 来回一次
        await db_module.downgrade("0001")
        assert "request_id" not in _columns(url, "audit_logs")
        assert "ix_audit_request" not in _index_names(url, "audit_logs")
        assert await self._counts(db_module) == (1, 1)

        await db_module.migrate("head")
        assert await self._counts(db_module) == (1, 1)
        assert _metadata_diff(url) == []

    async def test_new_rows_can_carry_a_request_id(self, migrated_db):
        """加完列之后，新写入的行要能真的带上一个 request_id。

        只验证"列存在"是不够的 —— 列存在但写不进去（长度不够、被别的默认值
        覆盖）时，功能测试里那条"审计行带 request_id"的用例会红在这个文件之外，
        而这里是最该先红的地方。
        """
        _, url = migrated_db
        assert "request_id" in _columns(url, "audit_logs")

        from lagent.db import session_scope
        from lagent.models import AuditLog
        from lagent.obs import bind_request_id

        with bind_request_id("req-from-test"):
            from lagent import audit

            await audit.record(action="probe.migrated", actor_name="迁移后写入")

        async with session_scope() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT request_id, action, actor_name FROM audit_logs "
                        "ORDER BY id DESC LIMIT 1"
                    )
                )
            ).one()
        assert row[0] == "req-from-test"
        assert row[1] == "probe.migrated"
        assert row[2] == "迁移后写入"
        assert AuditLog.__tablename__ == "audit_logs"  # 顺手钉住表名没被改过
