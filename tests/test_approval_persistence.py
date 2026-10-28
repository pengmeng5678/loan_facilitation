"""审批（HITL）回归测试：持久化、参数绑定、超时策略。

要锁定的三件事：
    1. 进程重建后待办还在（内存 dict 做不到）；
    2. 审批单只对"这组参数"有效，参数变了旧审批不能放行；
    3. 超时策略真的生效：escalate 不会变成 approved。
"""

from __future__ import annotations

import pytest

from agent_platform.approvals_store import SqliteApprovalStore
from agent_platform.hitl import (
    ApprovalManager,
    ApprovalPolicy,
    ApprovalSubjectMismatch,
    InMemoryApprovalStore,
)
from agent_platform.observability import Metrics
from agent_platform.tools.spec import ToolSpec


def _spec(name: str = "crm.send_message") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="发消息",
        parameters={"type": "object", "properties": {"content": {"type": "string"}}},
        handler=lambda **_: None,
        risk_level="high",
        idempotent=False,
    )


def _create(manager: ApprovalManager, *, request_id: str = "apr_test", args=None):
    return manager.create_or_get(
        request_id,
        run_id="run-1",
        thread_id="th-1",
        spec=_spec(),
        arguments={"content": "hello"} if args is None else args,
        reason="高风险动作",
    )


def test_approval_survives_manager_rebuild(tmp_path):
    db = tmp_path / "approvals.sqlite"
    manager = ApprovalManager(store=SqliteApprovalStore(db), timeout_seconds=60)
    _create(manager)

    # 模拟进程重启：新 store + 新 manager 指向同一个库文件
    rebuilt = ApprovalManager(store=SqliteApprovalStore(db), timeout_seconds=60)
    req = rebuilt.get("apr_test")
    assert req is not None
    assert req.arguments == {"content": "hello"}
    assert [r.request_id for r in rebuilt.pending()] == ["apr_test"]


def test_approval_rejects_changed_arguments():
    metrics = Metrics()
    manager = ApprovalManager(store=InMemoryApprovalStore(), metrics=metrics)
    req, _ = _create(manager)

    with pytest.raises(ApprovalSubjectMismatch):
        manager.assert_subject(req, {"content": "changed"})
    assert metrics.counters().get("approval_subject_mismatch_total") == 1.0


def test_timeout_escalate_never_becomes_approved():
    manager = ApprovalManager(
        store=InMemoryApprovalStore(), timeout_seconds=0, timeout_strategy="escalate"
    )
    _create(manager)

    expired = manager.expire_overdue()
    assert len(expired) == 1
    assert expired[0].status == "escalated"
    assert expired[0].escalated_at is not None
    assert expired[0].status != "approved"
    # 已升级的请求再走 resolve 也不会被重新判定为 approved
    assert manager.resolve("apr_test", approved=True).status == "escalated"


def test_two_workers_share_pending(tmp_path):
    db = tmp_path / "approvals.sqlite"
    worker_a = ApprovalManager(store=SqliteApprovalStore(db), timeout_seconds=60)
    worker_b = ApprovalManager(store=SqliteApprovalStore(db), timeout_seconds=60)

    _create(worker_a)
    assert [r.request_id for r in worker_b.pending()] == ["apr_test"]

    worker_b.resolve("apr_test", approved=True, decided_by="主管B")
    assert worker_a.get("apr_test").status == "approved"


def test_policy_still_requires_approval_for_high_risk():
    need, reason = ApprovalPolicy().requires_approval(_spec())
    assert need and "high" in reason
