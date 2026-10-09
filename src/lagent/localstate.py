"""进程外状态的 SQLite 落地：会话（``agent_session``）与限流计数（``rate_hit``）。

为什么是 SQLite 而不是直接上 Redis：这两个组件的痛点是「多进程」与「重启」，
同机多进程用一把带 WAL 的 SQLite 就能解决；**跨机器**的多副本才真正需要
Redis / 共享数据库 —— 那是 P2 的事，本模块不假装自己解决了分布式。

连接策略：**每次操作开一条短连接**，而不是实例持有长连接。

  * 多进程共享靠的是文件锁（WAL + busy_timeout），进程内长连接帮不上忙；
  * 短连接避开了「跨线程共享连接」这一类 sqlite3 的坑，也不需要额外的锁；
  * 这里的操作都是毫秒级的小读写，连接开销可以接受 —— 真到了要省它的时候，
    说明量已经大到该换 Redis 了。

PRAGMA 三件套：WAL（读写不互斥）、busy_timeout（多进程写碰撞时等而不是炸）、
synchronous=NORMAL（WAL 下的标准取舍，丢电也只丢最后一笔事务）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_session (
    session_id TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_hit (
    key TEXT NOT NULL,
    ts  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rate_hit_key_ts ON rate_hit (key, ts);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """打开一条按 PRAGMA 配好的短连接。目录不存在则创建。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_state_db(path: str | Path) -> None:
    """建表（幂等）。服务启动与测试夹具调用。"""
    conn = connect(path)
    try:
        conn.executescript(_SCHEMA)
    finally:
        conn.close()
