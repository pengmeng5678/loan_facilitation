"""韧性组件：重试、熔断、预算守卫。

对应简历里的"失败恢复与自愈"：
    可重试错误   → 指数退避重试
    参数错误     → 回灌给模型自修正（由编排层判断，见 graph/nodes.py）
    连续失败     → 熔断器打开，快速失败，避免雪崩
    超步数/超预算 → 熔断转人工，杜绝死循环烧钱
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from agent_platform.errors import (
    StepBudgetExceeded,
    TokenBudgetExceeded,
)
from agent_platform.logging_config import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


# =========================================================================== #
# 一、重试
# =========================================================================== #
@dataclass
class RetryPolicy:
    """指数退避重试策略。"""

    max_retries: int = 3
    base_seconds: float = 0.2
    max_seconds: float = 5.0
    jitter: float = 0.1
    """抖动系数：避免多个失败请求在同一时刻齐刷刷重试（惊群）。"""

    retry_on: tuple[type[Exception], ...] = (Exception,)

    def delay_for(self, attempt: int) -> float:
        """第 attempt 次重试前应等待的秒数（attempt 从 1 开始）。"""
        raw = self.base_seconds * (2 ** (attempt - 1))
        capped = min(raw, self.max_seconds)
        # 加入 ±jitter 的随机扰动
        return max(0.0, capped * (1 + random.uniform(-self.jitter, self.jitter)))


def retry_call(
    fn: Callable[[], T],
    policy: RetryPolicy,
    *,
    on_retry: Callable[[int, Exception], None] | None = None,
    should_retry: Callable[[Exception], bool] | None = None,
) -> tuple[T, int]:
    """执行 fn 并在可重试异常上做指数退避。

    should_retry 用于**按异常性质**分流：参数错误重试多少次都是错的，
    只会浪费时间并污染下游；只有瞬时故障（超时/连接失败/5xx）才值得重试。
    返回： (结果, 实际尝试次数)
    失败时抛出最后一次的异常（由调用方决定是降级还是熔断）。
    """
    attempt = 0
    last_exc: Exception | None = None
    while attempt <= policy.max_retries:
        attempt += 1
        try:
            return fn(), attempt
        except policy.retry_on as exc:  # type: ignore[misc]
            last_exc = exc
            if should_retry is not None and not should_retry(exc):
                raise
            if attempt > policy.max_retries:
                break
            delay = policy.delay_for(attempt)
            logger.warning("第 %d 次尝试失败（%s），%.3fs 后重试", attempt, type(exc).__name__, delay)
            if on_retry:
                on_retry(attempt, exc)
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def is_transient_error(exc: Exception) -> bool:
    """判断异常是否属于瞬时故障（值得重试 + 应计入熔断）。

    永久错误（参数/类型/键不存在/未实现）重试不会变好，也不该把
    熔断器推开放 —— 否则模型连续给错参数就会把整个工具"熔断"掉，
    真正下游挂了反而无法快速失败。
    """
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, (ValueError, TypeError, KeyError, NotImplementedError)):
        return False
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if isinstance(status, int):
        return status in (408, 429) or status >= 500
    # 未知异常按可重试处理：宁可多试一次，也不要因未知类型放弃恢复机会
    return True


# =========================================================================== #
# 二、熔断器
# =========================================================================== #
@dataclass
class _BreakerState:
    """单个被保护资源的内部状态。"""

    failures: int = 0
    opened_at: float = 0.0
    state: str = "closed"  # closed | open | half_open


@dataclass
class CircuitBreaker:
    """熔断器：连续失败达到阈值即"打开"，快速失败；冷却后进入"半开"探测。

    状态机：
        closed   ──失败达阈值──▶ open ──冷却时间到──▶ half_open
        half_open ──成功──▶ closed
        half_open ──失败──▶ open
    """

    failure_threshold: int = 5
    reset_seconds: float = 30.0
    _states: dict[str, _BreakerState] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _get(self, key: str) -> _BreakerState:
        return self._states.setdefault(key, _BreakerState())

    def allow(self, key: str) -> bool:
        """判断当前是否允许调用；不允许则抛 CircuitOpenError（快速失败）。"""
        with self._lock:
            st = self._get(key)
            if st.state == "closed":
                return True
            if st.state == "open":
                if time.time() - st.opened_at >= self.reset_seconds:
                    st.state = "half_open"  # 冷却结束，放一个探针进去试试
                    logger.info("熔断器[%s]进入半开状态，放行探测请求", key)
                    return True
                return False
            return True  # half_open：放行探测

    def record_success(self, key: str) -> None:
        with self._lock:
            st = self._get(key)
            st.failures = 0
            if st.state != "closed":
                logger.info("熔断器[%s]恢复为 closed", key)
            st.state = "closed"

    def record_failure(self, key: str) -> bool:
        """记录一次失败，返回本次是否**新打开**了熔断（供指标计数）。"""
        with self._lock:
            st = self._get(key)
            was_closed = st.state != "open"
            st.failures += 1
            if st.state == "half_open" or st.failures >= self.failure_threshold:
                st.state = "open"
                st.opened_at = time.time()
                logger.warning("熔断器[%s]打开（累计失败 %d 次）", key, st.failures)
                return was_closed
        return False

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """导出状态，供 /metrics 或排障使用。"""
        with self._lock:
            return {
                k: {"state": v.state, "failures": v.failures, "opened_at": v.opened_at}
                for k, v in self._states.items()
            }

    def reset(self) -> None:
        with self._lock:
            self._states.clear()


# =========================================================================== #
# 三、预算守卫
# =========================================================================== #
@dataclass
class BudgetGuard:
    """步数 / token 双预算守卫 —— 死循环烧钱的最后一道闸。"""

    max_steps: int = 12
    max_tokens: int = 60_000

    def check_steps(self, step: int) -> None:
        """超出步数上限即抛异常，由编排层捕获并转人工。"""
        if step >= self.max_steps:
            raise StepBudgetExceeded(
                f"已达到最大步数上限 {self.max_steps}，判定为异常循环，熔断并转人工处理。"
            )

    def check_tokens(self, tokens_used: int) -> None:
        if tokens_used >= self.max_tokens:
            raise TokenBudgetExceeded(
                f"已超出 token 预算 {self.max_tokens}，熔断并转人工处理。"
            )

    def remaining_steps(self, step: int) -> int:
        return max(0, self.max_steps - step)
