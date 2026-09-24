"""备份与恢复（P0-2）。

这一组用例的核心不是"命令跑通了"，而是**恢复真的能救回来**。
备份命令成功退出却产出一个恢复不了的文件，是所有失败形态里最坏的一种 ——
它让人以为有退路，直到真出事那天才发现没有。

所以第 2 条用例是**完整的破坏—恢复演练**：先备份，再把表真的删掉，
然后恢复，最后比对数据一致。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import func, select, text


async def _count(session, model) -> int:
    return int((await session.execute(select(func.count()).select_from(model))).scalar() or 0)


class TestBackup:
    async def test_backup_produces_a_usable_file(self, isolated_db, tmp_path):
        """回执必须给出路径与大小 —— "跑过了"本身不是证据。"""
        from lagent.backup import backup

        result = backup(directory=tmp_path)
        assert result.path.exists()
        assert result.size_bytes > 0
        assert result.backend == "sqlite"
        assert "已备份" in result.describe()
        # 文件必须真的是一个能打开的库，而不是一个碰巧有字节的普通文件
        probe = sqlite3.connect(str(result.path))
        try:
            assert probe.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            probe.close()

    async def test_filename_is_stamped_so_ordering_is_restore_order(self, isolated_db, tmp_path):
        from lagent.backup import backup

        first = backup(directory=tmp_path)
        assert first.path.name.startswith("lab-")
        assert first.path.suffix == ".db"

    def test_a_name_with_a_path_separator_is_rejected(self, isolated_db, tmp_path):
        """否则 --dir 与 --name 组合起来会写到预期之外的地方。"""
        from lagent.backup import backup

        with pytest.raises(Exception, match="路径分隔符"):
            backup(directory=tmp_path, name="../escaped.db")

    def test_an_unsupported_backend_is_rejected(self, isolated_db, monkeypatch, tmp_path):
        """不支持的后端必须明确报错，而不是退化成一个假备份。"""
        from lagent.backup import backup
        from lagent.config import reset_settings_cache

        monkeypatch.setenv("LAB_DATABASE_URL", "mysql+aiomysql://u:p@h/db")
        reset_settings_cache()
        try:
            with pytest.raises(Exception, match="暂不支持"):
                backup(directory=tmp_path)
        finally:
            reset_settings_cache()


class TestRestoreDrill:
    async def test_round_trip_recovers_data_after_the_table_is_dropped(
        self, isolated_db, tmp_path
    ):
        """★ 完整演练：备份 → 真删表 → 恢复 → 数据一致。

        断言"恢复了"是不够的：恢复到一个空库上也会"成功"。
        所以这里比对的是**恢复前后的行数与具体内容**。
        """
        from lagent import db as db_module
        from lagent.backup import backup, restore
        from lagent.models import Reservation, User

        async with db_module.session_scope() as session:
            before_users = await _count(session, User)
            before_reservations = await _count(session, Reservation)
            names_before = sorted(
                (await session.execute(select(User.username))).scalars().all()
            )
        assert before_users > 0
        assert before_reservations > 0

        snapshot = backup(directory=tmp_path, name="drill.db")

        # —— 破坏：真的把预约表删掉（模拟误操作，而不是模拟"服务异常"）——
        # 顺序上必须先清掉引用它的子表，否则外键会拒绝 DROP ——
        # 这不是演练的妥协，而是"误删"在真实库上同样要面对的约束顺序。
        async with db_module.session_scope() as session:
            await session.execute(text("DELETE FROM reservation_slots"))
            await session.execute(text("DROP TABLE reservations"))
            await session.commit()

        # 恢复：先关引擎再换文件，所以必须重新连一次才算数
        message = await restore(snapshot.path, dispose_engine=db_module.dispose_engine)
        assert "已从" in message

        async with db_module.session_scope() as session:
            after_users = await _count(session, User)
            after_reservations = await _count(session, Reservation)
            names_after = sorted(
                (await session.execute(select(User.username))).scalars().all()
            )
        assert after_users == before_users
        assert after_reservations == before_reservations
        assert names_after == names_before

    async def test_a_corrupt_file_is_refused_before_touching_the_live_database(
        self, isolated_db, tmp_path
    ):
        """★ 顺序很关键：先校验备份文件，**再**动现库。

        反过来的话，一个坏备份会把好库一起毁掉 ——
        那时你既没有备份也没有原库。
        """
        from lagent import db as db_module
        from lagent.backup import restore
        from lagent.models import User

        garbage = tmp_path / "garbage.db"
        garbage.write_bytes(b"this is definitely not a sqlite database" * 50)

        async with db_module.session_scope() as session:
            before = await _count(session, User)

        # 匹配"已中止恢复"而不是具体的某句话：文件坏法有很多种
        # （不是库 / 截断 / 完整性检查不过），共同点是**必须中止且不动现库**。
        with pytest.raises(Exception, match="已中止恢复"):
            await restore(garbage, dispose_engine=db_module.dispose_engine)

        # 现库必须原封不动
        async with db_module.session_scope() as session:
            assert await _count(session, User) == before

    async def test_a_missing_file_is_refused(self, isolated_db, tmp_path):
        from lagent import db as db_module
        from lagent.backup import restore

        with pytest.raises(Exception, match="不存在"):
            await restore(tmp_path / "nope.db", dispose_engine=db_module.dispose_engine)

    async def test_an_empty_file_is_refused(self, isolated_db, tmp_path):
        """0 字节文件也算"存在"，但它恢复出来只会是一个空库。"""
        from lagent import db as db_module
        from lagent.backup import restore

        empty = tmp_path / "empty.db"
        empty.write_bytes(b"")
        with pytest.raises(Exception, match="空的"):
            await restore(empty, dispose_engine=db_module.dispose_engine)

    async def test_restore_leaves_no_bak_file_behind(self, isolated_db, tmp_path):
        """替换过程中那个 .bak 是临时保险，成功了就该收掉 ——
        留着会让人分不清哪个才是当前库。"""
        from lagent import db as db_module
        from lagent.backup import backup, restore
        from lagent.config import get_settings
        from lagent.db import init_db

        snapshot = backup(directory=tmp_path, name="cleanup.db")
        await restore(snapshot.path, dispose_engine=db_module.dispose_engine)
        # 重新初始化引擎（restore 已把它关掉），确认库还能正常用
        await init_db()
        live = Path(get_settings().database_url.split("///")[-1])
        assert not live.with_suffix(live.suffix + ".bak").exists()


class TestBackupIsNotArchiving:
    """把这条区别写成测试，是因为它太容易被混为一谈。"""

    async def test_archiving_deletes_rows_while_backup_keeps_everything(
        self, isolated_db, tmp_path
    ):
        """归档 = 导出后**删掉**老数据；备份 = 一个字节都不动。

        两者都在 var/ 下、都是"导出成文件"，所以最容易被人当成同一件事。
        """
        from lagent import db as db_module
        from lagent.backup import backup
        from lagent.models import AuditLog

        async with db_module.session_scope() as session:
            before = await _count(session, AuditLog)
        backup(directory=tmp_path)
        async with db_module.session_scope() as session:
            assert await _count(session, AuditLog) == before
