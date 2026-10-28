"""项目一的门面服务：把 RTC / ASR / TTS / RAG / 提示词 / 内容安全 / 观测装配成一个可用的语音客服。

服务对外暴露的能力（也是这个项目在业务上的完整闭环）：

    scenes()            可选场景（前端用来画选择页）
    join_info()         签发进房凭证（AppId/RoomId/UserId/Token）
    start_agent()       StartVoiceChat：把云端 AI 拉进房间
    stop_agent()        StopVoiceChat：挂断
    stream_reply()      RTC 回调进来的一轮：RAG → LLM → 内容安全 → 流式吐字
    interrupt()         客户插话：取消当前轮次的生成
    audit()             文本内容安全自查（Swagger / 业务员自助用）
    evaluate()/gate()   提示词版本评测与准入

依赖方向：
    voice_service 依赖 agent_platform（平台基座）与注入的"检索函数"，
    **不 import product_assistant** —— 知识库是"数据能力"，通过构造参数注入。
    这样语音模块可以单独部署成 RTC 回调服务，不影响其他业务。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from typing import Any

from agent_platform.config import Settings
from agent_platform.llm.base import LLMProvider
from agent_platform.logging_config import get_logger
from agent_platform.tools.registry import ToolRegistry
from agent_platform.tools.spec import ToolSpec
from voice_service.agent_config import build_start_payload, clamp_max_tokens
from voice_service.censor import Censor
from voice_service.dialog import CancelToken, DialogDeps, StreamEvent, TurnRequest, VoiceDialogEngine
from voice_service.domain import (
    EvalCase,
    EvalReport,
    GateDecision,
    RoomJoinInfo,
    RtcCallResult,
    VoiceScene,
    VoiceSession,
    VoiceTurn,
)
from voice_service.evaluation import VoiceEvaluator, builtin_cases
from voice_service.metrics import VoiceMetrics
from voice_service.prompt import PromptRegistry
from voice_service.rag import RetrieveFn, VoiceRetriever
from voice_service.rtc_client import RtcClient
from voice_service.rtc_token import RtcTokenBuilder
from voice_service.session import VoiceSessionStore

logger = get_logger(__name__)

# dry-run 用的占位凭证：没有真实凭证时也要能把 Token 签发、开房、对话全链路演一遍
_DRY_APP_ID = "dry-run-app-id"
_DRY_APP_KEY = "dry-run-app-key"


def _empty_retriever(query: str, **_kwargs: Any) -> list[Any]:
    """没有注入知识库时的空检索（语音会全部走转人工，不会胡编）。"""
    return []


# =========================================================================== #
# 内置场景（首次启动写盘；生产按业务线在 scenes.json 里配）
# =========================================================================== #
DEFAULT_SCENES: list[dict[str, Any]] = [
    {
        "scene_id": "consultant",
        "name": "助贷接待顾问",
        "description": "首轮接待：问需求、看资质、给方向、推进到下一步",
        "prompt_key": "consultant",
        "welcome_message": "您好，我是这边的顾问，您是想了解哪方面的资金需求？",
        "temperature": 0.3,
        "max_tokens": 220,
        "knowledge_limit": 2,
        "interrupt_mode": True,
        "speed_ratio": 1.0,
    },
    {
        "scene_id": "amount",
        "name": "额度与资质初判",
        "description": "客户最关心能贷多少：只给区间与影响因素，绝不承诺额度",
        "prompt_key": "amount",
        "welcome_message": "您好，额度这块我帮您先估一下，您公积金或社保缴了多久？",
        "temperature": 0.2,
        "max_tokens": 200,
        "knowledge_limit": 2,
        "interrupt_mode": True,
        "speed_ratio": 1.0,
    },
    {
        "scene_id": "docs",
        "name": "资料清单顾问",
        "description": "已决定办理的客户：一次说清三样资料，强调真实性",
        "prompt_key": "docs",
        "welcome_message": "您好，咱们先说下需要准备的资料，不复杂，就三样。",
        "temperature": 0.2,
        "max_tokens": 200,
        "knowledge_limit": 1,
        "interrupt_mode": True,
        "speed_ratio": 1.0,
    },
]


class VoiceService:
    """语音客服门面。"""

    def __init__(
        self,
        *,
        settings: Settings,
        llm: LLMProvider,
        retrieve_fn: RetrieveFn | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm

        self.prompts = PromptRegistry(settings.voice_abs_dir / "prompts.json")
        self.censor = Censor()
        self.retriever = VoiceRetriever(
            retrieve_fn or _empty_retriever,
            limit=settings.voice_knowledge_limit,
            max_chars=settings.voice_knowledge_max_chars,
            min_relevance=settings.voice_min_relevance,
        )
        self.store = VoiceSessionStore(settings.voice_sqlite_abs_path)
        self.client = RtcClient(settings)
        self.metrics = VoiceMetrics()
        self.dialog = VoiceDialogEngine(
            DialogDeps(
                llm=llm,
                retriever=self.retriever,
                prompts=self.prompts,
                censor=self.censor,
                history_turns=settings.voice_history_turns,
                max_tokens_cap=settings.voice_max_tokens,
            )
        )

        # 真实凭证缺失时退化为占位值：Token 仍能签出（结构正确），只是进不了真实房间
        self._token_builder = RtcTokenBuilder(
            settings.rtc_app_id or _DRY_APP_ID,
            settings.rtc_app_key or _DRY_APP_KEY,
        )
        self._baselines: dict[str, EvalReport] = {}
        self._cancels: dict[str, CancelToken] = {}
        self.scenes: dict[str, VoiceScene] = {}

    # ------------------------------------------------------------------ #
    # 启动
    # ------------------------------------------------------------------ #
    def bootstrap(self) -> dict[str, Any]:
        self.prompts.load()
        self._load_scenes()
        if not self.settings.voice_callback_reachable:
            logger.warning(
                "语音回调地址不可达（VOICE_PUBLIC_BASE_URL=%r）：RTC 云端无法访问本地服务，"
                "真实开房后会出现「AI 不说话」。请用 ngrok 等内网穿透，或部署到公网。",
                self.settings.voice_public_base_url,
            )
        stats = self.stats()
        logger.info("语音客服初始化完成：%s", {k: stats[k] for k in ("scenes", "prompts", "rtc")})
        return stats

    def _load_scenes(self) -> None:
        import json

        path = self.settings.voice_abs_dir / "scenes.json"
        raw: list[dict[str, Any]]
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
        else:
            raw = DEFAULT_SCENES
            path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

        scenes: dict[str, VoiceScene] = {}
        for item in raw:
            try:
                scene = VoiceScene(**item)
            except Exception:  # noqa: BLE001 - 单条配置写错不该拖垮整个服务
                logger.exception("语音场景配置非法，已跳过：%s", item.get("scene_id"))
                continue
            scenes[scene.scene_id] = scene
        # 兜底：至少要有 consultant，否则回调进来无处可去
        if "consultant" not in scenes:
            scenes["consultant"] = VoiceScene(**DEFAULT_SCENES[0])
        self.scenes = scenes

    # ------------------------------------------------------------------ #
    # 场景与进房
    # ------------------------------------------------------------------ #
    def scene_list(self) -> list[VoiceScene]:
        return list(self.scenes.values())

    def scene_of(self, scene_id: str) -> VoiceScene | None:
        return self.scenes.get(scene_id) or self.scenes.get("consultant")

    def join_info(
        self,
        *,
        scene_id: str = "consultant",
        user_id: str = "",
        room_id: str = "",
    ) -> RoomJoinInfo:
        """签发进房凭证。

        真实 Token 必须由 appId + roomId + userId 现算 —— 客户端拿到的 Token
        只能以这个身份进这个房间，泄露也进不了别的房间。
        """
        scene = self.scene_of(scene_id)
        if scene is None:
            raise KeyError(f"场景不存在：{scene_id}")
        room = room_id or f"room_{uuid.uuid4().hex[:8]}"
        user = user_id or f"user_{uuid.uuid4().hex[:6]}"
        ttl = self.settings.voice_token_ttl_seconds
        token = self._token_builder.issue(room_id=room, user_id=user, ttl_seconds=ttl)
        return RoomJoinInfo(
            scene_id=scene.scene_id,
            app_id=self.settings.rtc_app_id or _DRY_APP_ID,
            room_id=room,
            user_id=user,
            token=token,
            bot_user_id=scene.bot_user_id,
            expire_at=int(time.time()) + ttl,
            dry_run=not self.settings.voice_enabled,
        )

    # ------------------------------------------------------------------ #
    # 开房 / 挂断
    # ------------------------------------------------------------------ #
    def start_agent(
        self,
        *,
        scene_id: str = "consultant",
        room_id: str = "",
        user_id: str = "",
    ) -> dict[str, Any]:
        """把云端 AI 拉进房间，并建立一条会话记录。"""
        scene = self.scene_of(scene_id)
        if scene is None:
            raise KeyError(f"场景不存在：{scene_id}")
        room = room_id or f"room_{uuid.uuid4().hex[:8]}"
        user = user_id or f"user_{uuid.uuid4().hex[:6]}"

        session = self.store.create(
            scene_id=scene.scene_id, room_id=room, user_id=user, bot_user_id=scene.bot_user_id
        )

        prompt = self.prompts.get(scene.prompt_key)
        # 下发给云端的 system prompt 只带人设、不带知识 ——
        # 知识必须每轮由我们服务端现检索注入，否则客户问的是"昨天刚改的利率"，
        # 而云端拿的是开房那一刻的快照。
        system_prompt = prompt.render(knowledge_block="（每轮由服务端实时注入）")
        payload = build_start_payload(
            self.settings,
            room_id=room,
            user_id=user,
            bot_user_id=scene.bot_user_id,
            system_prompt=system_prompt,
            welcome_message=scene.welcome_message,
            task_id=f"task_{session.session_id}",
            max_tokens=clamp_max_tokens(scene.max_tokens or prompt.max_tokens),
            temperature=scene.temperature,
            history_turns=self.settings.voice_history_turns,
            voice_type=scene.voice_type,
            speed_ratio=scene.speed_ratio,
            hotwords=self.settings.voice_hotword_list(),
        )
        result: RtcCallResult = self.client.start_voice_chat(payload)
        if not result.ok:
            self.store.stop(session.session_id)
            session.status = "failed"

        return {
            "session": session.to_dict(),
            "rtc": result.model_dump(mode="json"),
            "join": self.join_info(scene_id=scene.scene_id, user_id=user, room_id=room).masked(),
        }

    def stop_agent(self, session_id: str) -> dict[str, Any]:
        """挂断一场会话。"""
        session = self.store.get(session_id)
        if session is None:
            raise KeyError(f"会话不存在：{session_id}")
        result = self.client.stop_voice_chat(
            app_id=self.settings.rtc_app_id, room_id=session.room_id, task_id=f"task_{session.session_id}"
        )
        self.store.stop(session_id)
        return {"session_id": session_id, "stopped": True, "rtc": result.model_dump(mode="json")}

    # ------------------------------------------------------------------ #
    # 回调：一轮对话
    # ------------------------------------------------------------------ #
    def _resolve_session(self, *, room_id: str = "", session_id: str = "") -> VoiceSession:
        """定位本轮属于哪场会话。

        优先级：session_id > 房间号 > 最近一场运行中的会话 > 新建临时会话。
        最后一层兜底是为了"回调永远不要 500" —— 云端拿不到结果会重试并最终放弃，
        客户听到的就是"AI 哑了"。
        """
        if session_id:
            found = self.store.get(session_id)
            if found:
                return found
        if room_id:
            found = self.store.find_running_by_room(room_id)
            if found:
                return found
        found = self.store.latest_running()
        if found:
            return found
        scene = self.scene_of("consultant")
        logger.warning("未找到进行中的语音会话，已新建临时会话（room=%s）", room_id or "-")
        return self.store.create(
            scene_id=scene.scene_id if scene else "consultant",
            room_id=room_id or "unknown-room",
            user_id="unknown-user",
            bot_user_id=scene.bot_user_id if scene else "",
        )

    def stream_reply(
        self,
        *,
        user_text: str,
        room_id: str = "",
        session_id: str = "",
        cancel: CancelToken | None = None,
    ) -> Iterator[StreamEvent]:
        """跑一轮并把结果落库；流式产出给 API 层转成 SSE。"""
        session = self._resolve_session(room_id=room_id, session_id=session_id)
        scene = self.scene_of(session.scene_id) or self.scene_of("consultant")
        if scene is None:
            raise KeyError("没有任何可用场景")

        # 这里的轮次号只是生成期的临时展示值；权威轮次号由 append_turn 落库时原子分配
        #（并发回调各自都会算出"下一轮"，若直接以此号落库会互相覆盖）
        round_id = self.store.next_round(session.session_id)
        req = TurnRequest(
            scene=scene,
            user_text=user_text,
            session_id=session.session_id,
            round_id=round_id,
            history=self.store.turns(session.session_id),
            cancel=cancel,
        )
        for event in self.dialog.stream_turn(req):
            if event.done and event.turn is not None:
                self.store.append_turn(session.session_id, event.turn)
                self.metrics.record(event.turn, replaced=event.replaced)
            yield event

    def interrupt(self, *, session_id: str = "", room_id: str = "") -> dict[str, Any]:
        """客户插话：让正在生成的这一轮尽快收口。

        为什么要服务端也管打断：RTC 只负责把 TTS 停掉，
        我们的生成循环还在跑（继续花 token、继续占连接）。置位后
        引擎会在下一个句子边界退出 —— 这就是"毫秒级打断"在服务端的配合动作。
        """
        key = session_id or room_id or "default"
        token = self._cancels.setdefault(key, CancelToken())
        token.cancel("用户插话")
        return {"interrupted": True, "target": key}

    def reset_cancel(self, key: str) -> None:
        """新一轮开始前清掉打断标记。"""
        self._cancels.pop(key, None)

    def cancel_token(self, key: str) -> CancelToken:
        return self._cancels.setdefault(key, CancelToken())

    # ------------------------------------------------------------------ #
    # 文本模拟一轮（Swagger 调试 / 评测 / 离线演示）
    # ------------------------------------------------------------------ #
    def ask(
        self,
        question: str,
        *,
        scene_id: str = "consultant",
        persist: bool = False,
        prompt_version: str = "",
    ) -> VoiceTurn:
        """不开房、不依赖 RTC，直接跑一轮对话（面试演示最好用）。"""
        scene = self.scene_of(scene_id)
        if scene is None:
            raise KeyError(f"场景不存在：{scene_id}")
        req = TurnRequest(
            scene=scene,
            user_text=question,
            round_id=1,
            prompt_version=prompt_version,
        )
        turn = self.dialog.ask(req)
        if persist:
            session = self.store.create(
                scene_id=scene.scene_id, room_id=f"text_{uuid.uuid4().hex[:8]}", user_id="swagger"
            )
            self.store.append_turn(session.session_id, turn)
            self.store.stop(session.session_id)
            turn = turn.model_copy(update={"round_id": 1})
        self.metrics.record(turn)
        return turn

    # ------------------------------------------------------------------ #
    # 内容安全
    # ------------------------------------------------------------------ #
    def audit(self, text: str) -> dict[str, Any]:
        verdict = self.censor.check(text)
        return {
            "passed": verdict.passed,
            "needs_human": verdict.needs_human,
            "blocked_at_runtime": verdict.blocked,
            "max_severity": verdict.max_severity if verdict.findings else "none",
            "sanitized_text": verdict.sanitized_text,
            "safe_reply": verdict.safe_reply,
            "findings": [f.model_dump(mode="json") for f in verdict.findings],
            "text": verdict.to_text(),
        }

    # ------------------------------------------------------------------ #
    # 会话查询
    # ------------------------------------------------------------------ #
    def sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.store.list_sessions(limit=limit)]

    def session_detail(self, session_id: str) -> dict[str, Any] | None:
        session = self.store.get(session_id)
        if session is None:
            return None
        data = session.to_dict()
        data["transcript"] = session.to_transcript()
        return data

    def report(self, session_id: str) -> dict[str, Any] | None:
        """单场会话的复盘（业务方最关心：聊了几轮、拒答几次、转人工几次、快不快）。"""
        session = self.store.get(session_id)
        if session is None:
            return None
        latencies = [t.latency for t in session.turns]
        return {
            "session_id": session.session_id,
            "scene_id": session.scene_id,
            "turns": len(session.turns),
            "refused": session.refused_count,
            "handoff": session.handoff_count,
            "avg_ttft_ms": round(sum(x.llm_ttft_ms for x in latencies) / len(latencies), 1)
            if latencies
            else 0.0,
            "avg_total_ms": round(sum(x.total_ms for x in latencies) / len(latencies), 1) if latencies else 0.0,
            "citations": sorted({c for t in session.turns for c in t.citations}),
            "transcript": session.to_transcript(),
        }

    # ------------------------------------------------------------------ #
    # 提示词评测与准入
    # ------------------------------------------------------------------ #
    def evaluate(
        self,
        *,
        prompt_key: str = "consultant",
        version: str = "",
        cases: list[EvalCase] | None = None,
    ) -> EvalReport:
        """跑一遍 Golden Set。

        刻意**不落库会话**：评测是"试跑"，不应该污染真实会话数据。
        """
        prompt = self.prompts.get(prompt_key, version)
        scene = next((s for s in self.scenes.values() if s.prompt_key == prompt_key), None)
        if scene is None:
            scene = self.scene_of("consultant")
        if scene is None:
            raise KeyError("没有任何可用场景")

        def _ask(case: EvalCase) -> VoiceTurn:
            target_scene = self.scene_of(case.scene_id) or scene
            return self.dialog.ask(
                TurnRequest(
                    scene=target_scene,
                    user_text=case.question,
                    round_id=1,
                    prompt_version=prompt.version,
                )
            )

        report = VoiceEvaluator(_ask).evaluate(
            cases or builtin_cases(), prompt_key=prompt_key, version=prompt.version
        )
        return report

    def gate_prompt(
        self,
        *,
        prompt_key: str = "consultant",
        version: str = "",
        max_regression: float | None = None,
    ) -> dict[str, Any]:
        """提示词版本准入：跑评测 → 与基线比 → 决定是否允许上线。

        注意顺序：**先取基线再看本次**。反过来写（先跑再取基线）等于拿自己比自己，
        gate 永远通过 —— 项目六踩过这个坑。
        """
        baseline = self._baselines.get(prompt_key)
        current = self.evaluate(prompt_key=prompt_key, version=version)
        decision: GateDecision = VoiceEvaluator(lambda c: self.ask(c.question)).compare(
            current,
            baseline,
            max_regression=(
                self.settings.voice_eval_max_regression if max_regression is None else max_regression
            ),
        )
        if decision.allowed:
            self._baselines[prompt_key] = current
        return {
            "current": {"version": current.version, "score": current.score, "passed": current.passed},
            "baseline": {"version": baseline.version, "score": baseline.score} if baseline else None,
            "gate": decision.model_dump(mode="json"),
            "report": current.to_text(),
        }

    def prompt_catalog(self) -> dict[str, Any]:
        return {
            "keys": self.prompts.keys(),
            "versions": {
                key: [
                    {
                        "version": v.version,
                        "status": v.status,
                        "note": v.note,
                        "max_tokens": v.max_tokens,
                        "temperature": v.temperature,
                    }
                    for v in self.prompts.versions(key)
                ]
                for key in self.prompts.keys()
            },
            "active": {k: self.prompts.get(k).version for k in self.prompts.keys()},
        }

    def add_prompt(
        self,
        key: str,
        system_prompt: str,
        *,
        note: str = "",
        temperature: float = 0.3,
        max_tokens: int = 220,
        activate: bool = False,
    ) -> dict[str, Any]:
        prompt = self.prompts.add(
            key,
            system_prompt,
            note=note,
            temperature=temperature,
            max_tokens=max_tokens,
            activate=activate,
        )
        return prompt.model_dump(mode="json")

    def activate_prompt(self, key: str, version: str) -> dict[str, Any]:
        return self.prompts.activate(key, version).model_dump(mode="json")

    # ------------------------------------------------------------------ #
    # 工具注册（供平台 Agent 调用）
    # ------------------------------------------------------------------ #
    def register_tools(self, registry: ToolRegistry) -> list[str]:
        registered: list[str] = []

        def _rooms(scene_id: str = "consultant", user_id: str = "") -> dict[str, Any]:
            return self.start_agent(scene_id=scene_id, user_id=user_id)

        registry.register(
            ToolSpec(
                name="voice.start_session",
                description=(
                    "开一场语音接待：签发 RTC 进房凭证，并把云端 AI 拉进房间。"
                    "返回 session_id / 房间号 / Token（已打码）/ 下发配置。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "scene_id": {"type": "string", "description": "consultant | amount | docs"},
                        "user_id": {"type": "string", "description": "客户标识，留空自动生成"},
                    },
                    "required": [],
                },
                handler=_rooms,
                tags=("voice", "room", "write"),
                # 开房会产生真实费用，必须人工确认
                risk_level="high",
                idempotent=False,
            )
        )
        registered.append("voice.start_session")

        def _stop(session_id: str) -> dict[str, Any]:
            return self.stop_agent(session_id)

        registry.register(
            ToolSpec(
                name="voice.stop_session",
                description="挂断语音会话（StopVoiceChat）。客户挂机后必须调用，否则同一房间无法再开新 Agent。",
                parameters={
                    "type": "object",
                    "properties": {"session_id": {"type": "string", "description": "会话 ID"}},
                    "required": ["session_id"],
                },
                handler=_stop,
                tags=("voice", "room", "write"),
                risk_level="medium",
            )
        )
        registered.append("voice.stop_session")

        def _ask(question: str, scene_id: str = "consultant") -> dict[str, Any]:
            turn = self.ask(question, scene_id=scene_id, persist=True)
            return {
                "answer": turn.answer_text,
                "refused": turn.refused,
                "needs_human": turn.needs_human,
                "citations": turn.citations,
                "latency_ms": round(turn.latency.total_ms, 1),
                "ttft_ms": round(turn.latency.llm_ttft_ms, 1),
            }

        registry.register(
            ToolSpec(
                name="voice.ask",
                description=(
                    "用文本模拟一轮语音对话（不开房）：走真实的 RAG + 提示词 + 内容安全链路，"
                    "返回回答、是否拒答、引用与延迟。调试与演示首选。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "客户说的话"},
                        "scene_id": {"type": "string", "description": "consultant | amount | docs"},
                    },
                    "required": ["question"],
                },
                handler=_ask,
                tags=("voice", "dialog"),
            )
        )
        registered.append("voice.ask")

        def _audit(text: str) -> dict[str, Any]:
            return self.audit(text)

        registry.register(
            ToolSpec(
                name="voice.audit",
                description=(
                    "语音出口内容安全自查：命中承诺/造假/违规收费/以贷养贷/隐私等红线时返回兜底话术。"
                ),
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string", "description": "准备播报的文本"}},
                    "required": ["text"],
                },
                handler=_audit,
                tags=("voice", "audit"),
            )
        )
        registered.append("voice.audit")

        def _report(session_id: str) -> dict[str, Any]:
            data = self.report(session_id)
            return data or {"error": f"会话不存在：{session_id}"}

        registry.register(
            ToolSpec(
                name="voice.session_report",
                description="单场语音会话复盘：轮数、拒答、转人工、平均首字延迟、引用条款。",
                parameters={
                    "type": "object",
                    "properties": {"session_id": {"type": "string"}},
                    "required": ["session_id"],
                },
                handler=_report,
                tags=("voice", "observe"),
            )
        )
        registered.append("voice.session_report")

        def _metrics() -> dict[str, Any]:
            return self.metrics.snapshot()

        registry.register(
            ToolSpec(
                name="voice.metrics",
                description="语音链路指标：首字延迟 p50/p95、拒答率、转人工率、内容安全替换次数。",
                parameters={"type": "object", "properties": {}, "required": []},
                handler=_metrics,
                tags=("voice", "observe"),
            )
        )
        registered.append("voice.metrics")

        def _eval(prompt_key: str = "consultant", version: str = "") -> dict[str, Any]:
            report = self.evaluate(prompt_key=prompt_key, version=version)
            return {
                "prompt_key": report.prompt_key,
                "version": report.version,
                "total": report.total,
                "passed": report.passed,
                "score": report.score,
                "failed_cases": [c.case_id for c in report.cases if not c.passed],
                "text": report.to_text(),
            }

        registry.register(
            ToolSpec(
                name="voice.eval_prompt",
                description="跑语音 Golden Set 评测：合规红线一票否决 + 拒答正确性 + 事实覆盖 + 语速长度。",
                parameters={
                    "type": "object",
                    "properties": {
                        "prompt_key": {"type": "string", "description": "默认 consultant"},
                        "version": {"type": "string", "description": "留空取 active 版本"},
                    },
                    "required": [],
                },
                handler=_eval,
                tags=("voice", "eval"),
            )
        )
        registered.append("voice.eval_prompt")

        logger.info("语音客服注册工具 %d 个：%s", len(registered), registered)
        return registered

    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, Any]:
        return {
            "scenes": len(self.scenes),
            "prompts": self.prompts.stats(),
            "rtc": {
                "enabled": self.settings.voice_enabled,
                "dry_run": self.client.dry_run,
                "app_id": self.settings.rtc_app_id or "（未配置）",
                "callback_url": self.settings.voice_callback_url or "（未配置）",
                "callback_reachable": self.settings.voice_callback_reachable,
                "llm_model": self.settings.voice_llm_model,
            },
            "dialog": {
                "max_tokens": self.settings.voice_max_tokens,
                "history_turns": self.settings.voice_history_turns,
                "knowledge_limit": self.settings.voice_knowledge_limit,
            },
            "store": self.store.stats(),
            "metrics": self.metrics.snapshot(),
        }

    def close(self) -> None:
        """释放资源（进程退出 / 测试析构时调用）。"""
        self.store.close()
