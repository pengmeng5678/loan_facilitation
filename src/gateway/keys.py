"""多密钥轮转（Key Rotation）。

为什么需要它：
    生产里单密钥很容易撞上"某个 key 被限流 / 被风控"，表现就是部分请求
    间歇性 429。多密钥轮转 + 失败冷却能把这种抖动吸收掉，业务无感。

设计要点：
    - **轮转**：round-robin 挑可用 key，避免总压同一个 key；
    - **冷却**：某 key 失败后进入冷却期，期间不再被选中（冷却结束自动恢复）；
    - **无密钥也能跑**：keys 为空时 acquire() 返回 None，调用方回落到
      "不传密钥"的离线实现 —— 这保证了本地零密钥可运行。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class KeyState:
    """单个密钥的运行态。"""

    key: str
    failures: int = 0
    successes: int = 0
    cooldown_until: float = 0.0

    def masked(self) -> str:
        """脱敏展示（日志/接口里不能出现明文密钥）。"""
        if len(self.key) <= 8:
            return "*" * len(self.key)
        return f"{self.key[:4]}****{self.key[-4:]}"

    def available(self, now: float) -> bool:
        return now >= self.cooldown_until

    def to_dict(self, now: float) -> dict[str, object]:
        return {
            "key": self.masked(),
            "failures": self.failures,
            "successes": self.successes,
            "available": self.available(now),
            "cooldown_remaining_s": round(max(0.0, self.cooldown_until - now), 1),
        }


class KeyPool:
    """密钥池。线程安全。"""

    def __init__(self, keys: list[str], *, cooldown_seconds: float = 60.0) -> None:
        self.cooldown_seconds = cooldown_seconds
        self._states: list[KeyState] = [KeyState(key=k) for k in keys]
        self._cursor = 0
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._states)

    @property
    def is_empty(self) -> bool:
        return not self._states

    def acquire(self, model: str = "") -> str | None:
        """取一个可用密钥。全部处于冷却期时返回 None（调用方决定等还是降级）。"""
        if not self._states:
            return None
        now = time.monotonic()
        with self._lock:
            n = len(self._states)
            for offset in range(n):
                idx = (self._cursor + offset) % n
                state = self._states[idx]
                if state.available(now):
                    self._cursor = (idx + 1) % n  # 下次从下一个开始 → 轮转
                    return state.key
        return None

    def report_success(self, key: str | None) -> None:
        if not key:
            return
        with self._lock:
            for s in self._states:
                if s.key == key:
                    s.successes += 1
                    s.failures = 0
                    s.cooldown_until = 0.0
                    return

    def report_failure(self, key: str | None) -> None:
        """失败 → 该 key 进入冷却，避免继续撞墙。"""
        if not key:
            return
        now = time.monotonic()
        with self._lock:
            for s in self._states:
                if s.key == key:
                    s.failures += 1
                    s.cooldown_until = now + self.cooldown_seconds
                    return

    def stats(self) -> dict[str, object]:
        now = time.monotonic()
        with self._lock:
            return {
                "total_keys": len(self._states),
                "available_keys": sum(1 for s in self._states if s.available(now)),
                "keys": [s.to_dict(now) for s in self._states],
            }
