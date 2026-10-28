"""话术陪练服务（项目三的门面）。

四个能力：
    start_session / reply / finish_session   陪练（角色扮演 + 评分）
    qc                                      全量质检
    mine                                    话术挖掘回流

并通过 register_tools() 装配到平台；高风险的"违规告警主管"动作走平台 HITL 审批。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from agent_platform.config import Settings, get_settings
from agent_platform.llm.base import LLMProvider
from agent_platform.llm.providers import build_llm
from agent_platform.logging_config import get_logger
from agent_platform.tools.registry import ToolRegistry
from agent_platform.tools.spec import ToolSpec
from coaching.domain import (
    MiningResult,
    Persona,
    QCReport,
    RoleplaySession,
    ScoreCard,
    SessionMessage,
)
from coaching.judge import Judge
from coaching.mining import PhraseMiner
from coaching.qc import QCEngine
from coaching.roleplay import CustomerSimulator
from coaching.store import SessionStore

logger = get_logger(__name__)


class CoachingService:
    """陪练与考核服务。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        llm: LLMProvider | None = None,
        profile_provider: Callable[[str], Any] | None = None,
        response_time_limit: float = 1800.0,
    ) -> None:
        self.settings = settings or get_settings()
        self.llm = llm or build_llm(self.settings)
        # profile_provider: 由外部注入的"客户档案查询函数"（解耦：本项目不直接依赖项目二）
        self.profile_provider = profile_provider

        self.store = SessionStore(self.settings.sqlite_abs_path.parent / "coaching.sqlite")
        self.simulator = CustomerSimulator(llm=self.llm)
        self.judge = Judge(llm=self.llm)
        self.qc_engine = QCEngine(llm=self.llm, response_time_limit=response_time_limit)
        self.miner = PhraseMiner()

        self.personas: dict[str, Persona] = {}
        self.conversations: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #
    def bootstrap(self) -> dict[str, Any]:
        """加载客户画像与历史对话语料。"""
        self._load_personas()
        self._load_conversations()
        stats = {
            "personas": len(self.personas),
            "conversations": len(self.conversations),
            "sessions": len(self.store.list_sessions()),
        }
        logger.info("陪练服务初始化完成：%s", stats)
        return stats

    def _load_personas(self) -> None:
        path = self.settings.profile_abs_dir / "personas.json"
        if not path.exists():
            logger.warning("未找到客户画像文件：%s（请先运行 scripts/seed_data.py）", path)
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        for item in raw:
            persona = Persona(**item)
            self.personas[persona.persona_id] = persona
        logger.info("加载客户画像：%d 个", len(self.personas))

    def _load_conversations(self) -> None:
        path = self.settings.policy_abs_dir / "conversations.jsonl"
        if not path.exists():
            logger.warning("未找到历史对话文件：%s", path)
            return
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        self.conversations = rows
        logger.info("加载历史对话：%d 段", len(rows))

    # ------------------------------------------------------------------ #
    # 陪练
    # ------------------------------------------------------------------ #
    def start_session(self, persona_id: str, trainee: str = "unknown") -> RoleplaySession:
        """开始一场陪练。"""
        persona = self.personas.get(persona_id)
        if persona is None:
            raise KeyError(f"客户画像不存在：{persona_id}。可选：{sorted(self.personas)}")

        session_id = f"cs_{uuid.uuid4().hex[:10]}"
        session = self.store.create(session_id, persona, trainee)

        # 记录开场（客户先说话）
        opening = self.simulator.opening(session)
        msg = SessionMessage(role="customer", text=opening, ts=0.0)
        self.store.append_message(session_id, msg, turn=0)
        session.messages.append(msg)
        logger.info("陪练开始 session=%s persona=%s trainee=%s", session_id, persona_id, trainee)
        return session

    def reply(self, session_id: str, business_text: str) -> dict[str, Any]:
        """业务员说一句，模拟客户回一句。"""
        session = self.store.get(session_id)
        if session is None:
            raise KeyError(f"陪练会话不存在：{session_id}")
        if session.status != "active":
            raise ValueError(f"会话已结束，无法继续对话：{session_id}")

        # 记录业务员发言
        business_msg = SessionMessage(role="business", text=business_text, ts=float(session.turn * 30))
        self.store.append_message(session_id, business_msg, turn=session.turn + 1)
        session.messages.append(business_msg)
        session.turn += 1

        # 生成客户回复
        facts = self._facts_for(session.persona)
        customer_reply = self.simulator.reply(session, business_text, facts=facts)
        customer_msg = SessionMessage(
            role="customer", text=customer_reply, ts=float(session.turn * 30 + 25)
        )
        self.store.append_message(session_id, customer_msg, turn=session.turn + 1)
        session.messages.append(customer_msg)
        session.turn += 1

        return {
            "session_id": session_id,
            "turn": session.turn,
            "customer_reply": customer_reply,
            "resistant": CustomerSimulator.is_resistant(customer_reply),
            "persona_id": session.persona.persona_id,
        }

    def finish_session(self, session_id: str) -> ScoreCard:
        """结束陪练并出评分卡。"""
        session = self.store.get(session_id)
        if session is None:
            raise KeyError(f"陪练会话不存在：{session_id}")

        card = self.judge.score_session(session)
        self.store.save_scorecard(card)
        self.store.finish(session_id)
        return card

    def replay(self, session_id: str) -> list[dict]:
        """复盘回放。"""
        return self.store.replay(session_id)

    def list_sessions(self, limit: int = 50) -> list[dict]:
        return self.store.list_sessions(limit)

    # ------------------------------------------------------------------ #
    # 质检
    # ------------------------------------------------------------------ #
    def qc(self, conversation_id: str = "", conversation: dict | None = None) -> QCReport:
        """质检一段对话。

        传 conversation_id 则从语料库取；也可直接传 conversation（对接企微会话存档时用）。
        """
        if conversation is None:
            conversation = next(
                (c for c in self.conversations if c.get("conversation_id") == conversation_id),
                None,
            )
        if conversation is None:
            raise KeyError(
                f"未找到对话：{conversation_id}。可用：{[c.get('conversation_id') for c in self.conversations]}"
            )
        return self.qc_engine.check(conversation)

    def qc_all(self) -> list[QCReport]:
        """全量质检（对应"覆盖率 100%"）：批量跑完整个语料库。"""
        return [self.qc_engine.check(c) for c in self.conversations]

    # ------------------------------------------------------------------ #
    # 话术挖掘
    # ------------------------------------------------------------------ #
    def mine(self) -> MiningResult:
        return self.miner.mine(self.conversations)

    # ------------------------------------------------------------------ #
    # 校准
    # ------------------------------------------------------------------ #
    @staticmethod
    def calibrate(judge_scores: list[float], human_scores: list[float]) -> dict:
        """双盲一致性校准（Judge vs 人工专家）。"""
        return Judge.calibrate(judge_scores, human_scores)

    # ------------------------------------------------------------------ #
    # 工具注册
    # ------------------------------------------------------------------ #
    def register_tools(self, registry: ToolRegistry) -> list[str]:
        """把陪练/质检/挖掘能力注册成平台工具。"""
        registered: list[str] = []

        def _start(persona_id: str, trainee: str = "trainee") -> dict[str, Any]:
            session = self.start_session(persona_id, trainee)
            return {
                "session_id": session.session_id,
                "persona": session.persona.model_dump(mode="json"),
                "opening": session.messages[-1].text if session.messages else "",
            }

        registry.register(
            ToolSpec(
                name="coaching.start_session",
                description=(
                    "开启一场话术陪练：按客户画像生成虚拟客户并给出开场白。"
                    "返回 session_id 供后续 coaching.reply 使用。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "persona_id": {"type": "string", "description": "客户画像编号，如 P-PRICE"},
                        "trainee": {"type": "string", "description": "学员姓名/工号"},
                    },
                    "required": ["persona_id"],
                },
                handler=_start,
                tags=("practice", "write"),
            )
        )
        registered.append("coaching.start_session")

        def _reply(session_id: str, text: str) -> dict[str, Any]:
            return self.reply(session_id, text)

        registry.register(
            ToolSpec(
                name="coaching.reply",
                description="学员说一句，模拟客户按画像回复一句（会抵抗、会比价、会质疑，不放水）。",
                parameters={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "text": {"type": "string", "description": "学员的话术"},
                    },
                    "required": ["session_id", "text"],
                },
                handler=_reply,
                tags=("practice", "write"),
            )
        )
        registered.append("coaching.reply")

        def _finish(session_id: str) -> dict[str, Any]:
            card = self.finish_session(session_id)
            return {**card.model_dump(mode="json"), "report_text": card.to_text()}

        registry.register(
            ToolSpec(
                name="coaching.finish_session",
                description=(
                    "结束陪练并生成四维评分卡（合规红线/需求挖掘/异议处理/邀约促成），"
                    "含违规记录、薄弱点与改进建议。"
                ),
                parameters={
                    "type": "object",
                    "properties": {"session_id": {"type": "string"}},
                    "required": ["session_id"],
                },
                handler=_finish,
                tags=("practice", "read"),
            )
        )
        registered.append("coaching.finish_session")

        def _qc(conversation_id: str = "") -> dict[str, Any]:
            report = self.qc(conversation_id) if conversation_id else self.qc_all()[0]
            return {**report.model_dump(mode="json"), "report_text": report.to_text()}

        registry.register(
            ToolSpec(
                name="coaching.qc",
                description=(
                    "对业务对话做全量质检：检查违规承诺、敏感信息泄露、SOP 缺失、响应时效。"
                    "返回问题等级与证据原文。存在 high 级问题时需要告警主管。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "conversation_id": {"type": "string", "description": "对话编号，留空则质检全部"},
                    },
                    "required": [],
                },
                handler=_qc,
                tags=("qc", "read"),
            )
        )
        registered.append("coaching.qc")

        def _mine() -> dict[str, Any]:
            result = self.mine()
            return result.model_dump(mode="json")

        registry.register(
            ToolSpec(
                name="coaching.mine_phrases",
                description="从已成交对话中挖掘高转化话术，回流知识库与陪练题库。",
                parameters={"type": "object", "properties": {}, "required": []},
                handler=_mine,
                tags=("mining", "read"),
            )
        )
        registered.append("coaching.mine_phrases")

        logger.info("陪练服务注册工具 %d 个：%s", len(registered), registered)
        return registered

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _facts_for(self, persona: Persona) -> dict[str, Any]:
        """取该画像关联的真实客户事实（用于让模拟客户答得'像真的'）。"""
        if not persona.customer_id or self.profile_provider is None:
            return {}
        try:
            profile = self.profile_provider(persona.customer_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取客户档案失败（%s）：%s", persona.customer_id, exc)
            return {}
        if profile is None:
            return {}
        # profile 可能是 pydantic 对象或 dict
        data = profile.model_dump() if hasattr(profile, "model_dump") else dict(profile)
        return {
            "qualifications": data.get("qualifications", []),
            "monthly_income": data.get("monthly_income"),
            "credit_query_6m": data.get("credit_query_6m"),
            "overdue_count_12m": data.get("overdue_count_12m"),
            "loans_count": data.get("loans_count"),
            "needs": data.get("needs", []),
            "desired_amount": data.get("desired_amount"),
        }

    def stats(self) -> dict[str, Any]:
        return {
            "personas": len(self.personas),
            "conversations": len(self.conversations),
            "sessions": len(self.store.list_sessions()),
            "judge_source": "blended" if self.judge._use_llm else "rules",
        }

    def close(self) -> None:
        self.store.close()
