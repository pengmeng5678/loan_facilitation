"""项目八的 HTTP 接口（前缀 /flow）。

分六组，与 service 门面对齐：

    /leads              线索录入与查询（含幂等键头）
    /leads/{id}/advance 阶段推进（人工操作）
    /leads/{id}/invite  邀约到店
    /leads/{id}/readiness  到店前准备
    /leads/{id}/fee     费用预估
    /sop/alerts         SOP 预警（主管待办）
    /approvals          高风险审批（HITL）
    /progress           客户自助查进度（身份校验 + 脱敏）
    /audit              审计留痕
    /metrics            流程指标
    /eval               Golden Set 与准入门禁

**幂等的 HTTP 约定**：所有写接口接受 `Idempotency-Key` 请求头
（Stripe 同款约定）。没带则按业务语义自动生成键（客户编号 + 动作），
双保险：客户端重试和消息重投都安全。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from agent_platform.logging_config import get_logger
from flow_ops.leads import FlowError
from flow_ops.service import FlowOpsService

logger = get_logger(__name__)

router = APIRouter(prefix="/flow", tags=["flow"])


# --------------------------------------------------------------------------- #
# 依赖注入
# --------------------------------------------------------------------------- #
def _service() -> FlowOpsService:
    from app_context import get_app_context

    return get_app_context().flow_ops


def _flow_error(exc: FlowError) -> HTTPException:
    """领域异常 → HTTP 状态码。业务拒绝不是 500。"""
    status_map = {
        "lead_not_found": 404,
        "approval_not_found": 404,
        "forbidden": 403,
        "lock_busy": 409,
        "lock_unavailable": 503,  # Redis 锁服务不可用：可重试，未做任何修改
        "illegal_transition": 409,
        "human_stage": 409,
        "approval_required": 409,
        "duplicate_lead": 409,
    }
    return HTTPException(status_code=status_map.get(exc.code, 400), detail=exc.message)


# --------------------------------------------------------------------------- #
# 请求模型
# --------------------------------------------------------------------------- #
class IntakeLeadRequest(BaseModel):
    customer_id: str = Field(description="客户编号，如 C1002")
    customer_name: str
    phone_masked: str = Field(description="脱敏手机号，如 138****5678")
    source: str = Field(default="douyin", description="留资渠道")
    owner: str = Field(default="rep_01", description="负责业务员")
    note: str = ""


class AdvanceRequest(BaseModel):
    to_stage: str = Field(description="目标阶段，如 assigned/first_called/...")
    reason: str = ""


class InviteRequest(BaseModel):
    store: str = "南京旗舰店"
    scheduled_at: float = Field(description="预约时间（Unix 秒）")
    note: str = ""


class FeeRequest(BaseModel):
    loan_amount: float = Field(gt=0, description="意向放款金额（元）")
    months: int | None = None
    annual_rate: float | None = None


class HandoffRequest(BaseModel):
    human_stage: str = Field(description="reception / negotiation / fee_confirmation")
    note: str = ""


class HighRiskRequest(BaseModel):
    reason: str = ""


class DecideRequest(BaseModel):
    approver: str
    approved: bool
    comment: str = ""


class ProgressQueryRequest(BaseModel):
    lead_id: str
    phone_last4: str = Field(min_length=4, max_length=4)


# --------------------------------------------------------------------------- #
# 线索
# --------------------------------------------------------------------------- #
@router.post("/leads", summary="录入线索（幂等）")
def intake_lead(
    body: IntakeLeadRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    svc = _service()
    try:
        return svc.intake_lead(
            customer_id=body.customer_id, customer_name=body.customer_name,
            phone_masked=body.phone_masked, source=body.source, owner=body.owner,
            note=body.note, idem_key=idempotency_key or "",
        )
    except FlowError as exc:
        raise _flow_error(exc) from exc


@router.get("/leads", summary="线索列表")
def list_leads(stage: str = "", owner: str = "", limit: int = 100) -> dict[str, Any]:
    leads = _service().leads(stage=stage, owner=owner, limit=limit)
    return {"count": len(leads), "leads": leads}


@router.get("/leads/{lead_id}", summary="线索详情（含流转与邀约）")
def lead_detail(lead_id: str) -> dict[str, Any]:
    detail = _service().lead_detail(lead_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"线索不存在：{lead_id}")
    return detail


@router.post("/leads/{lead_id}/advance", summary="推进阶段（人工操作）")
def advance_lead(lead_id: str, body: AdvanceRequest) -> dict[str, Any]:
    try:
        return _service().advance(lead_id, body.to_stage, reason=body.reason)
    except FlowError as exc:
        raise _flow_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"阶段名不合法：{exc}") from exc


@router.post("/leads/{lead_id}/invite", summary="邀约到店（幂等）")
def invite(
    lead_id: str,
    body: InviteRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    try:
        return _service().invite(
            lead_id, store=body.store, scheduled_at=body.scheduled_at,
            note=body.note, idem_key=idempotency_key or "",
        )
    except FlowError as exc:
        raise _flow_error(exc) from exc


@router.post("/leads/{lead_id}/arrived", summary="到店确认（幂等）")
def confirm_arrival(
    lead_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    try:
        return _service().confirm_arrival(lead_id, idem_key=idempotency_key or "")
    except FlowError as exc:
        raise _flow_error(exc) from exc


@router.post("/leads/{lead_id}/readiness", summary="到店前准备（一页纸）")
def prepare_arrival(lead_id: str) -> dict[str, Any]:
    try:
        return _service().prepare_arrival(lead_id)
    except FlowError as exc:
        raise _flow_error(exc) from exc


@router.post("/leads/{lead_id}/fee", summary="费用预估")
def estimate_fee(lead_id: str, body: FeeRequest) -> dict[str, Any]:
    try:
        return _service().estimate_fee(
            lead_id, loan_amount=body.loan_amount, months=body.months,
            annual_rate=body.annual_rate,
        )
    except FlowError as exc:
        raise _flow_error(exc) from exc


@router.post("/leads/{lead_id}/handoff", summary="转人工节点")
def request_handoff(lead_id: str, body: HandoffRequest) -> dict[str, Any]:
    try:
        return _service().request_handoff(lead_id, body.human_stage, note=body.note)
    except FlowError as exc:
        raise _flow_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"人工节点名不合法：{exc}") from exc


# --------------------------------------------------------------------------- #
# SOP
# --------------------------------------------------------------------------- #
@router.get("/sop/alerts", summary="SOP 预警列表")
def list_alerts(include_acknowledged: bool = False, limit: int = 100) -> dict[str, Any]:
    alerts = _service().alerts(include_acknowledged=include_acknowledged, limit=limit)
    return {"count": len(alerts), "alerts": alerts}


@router.post("/sop/sweep", summary="手动触发 SOP 扫描")
def sweep_sop() -> dict[str, Any]:
    return _service().check_sop()


@router.post("/sop/alerts/{alert_id}/ack", summary="认领预警")
def acknowledge_alert(alert_id: str, by: str = "mgr_01") -> dict[str, Any]:
    return _service().acknowledge_alert(alert_id, by=by)


# --------------------------------------------------------------------------- #
# 审批（HITL）
# --------------------------------------------------------------------------- #
@router.get("/approvals", summary="审批列表")
def list_approvals(status: str = "", limit: int = 50) -> dict[str, Any]:
    items = _service().engine.store.list_approvals(status=status, limit=limit)
    return {"count": len(items), "approvals": [a.to_dict() for a in items]}


@router.post("/leads/{lead_id}/request-contract", summary="申请发送合同（HITL）")
def request_contract(lead_id: str, body: HighRiskRequest) -> dict[str, Any]:
    try:
        return _service().request_high_risk(
            "send_contract", lead_id=lead_id, reason=body.reason,
            payload={"lead_id": lead_id},
        )
    except FlowError as exc:
        raise _flow_error(exc) from exc


@router.post("/leads/{lead_id}/confirm-fee", summary="申请确认费用（HITL）")
def request_confirm_fee(lead_id: str, body: HighRiskRequest) -> dict[str, Any]:
    try:
        return _service().request_high_risk(
            "confirm_fee", lead_id=lead_id, reason=body.reason,
            payload={"lead_id": lead_id},
        )
    except FlowError as exc:
        raise _flow_error(exc) from exc


@router.post("/approvals/{approval_id}/decide", summary="审批决策")
def decide(approval_id: str, body: DecideRequest) -> dict[str, Any]:
    try:
        return _service().decide_approval(
            approval_id, approver=body.approver, approved=body.approved, comment=body.comment
        )
    except FlowError as exc:
        raise _flow_error(exc) from exc


@router.post("/approvals/{approval_id}/execute", summary="执行已审批动作")
def execute(approval_id: str) -> dict[str, Any]:
    try:
        return _service().execute_approved(approval_id)
    except FlowError as exc:
        raise _flow_error(exc) from exc


# --------------------------------------------------------------------------- #
# 自助查询 / 审计 / 指标 / 评测
# --------------------------------------------------------------------------- #
@router.post("/progress", summary="客户自助查进度（脱敏）")
def progress_query(body: ProgressQueryRequest) -> dict[str, Any]:
    try:
        return _service().progress_query(body.lead_id, body.phone_last4)
    except FlowError as exc:
        raise _flow_error(exc) from exc


@router.get("/audit", summary="审计留痕查询")
def audit(lead_id: str = "", action: str = "", limit: int = 100) -> dict[str, Any]:
    items = _service().audit_trail(lead_id=lead_id, action=action, limit=limit)
    return {"count": len(items), "entries": items}


@router.get("/metrics", summary="流程指标")
def metrics() -> dict[str, Any]:
    return _service().metrics_snapshot()


@router.get("/stats", summary="服务统计")
def stats() -> dict[str, Any]:
    return _service().stats()


@router.post("/eval", summary="跑流程 Golden Set")
def evaluate() -> dict[str, Any]:
    return _service().evaluate()


@router.post("/eval/gate", summary="准入门禁（先取基线再跑本次）")
def gate() -> dict[str, Any]:
    return _service().gate()
