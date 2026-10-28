"""项目七（销售素材推广与客户激活）的 HTTP 接口。

接口按业务动作分组：
    /channels                  渠道语感规格（前端提示、运营对齐用）
    /materials                 素材生成与素材库
    /cases                     成单案例脱敏包装
    /activation/*              未成单客户激活（计划 / 任务 / 发送）
    /audit /audits             合规审核与审计流水
    /attribute /touch          效果归因与触达上报
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/marketing", tags=["marketing"])


def _ctx():
    from app_context import get_app_context

    return get_app_context()


# =========================================================================== #
# 请求模型
# =========================================================================== #
class GenerateRequest(BaseModel):
    channel: str = Field(..., description="moments | douyin | xiaohongshu")
    theme: str = Field("", description="主题，如 债务置换 / 案例见证 / 费用透明；留空则用模板默认")
    count: int = Field(1, ge=1, le=20, description="生成条数")


class CaseRequest(BaseModel):
    channel: str = Field("moments", description="目标渠道")


class ActivationRequest(BaseModel):
    limit: int = Field(10, ge=1, le=50, description="最多生成多少条激活任务")


class SendRequest(BaseModel):
    customer_id: str = Field(..., description="客户编号，如 C1001")


class AuditRequest(BaseModel):
    text: str = Field(..., description="待审文本（素材正文 / 业务员自己写的话术）")
    ref_id: str = Field("", description="关联对象编号（素材 id 等），便于审计追溯")


class AttributeRequest(BaseModel):
    material_id: str = Field("", description="只看某条素材；留空看全部")


class TouchRequest(BaseModel):
    material_id: str = Field(..., description="素材编号")
    stage: str = Field(..., description="impression | consult | lead | deal")
    channel: str = Field("moments", description="渠道")
    customer_id: str = Field("", description="客户编号（可空）")


# =========================================================================== #
# 渠道规格
# =========================================================================== #
@router.get("/channels", summary="各渠道语感规格（字数/话题数/钩子风格）")
def channels() -> dict[str, Any]:
    specs = _ctx().marketing.channel_specs()
    return {"count": len(specs), "specs": specs}


# =========================================================================== #
# 素材
# =========================================================================== #
@router.post("/materials", summary="批量生成素材（生成即过合规与质量分）")
def generate(req: GenerateRequest) -> dict[str, Any]:
    ctx = _ctx()
    try:
        drafts = ctx.marketing.generate_materials(
            req.channel, theme=req.theme, count=req.count  # type: ignore[arg-type]
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "count": len(drafts),
        "channel": req.channel,
        "rejected": sum(1 for d in drafts if d.status == "rejected"),
        "needs_human": sum(1 for d in drafts if d.compliance and d.compliance.needs_human),
        "materials": [d.model_dump(mode="json") for d in drafts],
        "texts": [d.to_text() for d in drafts],
    }


@router.get("/materials", summary="素材库列表")
def list_materials(channel: str = "", status: str = "", limit: int = 50) -> dict[str, Any]:
    drafts = _ctx().marketing.store.list_materials(channel=channel, status=status, limit=limit)
    return {
        "count": len(drafts),
        "materials": [d.model_dump(mode="json") for d in drafts],
        "texts": [d.to_text() for d in drafts],
    }


# =========================================================================== #
# 案例
# =========================================================================== #
@router.post("/cases", summary="成单案例脱敏包装（只有成交对话可入库）")
def pack_cases(req: CaseRequest) -> dict[str, Any]:
    ctx = _ctx()
    drafts = ctx.marketing.pack_cases(channel=req.channel)  # type: ignore[arg-type]
    return {
        "count": len(drafts),
        "cases": [c.model_dump(mode="json") for c in ctx.marketing.cases],
        "materials": [d.model_dump(mode="json") for d in drafts],
        "texts": [d.to_text() for d in drafts],
    }


# =========================================================================== #
# 激活
# =========================================================================== #
@router.post("/activation/plan", summary="生成未成单客户激活计划（含防骚扰过滤）")
def plan_activation(req: ActivationRequest) -> dict[str, Any]:
    plan = _ctx().marketing.plan_activation(limit=req.limit)
    payload = plan.model_dump(mode="json")
    payload["plan_text"] = plan.to_text()
    return payload


@router.get("/activation/tasks", summary="激活任务列表（按优先级排序）")
def activation_tasks(limit: int = 50) -> dict[str, Any]:
    tasks = _ctx().marketing.store.list_activation_tasks(limit=limit)
    return {"count": len(tasks), "tasks": [t.model_dump(mode="json") for t in tasks]}


@router.post("/activation/send", summary="发送素材给客户（高风险动作，需人工审批）")
def send_to_customer(req: SendRequest) -> dict[str, Any]:
    """走平台 HITL：让 Agent 发起对外发送，返回 interrupt 等待人工审批。

    这里刻意**不**直接调用发送工具，而是交给编排平台 —— 因为发送是
    high 风险 + 非幂等动作，必须经过审批检查点，且超时只能中止/升级，
    绝不自动放行。
    """
    ctx = _ctx()
    result = ctx.runtime.invoke(
        "marketing",
        f"把激活素材发给客户 {req.customer_id}",
        context={"customer_id": req.customer_id},
    )
    return result.model_dump(mode="json")


# =========================================================================== #
# 合规
# =========================================================================== #
@router.post("/audit", summary="合规审核（先审后发）")
def audit(req: AuditRequest) -> dict[str, Any]:
    report = _ctx().marketing.audit(req.text, ref_id=req.ref_id)
    payload = report.model_dump(mode="json")
    payload["report_text"] = report.to_text()
    return payload


@router.get("/audits", summary="合规审核流水（可供监管审计）")
def audits(ref_id: str = "", limit: int = 50) -> dict[str, Any]:
    store = _ctx().marketing.store
    rows = store.audits(ref_id=ref_id, limit=limit)
    return {"count": len(rows), "audits": rows, "stats": store.audit_stats()}


# =========================================================================== #
# 归因
# =========================================================================== #
@router.post("/attribute", summary="素材效果归因（渠道线索占比 / 最佳素材）")
def attribute(req: AttributeRequest) -> dict[str, Any]:
    report = _ctx().marketing.attribute(material_id=req.material_id)
    payload = report.model_dump(mode="json")
    payload["report_text"] = report.to_text()
    return payload


@router.post("/touch", summary="上报一次素材触达（曝光/咨询/留资/成单）")
def touch(req: TouchRequest) -> dict[str, Any]:
    if req.stage not in ("impression", "consult", "lead", "deal"):
        raise HTTPException(status_code=422, detail=f"非法 stage：{req.stage}")
    record = _ctx().marketing.record_touch(
        req.material_id, req.stage, channel=req.channel, customer_id=req.customer_id  # type: ignore[arg-type]
    )
    return record.model_dump(mode="json")


# =========================================================================== #
# 统计
# =========================================================================== #
@router.get("/stats", summary="素材推广服务统计")
def stats() -> dict[str, Any]:
    return _ctx().marketing.stats()
