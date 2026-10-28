"""审计日志（项目八"全程留痕"的落地）。

回答的问题是：**"这条线索为什么变成现在这样？"**

监管/内审查的是链条，不是单点：谁录入了线索、谁在什么时候把它推到邀约、
费用预估基于什么口径、发合同前谁点的头。所以每条审计记录都带：
    actor        操作者（agent / 业务员 ID / system）
    action       动作（lead.intake / lead.advance / approval.decide / ...）
    target       对象（哪条线索 / 哪个审批）
    detail       人话描述（给非技术的人看）
    approval_id  关联的审批（高风险动作必须有，否则审计链条断）
    result       ok / blocked / failed / idempotent_hit

**设计取舍**：审计写在同一个 SQLite 里而不是另建服务。
留痕的价值在于"和业务操作同一个事务边界" —— 分成两个系统就会出现
"业务成功了但审计没写上"的窗口，那正是审计最致命的缺口。
"""

from __future__ import annotations

import time
import uuid

from agent_platform.logging_config import get_logger
from flow_ops.domain import AuditEntry

logger = get_logger(__name__)


class AuditLogger:
    """审计记录器。"""

    def __init__(self, store) -> None:
        self._store = store

    def log(
        self,
        *,
        actor: str,
        action: str,
        target_type: str,
        target_id: str,
        detail: str = "",
        approval_id: str = "",
        result: str = "ok",
        now: float | None = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            entry_id=f"au_{uuid.uuid4().hex[:12]}",
            ts=now if now is not None else time.time(),
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
            approval_id=approval_id,
            result=result,
        )
        self._store.insert_audit(entry)
        if result == "blocked":
            # 被拦截的动作必须显式告警：这是"系统兜底"正在工作的证据
            logger.warning("审计-拦截：%s %s/%s %s", actor, target_type, target_id, detail)
        return entry

    def trail(
        self,
        *,
        target_type: str = "",
        target_id: str = "",
        action: str = "",
        limit: int = 200,
    ) -> list[dict]:
        entries = self._store.list_audit(
            target_type=target_type, target_id=target_id, action=action, limit=limit
        )
        return [e.to_dict() for e in entries]

    def count_blocked(self) -> int:
        """被拦截的操作数（HITL / 权限 / 非法流转）。合规汇报直接取这个数。"""
        return sum(
            1 for e in self._store.list_audit(limit=10_000) if e.result == "blocked"
        )
