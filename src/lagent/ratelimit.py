"""进程内滑动窗口限流。

为什么自己写而不引 slowapi：本项目已经有一条零依赖的线
（手写 BM25、手写 JWT、零构建前端），而限流的核心逻辑就是
「窗口内计数 + 剪掉过期记录」，20 行且完全可测。
真要上生产多副本时，这个实现必须换成 Redis 计数器 ——
**进程内计数在多副本下等于把配额乘以副本数**，
这一点写在这里比藏在依赖后面更清楚。

一个诚实的限制：这是单进程的，重启即清零。
所以它能防「脚本高频刷接口」，防不住分布式刷。
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
