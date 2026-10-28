"""项目六（推理网关与可观测评测平台）的 HTTP 接口。

接口按"四件事"组织：
    /route*      路由与成本治理
    /costs /replay /stats   可观测
    /eval*       评测闭环与 CI 准入
    /calibrate   Judge 校准
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/gateway", tags=["gateway"])


def _ctx():
    from app_context import get_app_context

    return get_app_context()


# =========================================================================== #
# 请求模型
# =========================================================================== #
class RoutePreviewRequest(BaseModel):
    text: str = Field(..., description="待预览的请求文本")
    task: str = Field("", description="任务标签，如 policy_qa / material")


class DemoRequest(BaseModel):
    prompt: str = Field(..., description="用户输入")
    task: str = Field("", description="任务标签（决定默认缓存策略）")
    thread_id: str = Field("", description="会话 ID（留空自动生成）")
    cache: str = Field("", description="强制缓存策略：off / exact / semantic（留空按 task 默认）")
    tier: str = Field("", description="强制档位：small / medium / large（留空由复杂度路由决定）")


class EvalRequest(BaseModel):
    scene: str = Field("all", description="场景名或 all")
    target: str = Field("", description="指定评测目标（留空按场景自动选）")
    max_regression: float | None = Field(None, description="CI 准入允许的退化幅度")


class CalibrateRequest(BaseModel):
    judge_scores: list[float] = Field(..., description="Judge 打分（百分制）")
    human_scores: list[float] = Field(..., description="人工专家双盲打分（百分制）")
    tolerance: float = Field(10.0, description="判定一致的容差（分）")


# =========================================================================== #
# 路由与成本治理
# =========================================================================== #
@router.post("/route/preview", summary="路由预览（会走哪个档位、为什么）")
def route_preview(req: RoutePreviewRequest) -> dict[str, Any]:
    return _ctx().gateway_service.route_preview(req.text, task=req.task)


@router.post("/demo", summary="走一次真实网关调用（演示成本治理效果）")
def demo(req: DemoRequest) -> dict[str, Any]:
    cache = req.cache or None
    tier = req.tier or None
    try:
        return _ctx().gateway_service.demo_generate(
            req.prompt,
            task=req.task,
            thread_id=req.thread_id,
            cache=cache,  # type: ignore[arg-type]
            tier=tier,  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001 - 网关异常要结构化返回（限流/配额/全链失败）
        raise HTTPException(status_code=429, detail=f"{type(exc).__name__}: {exc}") from exc


# =========================================================================== #
# 可观测
# =========================================================================== #
@router.get("/costs", summary="成本报告（含每会话单价）")
def costs(thread_id: str = "") -> dict[str, Any]:
    return _ctx().gateway_service.cost_report(thread_id=thread_id)


@router.get("/replay/{thread_id}", summary="按会话回放决策轨迹")
def replay(thread_id: str) -> dict[str, Any]:
    return _ctx().gateway_service.replay(thread_id)


@router.get("/stats", summary="网关运行态快照")
def stats() -> dict[str, Any]:
    return _ctx().gateway_service.stats()


# =========================================================================== #
# 评测闭环
# =========================================================================== #
@router.get("/scenes", summary="评测场景与用例概况")
def list_scenes() -> dict[str, Any]:
    return _ctx().gateway_service.list_scenes()


@router.post("/eval", summary="跑一轮 Golden Set 评测")
def run_eval(req: EvalRequest) -> dict[str, Any]:
    try:
        return _ctx().gateway_service.run_eval(req.scene, target=req.target)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/eval/gate", summary="评测 + 基线比对（CI 准入结论）")
def gate_eval(req: EvalRequest) -> dict[str, Any]:
    try:
        return _ctx().gateway_service.gate_eval(
            req.scene, target=req.target, max_regression=req.max_regression
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/eval/runs", summary="历史评测运行记录")
def eval_runs(scene: str = "", limit: int = 20) -> dict[str, Any]:
    store = _ctx().gateway_service.gateway.store
    runs = store.eval_runs(scene=scene or None, limit=limit)
    return {"count": len(runs), "runs": runs}


@router.post("/calibrate", summary="Judge 与人工专家双盲一致性校准")
def calibrate(req: CalibrateRequest) -> dict[str, Any]:
    try:
        return _ctx().gateway_service.calibrate(
            req.judge_scores, req.human_scores, tolerance=req.tolerance
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
