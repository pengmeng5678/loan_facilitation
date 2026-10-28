"""HITL 审批门（项目八"人机边界"的代码级落地）。

**为什么需要一个"门"，而不是只依赖平台的高风险工具标记**：
平台层（项目五）的 HITL 是在 Agent 编排图里插入审批检查点；
本项目是**流程自动化**，大量动作由定时任务 / 事件触发（SOP 扫描、
邀约确认、到店提醒），根本不经过 Agent。这些路径同样需要
"高风险动作必须人批"—— 审批门因此是服务级设施，与触发路径无关。

**三条铁律**（对应简历"过度代理防护"）：

1. **没有 approved 记录就没有执行**。`assert_approved` 是执行前的
   唯一通行证，pending / rejected / expired 一律拒绝；
2. **过期只能 expired，绝不自动批准**。"没人批"的正确结果是
   "这事没发生"，不是"系统替人批了"；
3. **人工节点是 Agent 的禁区**。`reception / negotiation /
   fee_confirmation` 三个阶段，Agent 只能"标记待人工 + 准备材料"。
"""

from __future__ import annotations

import time
import uuid

from agent_platform.config import Settings
from agent_platform.logging_config import get_logger
from flow_ops.domain import ApprovalRequest, ApprovalStatus, HumanStage
from flow_ops.leads import FlowError
from flow_ops.store import FlowStore

logger = get_logger(__name__)

#: 高风险动作的默认说明（配置可覆盖集合，但说明写死在代码里保证可读）
ACTION_DESCRIPTIONS: dict[str, str] = {
    "send_contract": "发送合同/协议给客户",
    "confirm_fee": "向客户确认最终费用",
    "external_send": "对外发送含承诺性内容的消息",
    "sign": "签署文件",
}


class ApprovalGate:
    """高风险动作审批门。"""

    def __init__(self, store: FlowStore, settings: Settings) -> None:
        self._store = store
        self._settings = settings
        self._high_risk = settings.flow_high_risk_action_set()

    # ------------------------------------------------------------------ #
    # 规则查询
    # ------------------------------------------------------------------ #
    def is_high_risk(self, action: str) -> bool:
        return action in self._high_risk

    def high_risk_actions(self) -> list[str]:
        return sorted(self._high_risk)

    def describe(self, action: str) -> str:
        return ACTION_DESCRIPTIONS.get(action, "高风险动作")

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def request(
        self,
        action: str,
        *,
        lead_id: str,
        payload: dict | None = None,
        requester: str = "agent",
        reason: str = "",
        ttl_hours: int = 24,
        now: float | None = None,
    ) -> ApprovalRequest:
        """发起审批请求（状态 pending）。"""
        ts = now if now is not None else time.time()
        appr = ApprovalRequest(
            approval_id=f"ap_{uuid.uuid4().hex[:12]}",
            action=action,
            lead_id=lead_id,
            payload=dict(payload or {}),
            requester=requester,
            reason=reason or self.describe(action),
            status=ApprovalStatus.PENDING,
            created_at=ts,
            expires_at=ts + ttl_hours * 3600,
        )
        self._store.insert_approval(appr)
        logger.info(
            "审批请求：%s action=%s lead=%s requester=%s",
            appr.approval_id, action, lead_id, requester,
        )
        return appr

    def decide(
        self,
        approval_id: str,
        *,
        approver: str,
        approved: bool,
        comment: str = "",
        now: float | None = None,
    ) -> ApprovalRequest:
        """人工审批。拒绝时同样要留痕（谁拒的、为什么）。"""
        ts = now if now is not None else time.time()
        appr = self._store.get_approval(approval_id)
        if appr is None:
            raise FlowError("approval_not_found", f"审批不存在：{approval_id}")
        if appr.status.is_final:
            raise FlowError(
                "approval_not_found",
                f"审批已终结（{appr.status.value}），不能重复决策：{approval_id}",
            )
        appr.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        appr.decided_at = ts
        appr.decided_by = approver
        appr.decision_comment = comment
        self._store.update_approval(appr)
        logger.info(
            "审批决策：%s → %s by=%s comment=%s",
            approval_id, appr.status.value, approver, comment,
        )
        return appr

    def assert_approved(self, approval_id: str, *, now: float | None = None) -> ApprovalRequest:
        """执行前的唯一通行证。

        调用方（工具/流程引擎）在执行高风险动作**之前**必须调它。
        任何非 approved 状态都抛错 —— 宁可事情没办成，
        也不能出现"没批就执行"。
        """
        ts = now if now is not None else time.time()
        appr = self._store.get_approval(approval_id)
        if appr is None:
            raise FlowError("approval_not_found", f"审批不存在：{approval_id}")
        if appr.status == ApprovalStatus.PENDING and appr.expires_at and ts > appr.expires_at:
            # 顺手把过期的标记掉（只标记，不批准）
            appr.status = ApprovalStatus.EXPIRED
            self._store.update_approval(appr)
            raise FlowError(
                "approval_required",
                f"审批已过期（过期不等于批准）：{approval_id}",
            )
        if appr.status != ApprovalStatus.APPROVED:
            raise FlowError(
                "approval_required",
                f"动作未获批准（当前状态 {appr.status.value}）：{approval_id}",
            )
        return appr

    def mark_executed(self, approval_id: str, *, now: float | None = None) -> ApprovalRequest:
        """标记高风险动作已实际执行（发合同/发费用确认真的发出去了）。

        为什么要单独记：**批准和执行是两件事**。
        只记 status 的话，"主管批了但还没发"和"批了也发了"在数据上
        长得一模一样，出了纠纷分不清责任；审批中心的页面也正是靠
        这个字段把「已批准待执行」单独列出来，避免业务员以为批完就完事。
        重复调用不覆盖首次执行时间（幂等）。
        """
        ts = now if now is not None else time.time()
        appr = self._store.get_approval(approval_id)
        if appr is None:
            raise FlowError("approval_not_found", f"审批不存在：{approval_id}")
        if appr.executed_at:
            return appr
        appr.executed_at = ts
        self._store.update_approval(appr)
        logger.info("高风险动作已执行：%s（%s）", approval_id, appr.action)
        return appr

    def expire_overdue(self, *, now: float | None = None) -> list[str]:
        """把超时的 pending 标记为 expired。返回被过期的审批 ID 列表。

        **刻意不做的事**：不自动批准、不自动拒绝。
        过期只是让"没人管的事"不再挡路，决定权永远在人。
        """
        ts = now if now is not None else time.time()
        expired: list[str] = []
        for appr in self._store.list_approvals(status=ApprovalStatus.PENDING.value, limit=500):
            if appr.expires_at and ts > appr.expires_at:
                appr.status = ApprovalStatus.EXPIRED
                self._store.update_approval(appr)
                expired.append(appr.approval_id)
        if expired:
            logger.info("审批过期：%s", expired)
        return expired

    def pending(self, limit: int = 100) -> list[dict]:
        return [a.to_dict() for a in self._store.list_approvals(status="pending", limit=limit)]

    def get(self, approval_id: str) -> ApprovalRequest | None:
        return self._store.get_approval(approval_id)

    # ------------------------------------------------------------------ #
    # 人工节点
    # ------------------------------------------------------------------ #
    @staticmethod
    def assert_not_human_stage(human_stage: HumanStage | None, action: str) -> None:
        """Agent 侧调用：试图在人工节点上执行动作时直接拒绝。

        人工节点允许的"操作"只有两种，且都不经过这里：
            ① 标记待人工（request_handoff）
            ② 准备材料（readiness / fee 预估，是给人看的建议，不是决定）
        """
        if human_stage is not None:
            raise FlowError(
                "human_stage",
                f"当前处于人工节点（{human_stage.label}），"
                f"Agent 不得执行 {action}；请转人工处理",
            )
