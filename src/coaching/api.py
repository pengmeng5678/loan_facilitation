"""项目三（话术陪练与 AI 考核）的 HTTP 接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/coaching", tags=["coaching"])


def _ctx():
    from app_context import get_app_context

    return get_app_context()


# =========================================================================== #
# 请求模型
# =========================================================================== #
class StartSessionRequest(BaseModel):
    persona_id: str = Field(..., description="客户画像编号，如 P-PRICE")
    trainee: str = Field("trainee", description="学员姓名/工号")


class ReplyRequest(BaseModel):
    text: str = Field(..., description="学员的话术")


class QCRequest(BaseModel):
    conversation_id: str = Field("", description="对话编号；留空表示质检全部")


class CalibrateRequest(BaseModel):
    judge_scores: list[float] = Field(..., description="Judge 打分（百分制）")
    human_scores: list[float] = Field(..., description="人工专家双盲打分（百分制）")
    tolerance: float = Field(10.0, description="判定一致的容差（分）")


# =========================================================================== #
# 陪练
# =========================================================================== #
@router.get("/personas", summary="客户画像列表")
def list_personas() -> dict[str, Any]:
    ctx = _ctx()
    personas = [p.model_dump(mode="json") for p in ctx.coaching.personas.values()]
    return {"count": len(personas), "personas": personas}


@router.post("/sessions", summary="开启陪练会话")
def start_session(req: StartSessionRequest) -> dict[str, Any]:
    ctx = _ctx()
    try:
        session = ctx.coaching.start_session(req.persona_id, req.trainee)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "session_id": session.session_id,
        "persona": session.persona.model_dump(mode="json"),
        "opening": session.messages[-1].text if session.messages else "",
    }


@router.post("/sessions/{session_id}/reply", summary="学员发言，模拟客户回应")
def reply(session_id: str, req: ReplyRequest) -> dict[str, Any]:
    ctx = _ctx()
    try:
        return ctx.coaching.reply(session_id, req.text)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/finish", summary="结束陪练并出评分卡")
def finish(session_id: str) -> dict[str, Any]:
    ctx = _ctx()
    try:
        card = ctx.coaching.finish_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    payload = card.model_dump(mode="json")
    payload["report_text"] = card.to_text()
    return payload


@router.get("/sessions/{session_id}/replay", summary="复盘回放")
def replay(session_id: str) -> dict[str, Any]:
    ctx = _ctx()
    messages = ctx.coaching.replay(session_id)
    if not messages:
        raise HTTPException(status_code=404, detail=f"会话不存在或无消息：{session_id}")
    return {"session_id": session_id, "messages": messages}


@router.get("/sessions", summary="会话列表")
def list_sessions(limit: int = 50) -> dict[str, Any]:
    ctx = _ctx()
    sessions = ctx.coaching.list_sessions(limit)
    return {"count": len(sessions), "sessions": sessions}


# =========================================================================== #
# 质检 / 挖掘 / 校准
# =========================================================================== #
@router.post("/qc", summary="全量会话质检")
def qc(req: QCRequest) -> dict[str, Any]:
    ctx = _ctx()
    try:
        if req.conversation_id:
            report = ctx.coaching.qc(req.conversation_id)
            payload = report.model_dump(mode="json")
            payload["report_text"] = report.to_text()
            return payload
        reports = ctx.coaching.qc_all()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {
        "count": len(reports),
        "needs_alert": sum(1 for r in reports if r.needs_alert),
        "average_score": round(sum(r.score for r in reports) / len(reports), 1) if reports else 0,
        "reports": [
            {**r.model_dump(mode="json"), "report_text": r.to_text()} for r in reports
        ],
    }


@router.post("/qc/alert", summary="对高风险对话告警主管（高风险动作，需人工审批）")
def qc_alert(conversation_id: str, supervisor: str = "supervisor_duty") -> dict[str, Any]:
    """走平台 HITL：调用高风险工具发送告警，会返回 interrupt 等待审批。"""
    ctx = _ctx()
    result = ctx.runtime.invoke(
        "coaching",
        f"请对对话 {conversation_id} 做合规质检并告警主管",
        context={"conversation_id": conversation_id, "supervisor": supervisor},
    )
    return result.model_dump(mode="json")


@router.post("/mining", summary="高转化话术挖掘")
def mining() -> dict[str, Any]:
    ctx = _ctx()
    return ctx.coaching.mine().model_dump(mode="json")


@router.post("/calibrate", summary="Judge 与人工专家双盲一致性校准")
def calibrate(req: CalibrateRequest) -> dict[str, Any]:
    ctx = _ctx()
    try:
        return ctx.coaching.calibrate(req.judge_scores, req.human_scores)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/stats", summary="陪练服务统计")
def stats() -> dict[str, Any]:
    return _ctx().coaching.stats()
