"""限流与配额。

两件事要分清，生产里常被混为一谈：
    **限流（rate limit）** —— 保护下游：每秒/每分钟最多打多少次，
                              防止把上游模型服务打挂，也防止自己被限流。
    **配额（quota）**      —— 保护自己：某业务线/某任务本月最多花多少 token，
                              防止一个跑飞的 Agent 把预算烧光。

实现上都刻意做成"进程内、零依赖"，接口对齐生产方案（Redis + 滑动窗口），
替换时只需换实现。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


class TokenBucket:
    """令牌桶。

    参数：
        capacity:          桶容量（允许的突发量）
        refill_per_second: 每秒补充的令牌数（即稳态速率）
    """

    def __init__(self, capacity: float, refill_per_second: float) -> None:
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_second)
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def allow(self, n: float = 1.0) -> bool:
        """尝试取 n 个令牌。取不到返回 False（调用方应拒绝或排队）。"""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return {
                "capacity": self.capacity,
                "refill_per_second": round(self.refill_per_second, 4),
                "tokens": round(self._tokens, 2),
            }


class RateLimiter:
    """按维度隔离的限流器（默认按模型隔离：某个模型被限流不影响其他模型）。"""

    def __init__(self, per_minute: int, burst: int | None = None) -> None:
        capacity = float(burst if burst is not None else max(1, per_minute // 5))
        self._refill = per_minute / 60.0
        self._capacity = capacity
        self._buckets: dict[str, TokenBucket] = {}
        self._rejected: dict[str, int] = {}
        self._lock = threading.Lock()

    def allow(self, dimension: str = "default") -> bool:
        with self._lock:
            bucket = self._buckets.get(dimension)
            if bucket is None:
                bucket = TokenBucket(self._capacity, self._refill)
                self._buckets[dimension] = bucket
        ok = bucket.allow(1.0)
        if not ok:
            with self._lock:
                self._rejected[dimension] = self._rejected.get(dimension, 0) + 1
        return ok

    def stats(self) -> dict[str, object]:
        # 先在锁内拷贝引用、锁外逐桶取快照：避免"外层锁持有中再去抢
        # 桶内锁"的嵌套加锁（锁顺序一旦固化在代码里，后来人很难发现）。
        with self._lock:
            buckets = list(self._buckets.items())
            rejected = dict(self._rejected)
        return {
            "refill_per_second": round(self._refill, 4),
            "capacity": self._capacity,
            "rejected": rejected,
            "dimensions": {k: v.snapshot() for k, v in buckets},
        }


@dataclass
class _Quota:
    limit: int
    used: int = 0


@dataclass
class QuotaManager:
    """按作用域（业务线 / 任务）的 token 配额。

    超配额时 consume() 返回 False —— 网关据此把请求**降级到小模型**或直接拒绝，
    而不是让某个业务把整个月的预算烧光。
    """

    limit_per_scope: int
    _quotas: dict[str, _Quota] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def remaining(self, scope: str) -> int:
        with self._lock:
            q = self._quotas.get(scope)
            if q is None:
                return self.limit_per_scope
            return max(0, q.limit - q.used)

    def consume(self, scope: str, tokens: int) -> bool:
        """占用配额。返回 False 表示已超限（调用应被拦截或降级）。"""
        if tokens <= 0:
            return True
        with self._lock:
            q = self._quotas.setdefault(scope or "default", _Quota(limit=self.limit_per_scope))
            if q.used + tokens > q.limit:
                return False
            q.used += tokens
            return True

    def stats(self) -> dict[str, object]:
        with self._lock:
            return {
                "limit_per_scope": self.limit_per_scope,
                "scopes": {
                    k: {"used": v.used, "limit": v.limit, "remaining": max(0, v.limit - v.used)}
                    for k, v in self._quotas.items()
                },
            }
