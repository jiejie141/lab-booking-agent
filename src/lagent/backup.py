"""数据库备份与恢复。

为什么单独有这个模块 —— 后台清扫里的「归档」**不是**备份：

* 归档是把审计/流水里超过保留期的行导出成 JSONL，然后**从库里删掉**；
  它保护的是「日志表无限增长」。
* 备份保护的是另一件事：**库文件损坏、误删表、误 UPDATE 把全表改坏**。
  归档对这三类场景一点用都没有 —— 被改坏的行同样会被"归档"出去。

对高校实验室这种"数据要留好几年、运维可能由行政老师兼任"的场景，
第二件事才是真正怕的那个。所以这里提供 ``backup`` / ``restore`` 两条命令，
并且**恢复路径必须真的演练过一次**（没演练过的备份不算备份）。

两种后端两种做法，刻意不为统一接口牺牲正确性：

* **SQLite**：用 ``sqlite3`` 的**在线备份 API**（``Connection.backup``）。
  它是官方为"库正在被使用"这个场景设计的；直接 ``cp`` 一个开着 WAL 的库文件
  会拿到一个不完整的快照。``VACUUM INTO`` 也行，但它要求这段时间没有写入，
  而备份命令恰恰是"服务还在跑"时才需要。
* **PostgreSQL**：``pg_dump`` / ``psql``。没有自己写逻辑导出 ——
  权限、序列、大对象、部分索引这些细节自己实现必然漏，
  而官方工具已经把这件事做对了三十年。缺二进制就明确报错，
  不悄悄退化成一个"看起来成功"的假备份。
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from .config import get_settings

# sqlite3 的在线备份 API 需要 3.7 以上；Python 自带的版本远高于此。
# 仍然显式检查一次：这条命令唯一不能接受的结果就是"备份文件看起来好好的，
# 恢复时才发现是坏的"，所以每一步都要能自证。
_MIN_SQLITE = (3, 7)


class BackupError(RuntimeError):
    """备份或恢复失败。

    刻意不做"部分成功"：备份写到一半断了，文件是存在的 ——
    一个存在的坏文件比没有备份更危险，因为它会让人以为有退路。
    """


@dataclass(frozen=True)
class BackupResult:
    """一次备份的结果，用来打印一行人能看懂的回执。"""

    path: Path
    backend: str
    size_bytes: int
    took_seconds: float

    def describe(self) -> str:
        size = (
            f"{self.size_bytes / 1024:.1f} KB"
            if self.size_bytes < 1024 * 1024
            else f"{self.size_bytes / 1024 / 1024:.1f} MB"
        )
        return (
            f"已备份（{self.backend}）→ {self.path}  "
            f"{size} · 耗时 {self.took_seconds:.2f}s"
        )


def backup_dir() -> Path:
    """备份目录（``LAB_BACKUP_DIR``，默认 ``./var/backup``）。

    默认放在 ``var/`` 下与归档保持一致：那一整个目录都在 .gitignore 里，
    属于运行态产物而不是仓库内容。
    """
    return Path(get_settings().backup_dir)


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _suffix(backend: str) -> str:
    return ".db" if backend == "sqlite" else ".sql"


def _backend_of(url: str) -> str:
    name = make_url(url).get_backend_name()
    if name == "sqlite":
        return "sqlite"
    if name == "postgresql":
        return "postgresql"
    raise BackupError(f"暂不支持备份这种数据库：{name}（目前支持 sqlite / postgresql）")


def _check_sqlite() -> None:
    version = tuple(int(part) for part in sqlite3.sqlite_version.split(".")[:2])
    if version < _MIN_SQLITE:
        raise BackupError(
            f"SQLite 版本过低（{sqlite3.sqlite_version}），无法使用在线备份 API"
        )


def _sqlite_file(url: str) -> Path:
    database = make_url(url).database
    if not database:
        raise BackupError(f"无法从连接串里解析出库文件路径：{url}")
    return Path(database)


def _pg_dsn(url: str) -> str:
    """把 SQLAlchemy 连接串还原成 libpq 认识的形式。"""
    parts = make_url(url)
    host = parts.host or "localhost"
    port = f":{parts.port}" if parts.port else ""
    return f"postgresql://{parts.username or ''}:{parts.password or ''}@{host}{port}/{parts.database}"


def _require(binary: str) -> str:
    found = shutil.which(binary)
    if found is None:
        raise BackupError(
            f"找不到 {binary}。PostgreSQL 的备份/恢复用的是官方工具，"
            "没有自己实现一遍（自己写必然漏掉序列、权限、部分索引这些细节）。"
            f"请先安装 PostgreSQL 客户端工具，或把 {binary} 加进 PATH。"
        )
    return found


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------
def backup(
    *,
    directory: Path | None = None,
    name: str | None = None,
) -> BackupResult:
    """把当前库备份成一个文件。

    ``name`` 省略时按时间戳命名，于是同一目录下按时间排序就是恢复顺序。
    """
    settings = get_settings()
    url = settings.database_url
    backend = _backend_of(url)
    target_dir = directory or backup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    filename = name or f"lab-{_stamp()}{_suffix(backend)}"
    # 名字里带目录分隔符会让 --dir 与 --name 的组合写出预期之外的位置，
    # 而这类"备份其实没写到我以为的地方"正是最晚才被发现的那类错。
    if "/" in filename or "\\" in filename:
        raise BackupError(f"备份文件名不能含路径分隔符：{filename}")
    target = target_dir / filename

    started = time.perf_counter()
    if backend == "sqlite":
        _backup_sqlite(_sqlite_file(url), target)
    else:
        _backup_postgres(_pg_dsn(url), target)

    took = time.perf_counter() - started
    if not target.exists() or target.stat().st_size == 0:
        # 走到这里说明工具"成功"了却没产出文件 —— 宁可报错也不要回执成功。
        raise BackupError(f"备份未产出有效文件：{target}")
    return BackupResult(target, backend, target.stat().st_size, took)


def _backup_sqlite(source: Path, target: Path) -> None:
    _check_sqlite()
    if not source.exists():
        raise BackupError(f"源库文件不存在：{source}")
    # 只读方式打开源库：备份不该有写源库的可能。
    # 用 URI 形式是为了显式声明只读，靠"我只调读接口"是不够的。
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
        # 自证：备份完立刻在**副本**上做一次完整性检查。
        # 复制到一半的库文件在恢复前是看不出问题的，那时已经晚了。
        check = sqlite3.connect(str(target))
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()
        finally:
            check.close()
        if not result or result[0] != "ok":
            raise BackupError(f"备份副本未通过完整性检查：{result}")
    finally:
        src.close()


def _backup_postgres(dsn: str, target: Path) -> None:
    pg_dump = _require("pg_dump")
    # --no-owner / --no-privileges：备份要在**另一台机器、另一个库名**上也能恢复，
    # 带上原库的角色与授权会让恢复方必须存在同名角色。
    command = [pg_dump, "--no-owner", "--no-privileges", "--file", str(target), dsn]
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise BackupError(f"pg_dump 失败（退出码 {done.returncode}）：{done.stderr.strip()}")


# ---------------------------------------------------------------------------
# 恢复
# ---------------------------------------------------------------------------
async def restore(source: Path, *, dispose_engine) -> str:
    """从一个备份文件恢复。**会覆盖当前库里的全部数据。**

    ``dispose_engine`` 由调用方传入（而不是这里直接 import 后再调用）：
    恢复 SQLite 时要**先关掉引擎再换文件**，否则进程里还握着旧文件的句柄，
    换完之后读到的可能是旧内容 —— 而这个模块不该替调用方决定什么时候关引擎。
    """
    if not source.exists():
        raise BackupError(f"备份文件不存在：{source}")
    if source.stat().st_size == 0:
        raise BackupError(f"备份文件是空的：{source}")

    settings = get_settings()
    url = settings.database_url
    backend = _backend_of(url)

    started = time.perf_counter()
    if backend == "sqlite":
        await _restore_sqlite(source, _sqlite_file(url), dispose_engine)
    else:
        await _restore_postgres(source, _pg_dsn(url))
    return f"已从 {source} 恢复（{backend}）· 耗时 {time.perf_counter() - started:.2f}s"


async def _restore_sqlite(source: Path, target: Path, dispose_engine) -> None:
    _check_sqlite()
    # 先校验**副本本身**是不是一个完整可用的库，再动现库。
    # 顺序反了的话，一个坏备份会把好库一起毁掉。
    # ⚠️ sqlite3 是**惰性**打开的：connect 对"不是数据库的文件"并不报错，
    # 真正的 "file is not a database" 要等第一次查询才抛。
    # 所以 connect 与首次查询必须一起包住，只包 connect 等于没包。
    probe: sqlite3.Connection | None = None
    try:
        probe = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        result = probe.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise BackupError(f"备份文件未通过完整性检查，已中止恢复：{result}")
        tables = probe.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        if not tables:
            raise BackupError(f"备份文件里没有任何表，已中止恢复：{source}")
    except sqlite3.Error as exc:
        # 截断过、传错的文件会走到这里。必须换成一句人能看懂的话：
        # 底层的 "file is not a database" 不会告诉运维"这个文件不能用来恢复"。
        raise BackupError(
            f"{source} 不是一个可用的 SQLite 库（{exc}），已中止恢复 —— 现库未被改动。"
        ) from exc
    finally:
        if probe is not None:
            probe.close()

    await dispose_engine()
    # 现库**先改名再替换**，不做就地覆盖：中途失败还能把 .bak 改回来。
    # 直接覆盖的话，恢复写一半失败就两个版本都不完整。
    backup_of_current = target.with_suffix(target.suffix + ".bak")
    if backup_of_current.exists():
        backup_of_current.unlink()
    if target.exists():
        target.rename(backup_of_current)
    try:
        shutil.copyfile(source, target)
    except Exception:
        if backup_of_current.exists():
            backup_of_current.rename(target)
        raise
    # WAL 伴生文件属于**旧**库，留着会与新库文件混在一起。
    for suffix in ("-wal", "-shm"):
        stale = target.with_name(target.name + suffix)
        if stale.exists():
            stale.unlink()
    if backup_of_current.exists():
        backup_of_current.unlink()


async def _restore_postgres(source: Path, dsn: str) -> None:
    psql = _require("psql")
    # ⚠️ ON_ERROR_STOP=1 是必须的：psql 默认遇到单条语句失败会**继续往下跑**，
    # 最后返回一个非 0 退出码就了事 —— 于是"恢复了一半"也能看起来像是跑过了。
    # 加上它，第一条失败的语句就会立刻中止，不会出现"半张表新半张表旧"。
    command = [
        psql,
        "--quiet",
        "--no-psqlrc",
        "-v",
        "ON_ERROR_STOP=1",
        "--dbname",
        dsn,
        "--file",
        str(source),
    ]
    done = await asyncio.to_thread(
        subprocess.run, command, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise BackupError(
            f"psql 恢复失败（退出码 {done.returncode}）：{done.stderr.strip()}"
        )
