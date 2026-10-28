"""幂等键的跨进程语义测试。

旧实现用进程内 `threading.Lock` 包"查→执行→写"，两个 worker 仍会各执行一次。
这里锁定库级 claim/complete/release 的行为。
"""

from __future__ import annotations

import pytest

from flow_ops.idempotency import IdempotencyGuard
from flow_ops.leads import FlowError
from flow_ops.store import FlowStore


def _store(tmp_path) -> FlowStore:
    return FlowStore(tmp_path / "flow.sqlite")


def test_second_worker_gets_cached_result(tmp_path):
    store = _store(tmp_path)
    calls = {"n": 0}

    def action():
        calls["n"] += 1
        return {"lead_id": "L1"}

    guard_a = IdempotencyGuard(store, ttl_hours=24)
    guard_b = IdempotencyGuard(store, ttl_hours=24)

    first, hit_a = guard_a.run("idem_k1", action, action_name="intake")
    second, hit_b = guard_b.run("idem_k1", action, action_name="intake")

    assert hit_a is False and hit_b is True
    assert first == second == {"lead_id": "L1"}
    assert calls["n"] == 1


def test_in_progress_is_rejected_without_side_effect(tmp_path):
    store = _store(tmp_path)
    # 模拟 worker A 抢到租约但还没执行完
    assert store.claim_idempotent(
        "idem_k2", action="intake", owner="worker-a", now=100.0,
        lease_seconds=30, ttl_hours=24,
    )[0] == "claimed"

    guard_b = IdempotencyGuard(store)
    with pytest.raises(FlowError) as exc:
        guard_b.run("idem_k2", lambda: {"bad": True}, now=110.0)
    assert exc.value.code == "idem_in_progress"


def test_failure_releases_claim_for_retry(tmp_path):
    store = _store(tmp_path)
    guard = IdempotencyGuard(store)

    def boom():
        raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        guard.run("idem_k3", boom)

    # 失败已释放占位：同一键可以重试并成功
    result, hit = guard.run("idem_k3", lambda: {"ok": True})
    assert result == {"ok": True}
    assert hit is False


def test_expired_lease_can_be_taken_over(tmp_path):
    store = _store(tmp_path)
    store.claim_idempotent(
        "idem_k4", action="intake", owner="dead-worker", now=100.0,
        lease_seconds=30, ttl_hours=24,
    )

    guard = IdempotencyGuard(store)
    result, hit = guard.run("idem_k4", lambda: {"ok": True}, now=1000.0)
    assert hit is False
    assert result == {"ok": True}
