"""项目一（AI 实时语音对话智能客服）的 HTTP 接口。

对外有三类使用者，接口也按这个分：

    ① **前端（React + @volcengine/rtc）**
       GET  /voice/scenes                     拉场景列表
       POST /voice/rooms                      拿进房凭证（AppId/RoomId/UserId/Token）
       POST /voice/rooms/start                让服务端把云端 AI 拉进房间
       POST /voice/rooms/stop                 挂断
       POST /voice/rooms/interrupt            客户插话（服务端同步收口）

    ② **RTC 云端（第三方 LLM 回调）**
       POST /voice/callback/chat              OpenAI 协议；stream=true 时返回 SSE

    ③ **内部调试 / 运营**
       POST /voice/ask                        文本模拟一轮（不花 RTC 的钱）
       POST /voice/audit                      话术内容安全自查
       GET  /voice/sessions[/{id}]            会话与逐轮明细
       GET  /voice/metrics                    首字延迟 / 拒答率 / 转人工率
       GET  /voice/prompts                    提示词版本
       POST /voice/eval、/voice/eval/gate     评测与版本准入

关于 `/voice/callback/chat` 的两个硬约束：
    * 必须**流式**（stream=true）—— 否则 TTS 要等整段生成完才开口，首字延迟 3-5s；
    * 必须**校验回调鉴权**（VOICE_CALLBACK_API_KEY）—— 这个地址在公网上，
      不校验就等于给全世界开了个免费的大模型代理。
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from agent_platform.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])


def _ctx():
    from app_context import get_app_context

    return get_app_context()


# =========================================================================== #
# 请求模型
# =========================================================================== #
class JoinRequest(BaseModel):
    scene_id: str = Field("consultant", description="consultant | amount | docs")
    user_id: str = Field("", description="客户标识；留空自动生成")
    room_id: str = Field("", description="房间号；留空自动生成")


class StartRequest(BaseModel):
    scene_id: str = Field("consultant")
    room_id: str = Field("", description="与前端进房用的房间号一致")
    user_id: str = Field("", description="与前端进房用的 userId 一致（否则云端找不到人）")


class StopRequest(BaseModel):
    session_id: str = Field(..., description="会话 ID（vs_ 开头）")


class InterruptRequest(BaseModel):
    session_id: str = Field("")
    room_id: str = Field("")


class AskRequest(BaseModel):
    question: str = Field(..., description="客户说的话")
    scene_id: str = Field("consultant")
    persist: bool = Field(False, description="是否落库（调试默认否，避免污染会话数据）")


class AuditRequest(BaseModel):
    text: str = Field(..., description="准备播报的文本")


class PromptRequest(BaseModel):
    key: str = Field("consultant")
    system_prompt: str = Field(..., description="新的提示词全文（需包含 {{knowledge}} 占位）")
    note: str = Field("")
    temperature: float = Field(0.3, ge=0, le=2)
    max_tokens: int = Field(220, ge=150, le=300)
    activate: bool = Field(False, description="是否立即生效（建议先跑评测再切）")


class ActivateRequest(BaseModel):
    version: str = Field(..., description="要切换到的版本号，如 v2")


class EvalRequest(BaseModel):
    prompt_key: str = Field("consultant")
    version: str = Field("", description="留空取 active 版本")


class GateRequest(EvalRequest):
    max_regression: float | None = Field(None, description="允许的退化幅度，默认取配置")


# =========================================================================== #
# ① 前端：场景与房间
# =========================================================================== #
@router.get("/scenes", summary="场景列表（前端渲染用，不含任何密钥）")
def list_scenes() -> dict[str, Any]:
    service = _ctx().voice
    return {"scenes": [s.public_dict()["scene"] for s in service.scene_list()]}


@router.post("/rooms", summary="签发进房凭证（AppId / RoomId / UserId / Token）")
def create_room(req: JoinRequest) -> dict[str, Any]:
    service = _ctx().voice
    try:
        info = service.join_info(scene_id=req.scene_id, user_id=req.user_id, room_id=req.room_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # 这里必须返回**完整 Token**（客户端要拿它进房），所以不打码；
    # 但 dry_run=True 时 Token 是占位凭证，进不了真实房间。
    return {"rtc": info.public_dict(), "scene_id": info.scene_id, "dry_run": info.dry_run}


@router.post("/rooms/start", summary="拉起云端 AI（StartVoiceChat）")
def start_room(req: StartRequest) -> dict[str, Any]:
    service = _ctx().voice
    try:
        return service.start_agent(scene_id=req.scene_id, room_id=req.room_id, user_id=req.user_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/rooms/stop", summary="挂断（StopVoiceChat）")
def stop_room(req: StopRequest) -> dict[str, Any]:
    service = _ctx().voice
    try:
        return service.stop_agent(req.session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/rooms/interrupt", summary="客户插话：服务端同步收口当前生成")
def interrupt(req: InterruptRequest) -> dict[str, Any]:
    return _ctx().voice.interrupt(session_id=req.session_id, room_id=req.room_id)


# =========================================================================== #
# ② RTC 云端回调（OpenAI 兼容）
# =========================================================================== #
def _check_callback_auth(authorization: str) -> None:
    """校验回调鉴权。

    未配置 VOICE_CALLBACK_API_KEY 时放行（本地/演练），但会打警告 ——
    生产必须配，否则这个公网地址就是一个免费的大模型代理。
    """
    service = _ctx().voice
    expected = service.settings.voice_callback_api_key
    if not expected:
        return
    token = (authorization or "").removeprefix("Bearer ").strip()
    if token != expected:
        logger.warning("语音回调鉴权失败：token 不匹配")
        raise HTTPException(status_code=401, detail="invalid callback api key")


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages or []):
        if str(msg.get("role")) == "user":
            content = msg.get("content")
            if isinstance(content, list):
                # 多模态消息（如 [{"type":"text","text":...}]）取文本部分
                return " ".join(
                    str(part.get("text", "")) for part in content if isinstance(part, dict)
                ).strip()
            return str(content or "").strip()
    return ""


def _openai_chunk(model: str, *, delta: str = "", finish: str | None = None) -> str:
    payload = {
        "id": "chatcmpl-voice",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": delta} if delta else {},
                "finish_reason": finish,
            }
        ],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/callback/chat", summary="RTC 云端第三方 LLM 回调（OpenAI 协议，支持 SSE）")
async def callback_chat(
    request: Request,
    authorization: str = Header("", alias="Authorization"),
    x_room_id: str = Header("", alias="X-Room-Id"),
) -> Any:
    """RTC 云端把 ASR 文本打过来，我们返回回答。

    请求体是标准 OpenAI Chat Completions 结构（云端只认这个），
    房间信息可能出现在 header（X-Room-Id）、body.room_id 或 body.user 里，
    三者都取不到时退化为"最近一场进行中的会话"。
    """
    _check_callback_auth(authorization)
    service = _ctx().voice
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"请求体不是合法 JSON：{exc}") from exc

    question = _last_user_text(body.get("messages") or [])
    if not question:
        raise HTTPException(status_code=400, detail="messages 里没有 user 内容")

    room_id = x_room_id or str(body.get("room_id") or "")
    session_id = str(body.get("session_id") or "")
    model = str(body.get("model") or service.settings.voice_llm_model)
    stream = bool(body.get("stream", True))

    # 新一轮开始：清掉上一轮可能残留的打断标记
    cancel_key = session_id or room_id or "default"
    service.reset_cancel(cancel_key)
    cancel = service.cancel_token(cancel_key)

    if not stream:
        turn = await run_in_threadpool(
            lambda: _run_sync_turn(service, question, room_id, session_id, cancel)
        )
        return JSONResponse(
            {
                "id": "chatcmpl-voice",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": turn.answer_text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        )

    async def event_stream() -> AsyncIterator[str]:
        iterator = service.stream_reply(
            user_text=question, room_id=room_id, session_id=session_id, cancel=cancel
        )
        started = time.perf_counter()
        first = True
        try:
            while True:
                # 客户端断开（客户挂机 / 打断）→ 置位取消，生成循环在下一个句界退出
                if await request.is_disconnected():
                    cancel.cancel("客户端断开")
                    logger.info("语音回调客户端断开，已取消本轮生成")
                    break
                event = await run_in_threadpool(next, iterator, None)
                if event is None:
                    break
                if event.delta:
                    if first:
                        logger.info(
                            "语音回调首片 %.0fms（room=%s）", (time.perf_counter() - started) * 1000, room_id
                        )
                        first = False
                    yield _openai_chunk(model, delta=event.delta)
            yield _openai_chunk(model, finish="stop")
            yield "data: [DONE]\n\n"
        finally:
            # 确保生成器被关闭（否则线程池里的生成循环会继续跑）
            close = getattr(iterator, "close", None)
            if callable(close):
                close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def _run_sync_turn(service, question: str, room_id: str, session_id: str, cancel) -> Any:
    """非流式回调：把整轮跑完再一次性返回。"""
    turn = None
    for event in service.stream_reply(
        user_text=question, room_id=room_id, session_id=session_id, cancel=cancel
    ):
        if event.done:
            turn = event.turn
    if turn is None:
        raise HTTPException(status_code=500, detail="对话引擎未产出结果")
    return turn


# =========================================================================== #
# ③ 调试 / 运营
# =========================================================================== #
@router.post("/ask", summary="文本模拟一轮对话（不开房，走真实链路）")
def ask(req: AskRequest) -> dict[str, Any]:
    service = _ctx().voice
    try:
        turn = service.ask(req.question, scene_id=req.scene_id, persist=req.persist)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return turn.to_dict()


@router.post("/audit", summary="话术内容安全自查")
def audit(req: AuditRequest) -> dict[str, Any]:
    return _ctx().voice.audit(req.text)


@router.get("/sessions", summary="会话列表")
def list_sessions(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    service = _ctx().voice
    items = service.sessions(limit=limit)
    return {"count": len(items), "sessions": items}


@router.get("/sessions/{session_id}", summary="会话明细（逐轮 + 文字稿）")
def session_detail(session_id: str) -> dict[str, Any]:
    data = _ctx().voice.session_detail(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{session_id}")
    return data


@router.get("/sessions/{session_id}/report", summary="会话复盘（轮数/拒答/转人工/延迟）")
def session_report(session_id: str) -> dict[str, Any]:
    data = _ctx().voice.report(session_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{session_id}")
    return data


@router.get("/metrics", summary="语音链路指标")
def metrics() -> dict[str, Any]:
    return _ctx().voice.metrics.snapshot()


@router.get("/stats", summary="语音服务运行态（RTC 模式 / 回调可达性 / 会话量）")
def stats() -> dict[str, Any]:
    return _ctx().voice.stats()


# =========================================================================== #
# 提示词与评测
# =========================================================================== #
@router.get("/prompts", summary="提示词版本目录")
def prompts() -> dict[str, Any]:
    return _ctx().voice.prompt_catalog()


@router.post("/prompts", summary="新增提示词版本（默认不生效）")
def add_prompt(req: PromptRequest) -> dict[str, Any]:
    if "{{knowledge}}" not in req.system_prompt:
        # 缺占位符会让 RAG 静默失效（模型永远看不到知识），必须在入口拦住
        raise HTTPException(status_code=400, detail="system_prompt 必须包含 {{knowledge}} 占位符")
    service = _ctx().voice
    return service.add_prompt(
        req.key,
        req.system_prompt,
        note=req.note or f"by api {uuid.uuid4().hex[:6]}",
        temperature=req.temperature,
        max_tokens=req.max_tokens,
        activate=req.activate,
    )


@router.post("/prompts/{key}/activate", summary="切换提示词版本")
def activate_prompt(key: str, req: ActivateRequest) -> dict[str, Any]:
    service = _ctx().voice
    try:
        return service.activate_prompt(key, req.version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/eval", summary="跑语音 Golden Set 评测")
def evaluate(req: EvalRequest) -> dict[str, Any]:
    report = _ctx().voice.evaluate(prompt_key=req.prompt_key, version=req.version)
    return {
        "prompt_key": report.prompt_key,
        "version": report.version,
        "total": report.total,
        "passed": report.passed,
        "score": report.score,
        "cases": [c.model_dump(mode="json") for c in report.cases],
        "text": report.to_text(),
    }


@router.post("/eval/gate", summary="提示词版本准入（跑评测 + 与基线比）")
def gate(req: GateRequest) -> dict[str, Any]:
    return _ctx().voice.gate_prompt(
        prompt_key=req.prompt_key, version=req.version, max_regression=req.max_regression
    )
