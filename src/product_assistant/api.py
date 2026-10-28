"""项目二（产品知识助手）的 HTTP 接口。

除了走平台编排的通用入口（POST /platform/agents/product_assistant/run），
这里额外提供"直接调用"接口 —— 业务系统对接时往往不需要跑完整 Agent，
只需要拿一个确定性的结果（比如前端产品页要一个 Q&A、CRM 要一次匹配结果）。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent_platform.errors import PolicyNotFoundError, RetrievalError

router = APIRouter(prefix="/product", tags=["product-assistant"])


def _ctx():
    from app_context import get_app_context

    return get_app_context()


# =========================================================================== #
# 请求模型
# =========================================================================== #
class QARequest(BaseModel):
    question: str = Field(..., description="业务员/客户的问题")
    product_code: str = Field("", description="限定产品编号（可选）")
    as_of: str = Field("", description="政策基准日期 YYYY-MM-DD（可选，默认今天）")


class MatchRequest(BaseModel):
    customer_id: str = Field(..., description="客户编号")
    as_of: str = Field("", description="政策基准日期 YYYY-MM-DD（可选）")


class ProposalRequest(BaseModel):
    customer_id: str = Field(..., description="客户编号")
    as_of: str = Field("", description="政策基准日期 YYYY-MM-DD（可选）")


def _parse(as_of: str) -> date | None:
    if not as_of:
        return None
    try:
        return date.fromisoformat(as_of)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"as_of 格式应为 YYYY-MM-DD：{as_of}") from exc


# =========================================================================== #
# 接口
# =========================================================================== #
@router.post("/qa", summary="产品政策问答（带引用溯源，无依据拒答）")
def product_qa(req: QARequest) -> dict[str, Any]:
    ctx = _ctx()
    try:
        answer = ctx.product.answer_question(
            req.question, as_of=_parse(req.as_of), product_code=req.product_code
        )
    except RetrievalError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return answer.model_dump(mode="json")


@router.post("/match", summary="结构化方案匹配")
def product_match(req: MatchRequest) -> dict[str, Any]:
    ctx = _ctx()
    try:
        result = ctx.product.match_customer(req.customer_id, as_of=_parse(req.as_of))
    except PolicyNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    payload = result.model_dump(mode="json")
    payload["summary"] = result.to_text()
    return payload


@router.post("/proposal", summary="生成方案书")
def product_proposal(req: ProposalRequest) -> dict[str, Any]:
    ctx = _ctx()
    try:
        proposal = ctx.product.generate_proposal(req.customer_id, as_of=_parse(req.as_of))
    except PolicyNotFoundError as exc:
        # 无匹配产品时不硬配，返回 409 让上游走人工流程
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    payload = proposal.model_dump(mode="json")
    payload["markdown"] = proposal.to_markdown()
    return payload


@router.get("/products", summary="产品目录")
def list_products() -> dict[str, Any]:
    ctx = _ctx()
    products = [p.model_dump(mode="json") for p in ctx.product.repository.active_products()]
    return {"count": len(products), "products": products}


@router.get("/products/{code}", summary="产品详情")
def get_product(code: str) -> dict[str, Any]:
    ctx = _ctx()
    product = ctx.product.repository.get_product(code)
    if product is None:
        raise HTTPException(status_code=404, detail=f"产品不存在：{code}")
    return product.model_dump(mode="json")


@router.get("/products/{code}/graph", summary="产品图谱关系（资质/用途/替代品）")
def product_graph(code: str) -> dict[str, Any]:
    ctx = _ctx()
    if ctx.product.repository.get_product(code) is None:
        raise HTTPException(status_code=404, detail=f"产品不存在：{code}")
    return {"product_code": code, **ctx.product.graph.neighbors(code)}


@router.get("/customers", summary="客户档案列表")
def list_customers() -> dict[str, Any]:
    ctx = _ctx()
    profiles = ctx.product.profiles.all()
    return {
        "count": len(profiles),
        "customers": [
            {
                "customer_id": p.customer_id,
                "name_masked": p.name_masked,
                "age": p.age,
                "city": p.city,
                "qualifications": p.qualifications,
                "needs": p.needs,
                "debt_ratio": p.debt_ratio,
            }
            for p in profiles
        ],
    }


@router.get("/customers/{customer_id}", summary="客户档案详情（含负债比）")
def get_customer(customer_id: str) -> dict[str, Any]:
    ctx = _ctx()
    profile = ctx.product.profiles.get(customer_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"客户不存在：{customer_id}")
    payload = profile.model_dump(mode="json")
    payload["debt_ratio"] = profile.debt_ratio
    return payload


@router.get("/stats", summary="知识库统计")
def stats() -> dict[str, Any]:
    return _ctx().product.stats()
