"""平台（项目五）的 HTTP 接口。

接口设计遵循"平台只暴露能力"的原则：
    - /agents*    查看与调用已装配的业务 Agent
    - /runs*      查询/恢复一次运行（HITL 审批走这里）
    - /tools      查看工具生态与治理元数据（风险等级、是否幂等）
    - /metrics    观测指标
    - /approvals  HITL 待办
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agent_platform.errors import AgentNotFoundError

router = APIRouter(tags=["platform"])


# =========================================================================== #
# 请求 / 响应模型
# =========================================================================== #
class RunRequest(BaseModel):
    """启动一次 Agent 运行。"""

    input: str = Field(..., description="用户本轮输入")
    context: dict[str, Any] = Field(default_factory=dict, description="业务上下文（客户档案等）")
    thread_id: str | None = Field(None, description="会话 ID；相同即视为同一会话，可续跑")


class ResumeRequest(BaseModel):
    """对 HITL 挂起的运行下达审批决策。"""

    approved: bool = Field(..., description="是否批准")
    decided_by: str = Field("unknown", description="审批人")
    comment: str | None = Field(None, description="审批意见")


class RunResponse(BaseModel):
    """运行结果（字段与 AgentRuntime.RunResult 对齐，便于前端直接渲染）。"""

    run_id: str
    thread_id: str
    agent: str
    status: str
    route: str = ""
    steps: int = 0
    tokens_used: int = 0
    output: dict[str, Any] = Field(default_factory=dict)
    interrupts: list[dict[str, Any]] = Field(default_factory=list)
    pending_approval: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    approvals: list[dict[str, Any]] = Field(default_factory=list)
    latency_ms: float = 0.0
    error: str | None = None
    trajectory: list[dict[str, Any]] = Field(default_factory=list)


# =========================================================================== #
# 依赖：延迟导入 AppContext，避免与业务包形成循环引用
# =========================================================================== #
def _ctx():
    from app_context import get_app_context

    return get_app_context()


# =========================================================================== #
# 接口
# =========================================================================== #
@router.get("/agents", summary="列出已装配的业务 Agent")
def list_agents() -> dict[str, Any]:
    ctx = _ctx()
    return {"count": len(ctx.runtime.registry.all()), "agents": ctx.runtime.registry.describe_all()}


@router.get("/agents/{name}", summary="查看单个 Agent 的装配信息")
def get_agent(name: str) -> dict[str, Any]:
    ctx = _ctx()
    try:
        reg = ctx.runtime.registry.get(name)
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return reg.describe()


@router.post("/agents/{name}/run", response_model=RunResponse, summary="运行一个 Agent")
def run_agent(name: str, req: RunRequest) -> RunResponse:
    ctx = _ctx()
    try:
        ctx.runtime.registry.get(name)
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    result = ctx.runtime.invoke(
        name, req.input, context=req.context, thread_id=req.thread_id
    )
    return RunResponse(**result.model_dump())


@router.get("/runs/{thread_id}", summary="查询会话状态")
def get_run(thread_id: str) -> dict[str, Any]:
    ctx = _ctx()
    try:
        return ctx.runtime.get_state(thread_id)
    except (AgentNotFoundError, KeyError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/runs/{thread_id}/resume", response_model=RunResponse, summary="恢复挂起的运行（HITL 审批）")
def resume_run(thread_id: str, req: ResumeRequest) -> RunResponse:
    ctx = _ctx()
    try:
        result = ctx.runtime.resume(
            thread_id,
            {"approved": req.approved, "decided_by": req.decided_by, "comment": req.comment},
        )
    except (AgentNotFoundError, KeyError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RunResponse(**result.model_dump())


@router.get("/tools", summary="列出工具生态（含风险等级与幂等性）")
def list_tools() -> dict[str, Any]:
    ctx = _ctx()
    tools = ctx.runtime.tools_describe()
    return {"count": len(tools), "tools": tools}


@router.get("/metrics", summary="平台观测指标快照")
def get_metrics() -> dict[str, Any]:
    ctx = _ctx()
    return {
        "metrics": ctx.runtime.metrics_snapshot(),
        "tool_breakers": ctx.runtime.tools.breaker_snapshot(),
        "agents": ctx.runtime.registry.names(),
    }


@router.get("/approvals", summary="HITL 待审批列表")
def list_approvals() -> dict[str, Any]:
    ctx = _ctx()
    pending = [r.to_dict() for r in ctx.runtime.approvals.pending()]
    return {"count": len(pending), "pending": pending}


@router.post("/approvals/{request_id}/decide", summary="按 request_id 审批")
def decide_approval(
    request_id: str, req: ResumeRequest
) -> dict[str, Any]:
    """按审批请求 ID 直接决策（用于审批中心集中处理，需自行拿着 thread_id 恢复）。"""
    ctx = _ctx()
    try:
        record = ctx.runtime.approvals.resolve(
            request_id, approved=req.approved, decided_by=req.decided_by, comment=req.comment
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return record.to_dict()
