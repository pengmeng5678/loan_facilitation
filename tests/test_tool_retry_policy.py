"""工具重试与熔断的分流测试。

三条底线：
    1. 非幂等工具绝不自动重试（重放 = 真实副作用）；
    2. 永久错误（参数/类型）不重试，也不推高熔断计数；
    3. 瞬时错误才重试，并且能把熔断器打开。
"""

from __future__ import annotations

from agent_platform.observability import Metrics
from agent_platform.resilience import CircuitBreaker, RetryPolicy
from agent_platform.tools.registry import ToolRegistry
from agent_platform.tools.spec import ToolSpec


def _registry(metrics: Metrics, *, threshold: int = 5) -> ToolRegistry:
    return ToolRegistry(
        breaker=CircuitBreaker(failure_threshold=threshold),
        metrics=metrics,
    )


def _spec(name: str, handler, *, idempotent: bool = True) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="test",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        idempotent=idempotent,
    )


def _policy() -> RetryPolicy:
    return RetryPolicy(max_retries=3, base_seconds=0.0, jitter=0.0)


def test_nonidempotent_tool_is_not_retried():
    metrics = Metrics()
    reg = _registry(metrics)
    calls = {"n": 0}

    def handler(**_: object) -> dict:
        calls["n"] += 1
        raise RuntimeError("downstream down")

    reg.register(_spec("api.send_sms", handler, idempotent=False))
    result = reg.invoke("api.send_sms", {}, retry_policy=_policy())

    assert result.ok is False
    assert calls["n"] == 1
    assert result.attempts == 1
    assert metrics.counters().get("tool_retry_skipped_nonidempotent_total{tool=api.send_sms}") == 1.0


def test_transient_error_is_retried_until_success():
    metrics = Metrics()
    reg = _registry(metrics)
    calls = {"n": 0}

    def handler(**_: object) -> dict:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("reset by peer")
        return {"ok": True}

    reg.register(_spec("api.read", handler))
    result = reg.invoke("api.read", {}, retry_policy=_policy())

    assert result.ok is True
    assert calls["n"] == 3
    assert result.attempts == 3


def test_permanent_error_does_not_retry_or_trip_breaker():
    metrics = Metrics()
    reg = _registry(metrics, threshold=1)

    reg.register(_spec("api.read", lambda **_: (_ for _ in ()).throw(ValueError("bad arg"))))
    result = reg.invoke("api.read", {}, retry_policy=_policy())

    assert result.ok is False
    assert result.attempts == 1
    assert metrics.counters().get("tool_permanent_error_total{tool=api.read}") == 1.0
    # 永久错误不该把熔断器打开
    assert reg.breaker_snapshot() == {} or reg.breaker_snapshot()["api.read"]["failures"] == 0


def test_transient_failures_open_breaker_and_emit_metric():
    metrics = Metrics()
    reg = _registry(metrics, threshold=1)
    reg.register(_spec("api.down", lambda **_: (_ for _ in ()).throw(TimeoutError("timeout"))))

    reg.invoke("api.down", {}, retry_policy=_policy())

    assert reg.breaker_snapshot()["api.down"]["state"] == "open"
    assert metrics.counters().get("tool_breaker_open_total{tool=api.down}") == 1.0
