"""PostgreSQL 真实链路（默认跳过，见下）。

## 为什么要单独一个文件

整套 800+ 条用例都跑在 SQLite 上 —— 连 `conftest.isolated_db` 都把库地址硬编码成
`sqlite+aiosqlite://`。而**生产形态是 PostgreSQL**：`docker-compose.yml` 里写的是
`postgresql+asyncpg://`，并发那段还用了 `pg_advisory_xact_lock()`。

也就是说，在加这个文件之前，**PG 那条路径一行都没被执行过**。这不是"覆盖率差一点"，
而是"最要紧的那条分支从来没跑过"：迁移在空库上能不能升到头、
部分唯一索引的 `postgresql_where` 有没有真的写进 DDL、advisory lock 的 SQL
在 asyncpg 下能不能过 —— 全都是"读代码觉得没问题"。读代码觉得没问题是最弱的证据。

## 为什么默认跳过，而不是自动跑

它需要一个**真的 PostgreSQL 库**，并且会 `DROP SCHEMA public CASCADE` 清场。
连到一个真实数据库上的后果是不可逆的，所以：

* 只有显式给了 ``LAB_TEST_PG_URL`` 才跑（本地默认不跑，也不能跑）；
* 给了 ``LAB_REQUIRE_PG_TESTS=1`` 却没给 URL → **当场报错，不静默跳过**。
  这条是给 CI 用的：那个 job 存在的唯一意义就是跑这些用例，
  静默跳过等于"job 绿了但什么都没验"，那是最坏的一种绿。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from lagent.clock import now_local
from lagent.db import get_engine, migrate, session_scope
from lagent.models import (
    ACTIVE_STATUSES,
    PERMIT_ISSUED,
    STATUS_CANCELLED,
    STATUS_CONFIRMED,
    STATUS_EXPIRED,
    EntryPermit,
    LabOccupancy,
    Reservation,
    ReservationSlot,
)

PG_URL = os.environ.get("LAB_TEST_PG_URL", "").strip()

if not PG_URL and os.environ.get("LAB_REQUIRE_PG_TESTS") == "1":
    raise RuntimeError(
        "LAB_REQUIRE_PG_TESTS=1 但没有 LAB_TEST_PG_URL："
        "这个 job 的唯一意义就是跑 PG 专项用例，不能静默跳过。"
        "请检查 workflow 里的 services.postgres 与 env.LAB_TEST_PG_URL。"
    )

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="未设置 LAB_TEST_PG_URL —— PG 专项用例只在 postgres job 里跑（它会清库，不能连真实库）",
)

# 与 conftest 同一份演示账号。李娜资质齐全；紫外可见分光光度计在分析楼301。
LINA, ZHANGWEI = 2, 1
UV = 2
LAB_301, LAB_205 = 1, 2
START, END = dt.time(10, 0), dt.time(12, 0)


def free_day() -> dt.date:
    """+2 天，时段取 10:00-12:00。

    理由与别处一样：`+1 天` 会随运行日期落到周末，而三个实验室的周末开放时间更短；
    10:00-12:00 落在「工作日 ∩ 周末」的交集里，哪天跑都合法。
    """
    return now_local().date() + dt.timedelta(days=2)


@pytest.fixture
async def pg_db(monkeypatch):
    """一个**从零建起来**的 PG 库：清 schema → 跑迁移 → 灌种子。

    刻意用 `DROP SCHEMA ... CASCADE` + `migrate()`，而不是 `init_db(drop_first=True)`：
    后者走的是 `Base.metadata.create_all`，那样就绕过了要验的东西。
    这里要的正是「空库 + alembic upgrade head」这条真实上线路径。
    """
    from lagent import db as db_module
    from lagent.config import reset_settings_cache
    from lagent.seed import seed

    monkeypatch.setenv("LAB_DATABASE_URL", PG_URL)
    monkeypatch.setenv("LAB_APP_MODE", "mock")
    monkeypatch.setenv("LAB_PASSWORD_KDF_N", "1024")
    monkeypatch.setenv("LAB_ALLOW_INSECURE_DEFAULTS", "true")
    monkeypatch.setenv("LAB_SWEEP_ENABLED", "false")

    reset_settings_cache()
    await db_module.dispose_engine()

    async with get_engine().begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))

    await migrate()
    await seed(force=True)

    yield db_module

    await db_module.dispose_engine()
    reset_settings_cache()


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
async def _table_names() -> set[str]:
    async with get_engine().connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
        )
        return {row[0] for row in rows}


async def _index_defs() -> dict[str, str]:
    async with get_engine().connect() as conn:
        rows = await conn.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'")
        )
        return {row[0]: row[1] for row in rows}


async def _insert_reservation(day: dt.date, status: str) -> int:
    async with session_scope() as session:
        row = Reservation(
            user_id=LINA,
            equipment_id=UV,
            date=day,
            start_time=START,
            end_time=END,
            status=status,
            purpose="PG 唯一索引验证",
            version=1,
        )
        session.add(row)
        await session.flush()
        return int(row.id)


async def _insert_permit(*, status: str, lab_id: int, tag: str = "") -> int:
    async with session_scope() as session:
        row = EntryPermit(
            user_id=LINA,
            lab_id=lab_id,
            date=free_day(),
            valid_from=dt.time(8, 0),
            valid_to=dt.time(22, 0),
            status=status,
            credential_hash=f"pg-test-{status}-{lab_id}-{tag}",
            required_certs=[],
        )
        session.add(row)
        await session.flush()
        return int(row.id)


# ==========================================================================
# 一、迁移与结构：空库 → head
# ==========================================================================
class TestMigrationsOnPostgres:
    async def test_an_empty_postgres_reaches_head(self, pg_db):
        """空库上 `alembic upgrade head` 必须成功。

        这条最容易被"我本地 SQLite 能跑"掩盖：迁移里只要有一句
        SQLite 能过、PG 过不去的 DDL，生产部署第一次 `migrate` 就会死在半路 ——
        而半路的库最难收拾。
        """
        from lagent.db import current_revision, head_revision

        assert await current_revision() == head_revision()

    async def test_schema_has_no_drift_on_postgres(self, pg_db):
        """迁移建出来的表与列必须覆盖 models.py 的全部声明。"""
        from lagent.db import schema_drift

        assert await schema_drift() == []

    async def test_downgrade_then_upgrade_round_trips(self, pg_db):
        """回退到底再升上来，表集合要能复原。

        回滚路径平时没人走，所以最容易烂 —— 真出事（上线后要回滚）时才会发现
        它坏了，而那个时间点最不能出问题。
        """
        from lagent.db import current_revision, downgrade
        from lagent.models import Base

        expected = {table.name for table in Base.metadata.sorted_tables}

        await downgrade("base")
        assert "users" not in await _table_names(), "回退到底之后业务表应该都没了"

        await migrate()
        assert await current_revision() is not None
        assert expected <= await _table_names(), "升回来之后表没复原"


# ==========================================================================
# 二、靠唯一索引兜住的不变式，在 PG 上同样成立
# ==========================================================================
class TestInvariantsOnPostgres:
    """这些不变式在 SQLite 上有测试，但 `postgresql_where=` 是**另一份** DDL。

    两边写重了、或者只写了 `sqlite_where`，SQLite 上全绿而 PG 上的索引退化成
    "无条件唯一"。退化的后果不是报错，是**取消过的时段再也约不上** ——
    一个平时看不出来、一到真实使用就被投诉的形状。
    """

    @pytest.mark.parametrize(
        "index_name",
        [
            "uq_res_active_slot",
            "uq_equipment_slot",
            "uq_lab_slot_seat",
            "uq_permit_one_inside",
            "uq_permit_credential",
        ],
    )
    async def test_the_index_exists_and_is_unique(self, pg_db, index_name):
        definitions = await _index_defs()
        assert index_name in definitions, f"{index_name} 没有在 PG 上建出来"
        assert "UNIQUE" in definitions[index_name].upper()

    @pytest.mark.parametrize("index_name", ["uq_res_active_slot", "uq_permit_one_inside"])
    async def test_the_partial_indexes_kept_their_where_clause(self, pg_db, index_name):
        """★ 部分索引的 WHERE 必须真的在 DDL 里。

        少了它，索引从"只在有效状态下唯一"变成"永远唯一" ——
        于是取消一条预约之后，那个时段被这条索引永久占住，谁都约不了。
        """
        definition = (await _index_defs())[index_name]
        assert " WHERE " in definition.upper(), (
            f"{index_name} 在 PG 上丢掉了 WHERE 子句：{definition}"
        )

    async def test_the_partial_index_actually_blocks_and_then_releases(self, pg_db):
        """不只查 DDL，真插一次：先撞车、取消后必须能重订。"""
        day = free_day()

        first = await _insert_reservation(day, STATUS_CONFIRMED)
        with pytest.raises(IntegrityError):
            await _insert_reservation(day, STATUS_CONFIRMED)

        # 取消之后同一个时段必须能重新预定 —— 这条就是 WHERE 子句存在的理由
        async with session_scope() as session:
            row = await session.get(Reservation, first)
            assert row is not None
            row.status = STATUS_CANCELLED
        second = await _insert_reservation(day, STATUS_CONFIRMED)
        assert second != first

    async def test_a_permit_cannot_be_inside_two_rooms_at_once(self, pg_db):
        """同一个 user_id 在 checked_in 状态下只能有一条凭证行。"""
        await _insert_permit(status="checked_in", lab_id=LAB_301)
        with pytest.raises(IntegrityError):
            await _insert_permit(status="checked_in", lab_id=LAB_205, tag="b")
        # 换一个状态就不该再冲突（这正是 WHERE 的语义）
        await _insert_permit(status="used", lab_id=LAB_205, tag="c")


# ==========================================================================
# 三、并发：advisory lock 那段 SQL 真的跑了
# ==========================================================================
class TestConcurrencyOnPostgres:
    async def test_the_lock_strategy_is_the_postgres_one(self, pg_db):
        """`acquire_equipment_lock` 在 PG 上必须走 advisory lock，而不是空实现。

        它在 SQLite 下返回 `"none(sqlite)"` 什么都不做 —— 如果那个分支判断写错，
        PG 上也会静默走到空实现，而"锁没生效"从任何日志里都看不出来。
        """
        from lagent.domain.booking import acquire_equipment_lock

        async with session_scope() as session:
            strategy = await acquire_equipment_lock(session, UV)

        assert strategy == "pg_advisory_xact_lock"

    async def test_concurrent_bookings_yield_exactly_one_winner(self, pg_db):
        """★ 20 个并发抢同一个时段，必须**恰好 1 个成功**。

        PG 下的路径与 SQLite 完全不同：advisory lock 先把同设备的请求串起来，
        后到的在锁内复检就发现坑没了，大多会走 `conflict` 而不是撞唯一索引。
        两条路径都得保证不超卖 —— 而在此之前，这条路径从没被执行过。
        """
        from lagent.agent.tools import tool_create_reservation

        day = free_day()
        concurrency = 20
        results = await asyncio.gather(
            *(
                tool_create_reservation(
                    user_id=LINA,
                    equipment_id=UV,
                    date_=day,
                    start=START,
                    end=END,
                    purpose=f"PG 并发-{i}",
                )
                for i in range(concurrency)
            ),
            return_exceptions=True,
        )

        failures = [r for r in results if isinstance(r, BaseException)]
        assert not failures, f"并发下单抛了异常：{[type(f).__name__ for f in failures][:3]}"

        winners = [r for r in results if not isinstance(r, BaseException) and r.ok]
        assert len(winners) == 1, f"应当恰好 1 个成功，实际 {len(winners)} 个"

        async with session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(Reservation).where(
                            Reservation.equipment_id == UV,
                            Reservation.date == day,
                            Reservation.status.in_(ACTIVE_STATUSES),
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1, "库里留下了不止一条有效预约 —— 超卖了"

    async def test_the_seat_index_holds_under_concurrency(self, pg_db):
        """房间座位：同一格同一座位并发抢占，也只能成功一次。"""
        day = free_day()
        # 每个竞争者拿一张自己的凭证（permit_id 是非空外键），
        # 状态用 issued —— checked_in 会被 uq_permit_one_inside 挡住，那是另一条不变式。
        permit_ids = [
            await _insert_permit(status=PERMIT_ISSUED, lab_id=LAB_301, tag=str(i))
            for i in range(8)
        ]

        async def grab(permit_id: int) -> bool:
            try:
                async with session_scope() as session:
                    session.add(
                        LabOccupancy(
                            lab_id=LAB_301,
                            date=day,
                            slot_index=0,
                            seat=1,
                            permit_id=permit_id,
                        )
                    )
                return True
            except IntegrityError:
                return False

        outcomes = await asyncio.gather(*(grab(pid) for pid in permit_ids))
        assert sum(outcomes) == 1, "同一个座位被占了不止一次"


# ==========================================================================
# 四、清扫在 PG 上跑得通（rowcount 语义两边不同）
# ==========================================================================
class TestSweepOnPostgres:
    async def test_expired_reservations_are_swept(self, pg_db):
        """过期预约收尾依赖 `UPDATE ... rowcount`；asyncpg 下的返回方式与
        aiosqlite 不一样，值得真跑一次。"""
        from lagent.sweep import sweep_expired_reservations

        past = now_local().date() - dt.timedelta(days=1)
        async with session_scope() as session:
            row = Reservation(
                user_id=ZHANGWEI,
                equipment_id=UV,
                date=past,
                start_time=START,
                end_time=END,
                status=STATUS_CONFIRMED,
                purpose="PG 清扫验证",
                version=1,
            )
            session.add(row)
            await session.flush()
            rid = int(row.id)
            session.add(ReservationSlot(reservation_id=rid, equipment_id=UV,
                                        date=past, slot_index=0))

        processed, _detail = await sweep_expired_reservations()
        assert processed >= 1

        async with session_scope() as session:
            after = await session.get(Reservation, rid)
            assert after is not None and after.status == STATUS_EXPIRED
            left = (
                (
                    await session.execute(
                        select(ReservationSlot).where(
                            ReservationSlot.reservation_id == rid
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert left == [], "占用格没跟着释放"
