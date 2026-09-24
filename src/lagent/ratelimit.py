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
