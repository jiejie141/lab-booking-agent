"""进程内滑动窗口限流。

为什么自己写而不引 slowapi：本项目已经有一条零依赖的线
（手写 BM25、手写 JWT、零构建前端），而限流的核心逻辑就是
「窗口内计数 + 剪掉过期记录」，20 行且完全可测。
真要上生产多副本时，这个实现必须换成 Redis 计数器 ——
**进程内计数在多副本下等于把配额乘以副本数**，
这一点写在这里比藏在依赖后面更清楚。

一个诚实的限制：这是单进程的，重启即清零。
所以它能防「脚本高频刷接口」，防不住分布式刷。
本文件里的 :class:`LoginThrottle` 是同一个限制的另一种形态，见它的注释。
"""

from __future__ import annotations

import threading
import time
from collections import deque

from .localstate import connect, init_state_db
from .metrics import record_state_backend


class SlidingWindowLimiter:
    """按 key（这里是用户）计数的滑动窗口限流器。

    用滑动窗口而不是固定窗口：固定窗口在边界上会放过 2 倍配额
    （12:00:59 打满一批、12:01:00 再打满一批）。
    """

    def __init__(self, limit: int, window_seconds: float = 60.0, max_keys: int = 4096) -> None:
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = {}
        self._clock = time.monotonic
        # 由**组件自己**上报后端，而不是服务启动时统一登记：
        # 哪天换成 Redis，改的是这个类，指标跟着变，不会留在 "memory" 上骗人。
        record_state_backend("ratelimit", "memory")

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def _sweep(self, cutoff: float) -> None:
        """丢掉已经全过期的 key。

        **必须调用方持锁。** 没有这一步，按用户限流就等于把每个访问过的
        user_id 永久留在内存里 —— 一个缓慢的内存泄漏，压测时才发现。
        """
        stale = [key for key, bucket in self._hits.items() if not bucket or bucket[-1] <= cutoff]
        for key in stale:
            self._hits.pop(key, None)

    def hit(self, key: str) -> tuple[bool, int]:
        """记一次访问。返回 ``(是否放行, 建议等待秒数)``。

        ``limit <= 0`` 视为关闭限流，永远放行（便于本地开发与压测）。
        """
        if not self.enabled:
            return True, 0

        now = self._clock()
        cutoff = now - self.window
        with self._lock:
            if len(self._hits) > self.max_keys:
                self._sweep(cutoff)

            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= self.limit:
                # 最早那次请求滑出窗口时就能再试
                retry_after = max(1, int(self.window - (now - bucket[0])) + 1)
                return False, retry_after

            bucket.append(now)
            return True, 0

    def reset(self, key: str | None = None) -> None:
        """清空计数。测试与运维用。"""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)

    def tracked_keys(self) -> int:
        """当前跟踪的 key 数（用于验证不会无界增长）。"""
        with self._lock:
            return len(self._hits)


class LoginThrottle:
    """登录失败计数 + 锁定（P1-7）。

    与 :class:`SlidingWindowLimiter` 的区别：那边限的是**频率**（不管成败），
    这里数的是**失败次数** —— 目的不同，防的是口令爆破。

    key 取 ``"来源地址#用户名"`` 而不是只取用户名，这是权衡的结果：

      * 只按用户名锁 → 任何人都能把别人的账号锁死。这个"攻击"比爆破口令
        更简单、更有效，而且受害者是正当用户；
      * 按"地址 + 账号"锁 → 同一个 IP 换一个账号还能继续试，但那个方向由
        IP 维度的频率限流（:class:`SlidingWindowLimiter`）兜着。

    ⚠️ **进程内状态**：重启即清零，多副本下配额 × 副本数 —— 与上面那个类
    是同一个限制。真要挡分布式爆破必须把计数外置（Redis），那属于 P2。
    这里先把"单机上拿脚本试口令"这条路堵上，并且**如实**说明它挡不住什么。
    """

    def __init__(
        self,
        max_attempts: int,
        window_seconds: float = 300.0,
        lock_seconds: float = 600.0,
        max_keys: int = 4096,
    ) -> None:
        self.max_attempts = max_attempts
        self.window = window_seconds
        self.lock_seconds = lock_seconds
        self.max_keys = max_keys
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._clock = time.monotonic

    @property
    def enabled(self) -> bool:
        return self.max_attempts > 0

    def check(self, key: str) -> tuple[bool, int]:
        """还能不能再试一次。返回 ``(是否允许, 建议等待秒数)``。"""
        if not self.enabled:
            return True, 0

        now = self._clock()
        with self._lock:
            until = self._locked_until.get(key, 0.0)
            if until > now:
                return False, max(1, int(until - now) + 1)
            if until:
                # 锁已到期：清掉，并把这一轮失败记录一并作废 ——
                # 否则用户刚等到解锁，又因为窗口里还留着旧记录被立刻再锁一次。
                self._locked_until.pop(key, None)
                self._failures.pop(key, None)

            bucket = self._failures.setdefault(key, deque())
            cutoff = now - self.window
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.max_attempts:
                self._locked_until[key] = now + self.lock_seconds
                return False, int(self.lock_seconds)
            return True, 0

    def record_failure(self, key: str) -> None:
        """记一次失败。"""
        if not self.enabled:
            return
        now = self._clock()
        with self._lock:
            if len(self._failures) > self.max_keys:
                # 与 SlidingWindowLimiter._sweep 同一件事：不清理就是内存泄漏
                for stale in [
                    k for k, b in self._failures.items()
                    if (not b or b[-1] <= now - self.window)
                    and self._locked_until.get(k, 0.0) <= now
                ]:
                    self._failures.pop(stale, None)
            self._failures.setdefault(key, deque()).append(now)

    def reset(self, key: str | None = None) -> None:
        """清空（登录成功后调用，或运维手动解锁）。"""
        with self._lock:
            if key is None:
                self._failures.clear()
                self._locked_until.clear()
            else:
                self._failures.pop(key, None)
                self._locked_until.pop(key, None)

    def locked_keys(self) -> int:
        """当前处于锁定状态的 key 数（运维看"有多少人被锁着"）。"""
        now = self._clock()
        with self._lock:
            return sum(1 for until in self._locked_until.values() if until > now)


class SqliteSlidingWindowLimiter:
    """:class:`SlidingWindowLimiter` 的 SQLite 版：计数跨进程共享、重启不清零（P2-1）。

    接口与进程内版一致（``hit`` / ``reset`` / ``enabled``），在 api.py 里按
    配置二选一，调用方无感。把计数放进 SQLite 带来三个**必要**的变化：

    * **时钟换成墙钟**（``time.time()``）：``monotonic`` 的起点是每个进程
      自己的，跨进程共享的窗口绝不能用。代价是限流器开始依赖系统时钟 ——
      NTP 回跳会拉长窗口。两害相权：跨进程是真实需求，时钟回跳是罕见事件；
    * 「查计数 + 记一笔」放进同一条 ``BEGIN IMMEDIATE`` 事务：多进程并发下
      两个进程同时数出"还剩一个名额"再都写入，就会超发 —— 写锁把这两步
      变成不可分割的动作（WAL 下写锁串行，代价可接受）；
    * 清理顺手做：每次 hit 先剪本 key 的过期记录，每 32 次 hit 全局扫一遍 ——
      与内存版 ``_sweep`` 同一条理由：不清就是慢性泄漏，只是从内存换成磁盘。

    边界照实说：它解决的是**同机多进程**（uvicorn --workers、脚本与服务并存），
    跨机器的多副本仍需 Redis / 共享数据库（P2）。登录失败锁定（LoginThrottle）
    维持进程内版本 —— 锁定语义（窗口 + 锁定两段状态）搬进 SQL 的复杂度
    与它的收益不成比例，等真有跨机部署需求时一起做。
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float = 60.0,
        *,
        path: str,
    ) -> None:
        self.limit = limit
        self.window = window_seconds
        self._path = path
        self._prunes = 0
        init_state_db(path)  # 幂等：与 SessionStore 同一约定，存储自建表
        # 由组件自己上报后端，与内存版同一条约定。
        record_state_backend("ratelimit", "sqlite")

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def hit(self, key: str) -> tuple[bool, int]:
        """记一次访问。返回 ``(是否放行, 建议等待秒数)``。``limit <= 0`` 关闭。"""
        if not self.enabled:
            return True, 0

        now = time.time()
        cutoff = now - self.window
        conn = connect(self._path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM rate_hit WHERE key = ? AND ts <= ?", (key, cutoff))
            rows = conn.execute(
                "SELECT ts FROM rate_hit WHERE key = ? ORDER BY ts", (key,)
            ).fetchall()
            if len(rows) >= self.limit:
                # 最早那次请求滑出窗口时就能再试
                oldest = rows[0][0]
                retry_after = max(1, int(self.window - (now - oldest)) + 1)
                conn.execute("COMMIT")  # 拒绝也要提交 —— 上面的剪枝是有效操作
                return False, retry_after
            conn.execute("INSERT INTO rate_hit (key, ts) VALUES (?, ?)", (key, now))
            # 全局清扫：低频做，扫掉「再也不会被 hit 到的 key」的陈年记录。
            self._prunes += 1
            if self._prunes % 32 == 0:
                conn.execute("DELETE FROM rate_hit WHERE ts <= ?", (cutoff,))
            conn.execute("COMMIT")
            return True, 0
        finally:
            conn.close()

    def reset(self, key: str | None = None) -> None:
        """清空计数。测试与运维用。"""
        conn = connect(self._path)
        try:
            if key is None:
                conn.execute("DELETE FROM rate_hit")
            else:
                conn.execute("DELETE FROM rate_hit WHERE key = ?", (key,))
        finally:
            conn.close()

    def tracked_keys(self) -> int:
        """当前跟踪的 key 数（用于验证不会无界增长）。"""
        conn = connect(self._path)
        try:
            row = conn.execute("SELECT COUNT(DISTINCT key) FROM rate_hit").fetchone()
            return int(row[0] if row else 0)
        finally:
            conn.close()
