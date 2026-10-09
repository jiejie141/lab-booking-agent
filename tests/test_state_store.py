"""进程外状态后端（P2-1）：SqliteSessionStore 与 SqliteSlidingWindowLimiter。

针对内存版接口立下的约定，换后端必须原样成立：

* 会话：update-not-replace（save 不清 turns）、LRU 容量上限、clear、
  **跨实例持久化**（多进程共享的核心价值）、TTL 过期即空、坏数据自删；
* 限流：窗口内限量、滑出窗口后放行、**多实例共享计数**、limit=0 关闭。

持久化类测试用 tmp_path 下独立的 db 文件 —— 与 conftest「每例一套库」
是同一条理由：共享可变状态会让用例之间互相污染，表现为时灵时不灵。
"""

from __future__ import annotations

from lagent.agent.state import SqliteSessionStore
from lagent.localstate import connect
from lagent.ratelimit import SqliteSlidingWindowLimiter
from lagent.schemas import Proposal, Requirement


def _requirement() -> Requirement:
    return Requirement.model_validate(
        {"date": "2026-10-08", "start": "14:00", "end": "16:00", "equipment_name": "荧光光谱仪"}
    )


def _proposal(i: int = 1) -> Proposal:
    return Proposal.model_validate(
        {
            "kind": "exact",
            "equipment_id": i,
            "equipment_name": "荧光光谱仪",
            "lab_label": "分析楼301",
            "date": "2026-10-08",
            "start": "14:00",
            "end": "16:00",
            "hours": 2.0,
        }
    )


# ==========================================================================
# SqliteSessionStore
# ==========================================================================
class TestSqliteSessionStore:
    async def test_roundtrip(self, tmp_path):
        store = SqliteSessionStore(str(tmp_path / "state.db"))
        store.save("s1", [_proposal(3)], _requirement(), awaiting=True)
        assert store.awaiting("s1") is True
        assert store.proposals("s1") == [_proposal(3)]
        assert store.requirement("s1") == _requirement()

    async def test_save_keeps_turns(self, tmp_path):
        """update-not-replace：save 之后 React 的轮次历史必须还在。"""
        store = SqliteSessionStore(str(tmp_path / "state.db"))
        store.append_turn("s1", "user", "明天下午约光谱仪")
        store.append_turn("s1", "assistant", "已给出方案")
        store.save("s1", [_proposal()], _requirement())
        assert [t["content"] for t in store.history("s1")] == [
            "明天下午约光谱仪",
            "已给出方案",
        ]

    async def test_append_turn_caps_history(self, tmp_path):
        store = SqliteSessionStore(str(tmp_path / "state.db"), max_turns=3)
        for i in range(5):
            store.append_turn("s1", "user", f"m{i}")
        assert [t["content"] for t in store.history("s1")] == ["m2", "m3", "m4"]

    async def test_lru_eviction(self, tmp_path):
        store = SqliteSessionStore(str(tmp_path / "state.db"), max_sessions=2)
        store.save("a", [], _requirement())
        store.save("b", [], _requirement())
        store.save("c", [], _requirement())
        assert store.requirement("a") is None
        assert store.requirement("c") is not None

    async def test_clear(self, tmp_path):
        store = SqliteSessionStore(str(tmp_path / "state.db"))
        store.save("s1", [_proposal()], _requirement(), awaiting=True)
        store.clear("s1")
        assert store.awaiting("s1") is False
        assert store.proposals("s1") == []

    async def test_persists_across_instances(self, tmp_path):
        """多进程共享的核心价值：第二个实例看到第一个实例写下的会话。"""
        path = str(tmp_path / "state.db")
        first = SqliteSessionStore(path)
        first.save("s1", [_proposal(7)], _requirement(), awaiting=True)
        second = SqliteSessionStore(path)
        assert second.proposals("s1") == [_proposal(7)]
        assert second.awaiting("s1") is True

    async def test_ttl_expiry(self, tmp_path):
        path = str(tmp_path / "state.db")
        store = SqliteSessionStore(path, ttl_seconds=60)
        store.save("s1", [_proposal()], _requirement())
        # 直接把 updated_at 拨回两小时前，等价于"这条会话写于很久以前"
        conn = connect(path)
        try:
            conn.execute("UPDATE agent_session SET updated_at = updated_at - 7200")
        finally:
            conn.close()
        assert store.requirement("s1") is None
        # 惰性清理：过期读一次之后行应该已经没了
        conn = connect(path)
        try:
            rows = conn.execute("SELECT COUNT(*) FROM agent_session").fetchone()
            assert rows[0] == 0
        finally:
            conn.close()

    async def test_corrupt_payload_self_deletes(self, tmp_path):
        """坏数据自删：会话是缓存级数据，一条脏行不能炸掉服务。"""
        path = str(tmp_path / "state.db")
        store = SqliteSessionStore(path)
        store.save("s1", [_proposal()], _requirement())
        conn = connect(path)
        try:
            conn.execute("UPDATE agent_session SET payload = '{not json'")
        finally:
            conn.close()
        assert store.proposals("s1") == []
        assert store.requirement("s1") is None


# ==========================================================================
# SqliteSlidingWindowLimiter
# ==========================================================================
class TestSqliteSlidingWindowLimiter:
    async def test_allows_up_to_limit_then_denies(self, tmp_path):
        limiter = SqliteSlidingWindowLimiter(3, 60.0, path=str(tmp_path / "state.db"))
        for _ in range(3):
            allowed, _ = limiter.hit("u1")
            assert allowed is True
        allowed, retry_after = limiter.hit("u1")
        assert allowed is False
        assert retry_after >= 1

    async def test_window_slides(self, tmp_path):
        path = str(tmp_path / "state.db")
        limiter = SqliteSlidingWindowLimiter(1, 60.0, path=path)
        assert limiter.hit("u1")[0] is True
        assert limiter.hit("u1")[0] is False
        # 把已有记录拨回两小时前 = 等价于"窗口已经滑过去"，应当放行
        conn = connect(path)
        try:
            conn.execute("UPDATE rate_hit SET ts = ts - 7200")
        finally:
            conn.close()
        assert limiter.hit("u1")[0] is True

    async def test_counts_are_shared_across_instances(self, tmp_path):
        """多进程共享的核心价值：另一个实例的命中也要占我的配额。"""
        path = str(tmp_path / "state.db")
        a = SqliteSlidingWindowLimiter(2, 60.0, path=path)
        b = SqliteSlidingWindowLimiter(2, 60.0, path=path)
        assert a.hit("u1")[0] is True
        assert b.hit("u1")[0] is True
        assert a.hit("u1")[0] is False
        assert b.hit("u1")[0] is False

    async def test_limit_zero_disables(self, tmp_path):
        limiter = SqliteSlidingWindowLimiter(0, 60.0, path=str(tmp_path / "state.db"))
        assert limiter.enabled is False
        for _ in range(5):
            assert limiter.hit("u1") == (True, 0)
        assert limiter.tracked_keys() == 0

    async def test_keys_are_independent(self, tmp_path):
        limiter = SqliteSlidingWindowLimiter(1, 60.0, path=str(tmp_path / "state.db"))
        assert limiter.hit("u1")[0] is True
        assert limiter.hit("u2")[0] is True
        assert limiter.hit("u1")[0] is False
