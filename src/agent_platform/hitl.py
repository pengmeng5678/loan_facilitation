"""Human-in-the-loop（人工审批）机制。

对应简历里的"高风险动作必须过 HITL 检查点"：
    - 哪些动作要审批？由 ApprovalPolicy 决定（高风险工具、非幂等写操作…）
    - 审批超时怎么办？按配置降级：abort（放弃并通知）/ escalate（挂起并升级提醒）
      —— 关键是"绝不自作主张执行"，这是过度代理（Excessive Agency）的工程约束。
    - 审批记录全程留痕，满足金融行业审计要求。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from agent_platform.logging_config import get_logger
from agent_platform.tools.spec import ToolSpec

logger = get_logger(__name__)

TimeoutStrategy = Literal["abort", "escalate"]


class ApprovalSubjectMismatch(RuntimeError):
    """审批单绑定的执行参数与本次要执行的参数不一致。

    审批的是"这个动作 + 这组参数"，恢复重放时若参数变了，旧审批绝不能再放行
    —— 否则等于让审批人给没看过的内容背书。
    """


@dataclass
class ApprovalRequest:
    """一条待审批请求。"""

    request_id: str
    run_id: str
    thread_id: str
    tool: str
    arguments: dict[str, Any]
    risk_level: str
    reason: str
    created_at: float
    expires_at: float
    status: str = "pending"  # pending | approved | rejected | timeout | escalated
    decided_by: str | None = None
    decided_at: float | None = None
    comment: str | None = None
    subject_hash: str = ""
    escalated_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "tool": self.tool,
            "arguments": self.arguments,
            "risk_level": self.risk_level,
            "reason": self.reason,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "comment": self.comment,
            "subject_hash": self.subject_hash,
            "escalated_at": self.escalated_at,
            "expired": self.is_expired,
        }

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at


class ApprovalStore(Protocol):
    """审批单存储协议。

    内存实现只用于测试/离线演示；生产注入 SQLite 实现，
    保证"进程重启后待办还在、多个 worker 看到同一份待办"。
    """

    def get(self, request_id: str) -> ApprovalRequest | None: ...

    def put(self, req: ApprovalRequest) -> None: ...

    def list_all(self, limit: int = 200) -> list[ApprovalRequest]: ...


class InMemoryApprovalStore:
    """进程内审批单存储（测试 / 单机演示）。"""

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}

    def get(self, request_id: str) -> ApprovalRequest | None:
        return self._requests.get(request_id)

    def put(self, req: ApprovalRequest) -> None:
        self._requests[req.request_id] = req

    def list_all(self, limit: int = 200) -> list[ApprovalRequest]:
        return list(self._requests.values())[:limit]

    def clear(self) -> None:
        self._requests.clear()


@dataclass
class ApprovalPolicy:
    """审批规则表。

    默认策略（可覆盖）：
        - risk_level == "high"          → 必须审批
        - risk_level == "medium" 且非幂等 → 必须审批
        - 显式列在 always_require 里的工具 → 必须审批
    """

    always_require: set[str] = field(default_factory=set)
    approve_medium: bool = True

    def requires_approval(self, spec: ToolSpec) -> tuple[bool, str]:
        """返回 (是否需要审批, 原因)。原因会展示给审批人，所以要说清楚。"""
        if spec.name in self.always_require:
            return True, f"工具 {spec.name} 在强制审批名单中"
        if spec.risk_level == "high":
            return True, f"高风险动作（risk=high，非幂等={not spec.idempotent}），需人工确认后执行"
        if spec.risk_level == "medium" and self.approve_medium and not spec.idempotent:
            return True, f"中风险且非幂等（{spec.name}），需人工确认"
        return False, ""


class ApprovalManager:
    """审批请求的创建、查询与决策。

    存储通过 `ApprovalStore` 注入：默认内存实现，生产注入 SQLite 实现，
    这样"进程重启后待办还在、多个 worker 看到同一份待办"才成立。
    """

    def __init__(
        self,
        policy: ApprovalPolicy | None = None,
        timeout_seconds: float = 300.0,
        timeout_strategy: TimeoutStrategy = "abort",
        *,
        store: ApprovalStore | None = None,
        notifier: Any | None = None,
        metrics: Any | None = None,
    ) -> None:
        self.policy = policy or ApprovalPolicy()
        self.timeout_seconds = timeout_seconds
        self.timeout_strategy = timeout_strategy
        self._store: ApprovalStore = store or InMemoryApprovalStore()
        self._notifier = notifier
        self._metrics = metrics

    def _incr(self, name: str, **labels: Any) -> None:
        if self._metrics is not None:
            self._metrics.incr(name, **labels)

    def requires_approval(self, spec: ToolSpec) -> tuple[bool, str]:
        return self.policy.requires_approval(spec)

    def create(
        self,
        *,
        run_id: str,
        thread_id: str,
        spec: ToolSpec,
        arguments: dict[str, Any],
        reason: str,
        subject_hash: str = "",
    ) -> ApprovalRequest:
        now = time.time()
        req = ApprovalRequest(
            request_id=f"apr_{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            thread_id=thread_id,
            tool=spec.name,
            arguments=arguments,
            risk_level=spec.risk_level,
            reason=reason,
            created_at=now,
            expires_at=now + self.timeout_seconds,
            subject_hash=subject_hash,
        )
        self._store.put(req)
        logger.info("创建审批请求 %s：工具=%s 原因=%s", req.request_id, spec.name, reason)
        return req

    def create_or_get(
        self,
        request_id: str,
        *,
        run_id: str,
        thread_id: str,
        spec: ToolSpec,
        arguments: dict[str, Any],
        reason: str,
        subject_hash: str = "",
    ) -> tuple[ApprovalRequest, bool]:
        """幂等地创建审批请求，返回 (请求, 是否新建)。

        **为什么必须幂等**：LangGraph 在 HITL 恢复时会**重放整个节点**，
        如果这里每次都新建请求，审批中心会看到一堆重复待办、指标也会被重复计数。
        所以 request_id 必须由调用方按 (thread_id, step, tool, 参数指纹) 确定性生成。
        """
        existing = self._store.get(request_id)
        if existing is not None:
            return existing, False

        now = time.time()
        req = ApprovalRequest(
            request_id=request_id,
            run_id=run_id,
            thread_id=thread_id,
            tool=spec.name,
            arguments=arguments,
            risk_level=spec.risk_level,
            reason=reason,
            created_at=now,
            expires_at=now + self.timeout_seconds,
            subject_hash=subject_hash,
        )
        self._store.put(req)
        logger.info("创建审批请求 %s：工具=%s 原因=%s", request_id, spec.name, reason)
        return req, True

    def get(self, request_id: str) -> ApprovalRequest | None:
        return self._store.get(request_id)

    def assert_subject(self, req: ApprovalRequest, arguments: dict[str, Any]) -> None:
        """校验审批单绑定的参数与本次执行参数一致。

        审批的是"这个动作 + 这组参数"；参数变了就必须重新审批，
        否则等于让审批人给没看过的内容背书。
        """
        if req.arguments != arguments:
            self._incr("approval_subject_mismatch_total")
            raise ApprovalSubjectMismatch(
                f"审批单 {req.request_id} 绑定的参数与本次执行参数不一致，已拒绝执行"
            )

    def resolve(
        self,
        request_id: str,
        *,
        approved: bool,
        decided_by: str = "unknown",
        comment: str | None = None,
    ) -> ApprovalRequest:
        """记录审批决策。"""
        req = self._store.get(request_id)
        if req is None:
            raise KeyError(f"审批请求不存在：{request_id}")
        if req.status != "pending":
            return req  # 幂等：重复提交不改变已定结果
        if req.is_expired:
            return self._apply_timeout(req)

        req.status = "approved" if approved else "rejected"
        req.decided_by = decided_by
        req.decided_at = time.time()
        req.comment = comment
        self._store.put(req)
        logger.info("审批请求 %s 结论=%s 审批人=%s", request_id, req.status, decided_by)
        return req

    def apply_timeout(self, request_id: str) -> ApprovalRequest:
        """外部（如定时任务）触发超时处理。"""
        req = self._store.get(request_id)
        if req is None:
            raise KeyError(f"审批请求不存在：{request_id}")
        if req.status != "pending":
            return req
        return self._apply_timeout(req)

    def expire_overdue(self, now: float | None = None) -> list[ApprovalRequest]:
        """扫描所有超时未决审批，按策略降级。供调度循环周期调用。"""
        ts = time.time() if now is None else now
        expired: list[ApprovalRequest] = []
        for req in self._store.list_all(limit=10000):
            if req.status == "pending" and req.expires_at <= ts:
                expired.append(self._apply_timeout(req))
        return expired

    def _apply_timeout(self, req: ApprovalRequest) -> ApprovalRequest:
        """超时降级。

        abort     —— 放弃执行（低风险场景）
        escalate  —— 保持不可执行并升级提醒（高风险场景）
        两种策略都不会"自动批准"，这是设计的底线。
        """
        now = time.time()
        if self.timeout_strategy == "escalate":
            req.status = "escalated"
            req.escalated_at = now
            self._notify_escalation(req)
        else:
            req.status = "timeout"
        req.decided_at = now
        req.comment = f"审批超时，按策略 {self.timeout_strategy} 处理"
        self._store.put(req)
        logger.warning("审批请求 %s 超时，策略=%s", req.request_id, self.timeout_strategy)
        return req

    def _notify_escalation(self, req: ApprovalRequest) -> None:
        if self._notifier is None:
            return
        try:
            notify = getattr(self._notifier, "notify_supervisor", None)
            if callable(notify):
                notify(req)
        except Exception as exc:  # noqa: BLE001 - 通知失败不能影响审批状态机
            logger.error("审批升级通知失败 %s：%s", req.request_id, exc)

    def pending(self, limit: int = 200) -> list[ApprovalRequest]:
        return [r for r in self._store.list_all(limit=limit) if r.status == "pending"]

    def all(self, limit: int = 200) -> list[ApprovalRequest]:
        return self._store.list_all(limit=limit)

    def clear(self) -> None:
        clear = getattr(self._store, "clear", None)
        if callable(clear):
            clear()
