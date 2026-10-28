"""项目一（AI 实时语音对话智能客服）的领域模型。

一条完整的语音会话长这样：

    ① 客户端拿到 /voice/scenes  → 选场景（助贷顾问 / 额度测算 / 资料清单…）
    ② POST /voice/rooms         → 服务端签发 RTC Token（001 方案），前端用它进房
    ③ POST /voice/rooms/start   → 服务端调 RTC OpenAPI「StartVoiceChat」把云端
                                  AI 拉进同一个房间（ASR / TTS / 第三方 LLM 都在这配）
    ④ RTC 云端把 ASR 出来的文本 POST 到我们的 /voice/callback/chat
       → 我们做 RAG 检索 → 拼 system prompt → 调 LLM 流式输出
       → 把 SSE 分片回给 RTC 云端 → 云端 TTS 合成语音推回房间
    ⑤ 每一轮都落库（VoiceTurn），带延迟拆解，供观测与优化
    ⑥ POST /voice/rooms/stop     → 挂断，收尾

**为什么服务端只负责"文本这一小段"**：
    音频流走 RTC 的 UDP 通道（浏览器 ↔ 火山边缘节点 ↔ 云端 Agent），
    我们的 Python 服务既不收也不发音频 —— 所以它永远不会成为带宽/CPU 瓶颈。
    服务端做的只有三件事：签发进房凭证、下发会话配置、处理 LLM 回调。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

PrivacyLevel = Literal["public", "internal", "sensitive"]


# =========================================================================== #
# 配置类模型：场景 / 房间 / Token
# =========================================================================== #
class VoiceScene(BaseModel):
    """一个语音接待场景。

    场景 = 人设（prompt）+ 语音参数（语速/音色）+ 检索策略。
    业务上就是"客户打进来时，AI 以什么身份、用什么口径接待"。
    """

    scene_id: str
    name: str
    description: str = ""
    # 人设提示词在 PromptRegistry 里的键
    prompt_key: str
    # 云端 Agent 在房间里的用户 ID（前端靠它区分"谁在说话"）
    bot_user_id: str = "Huoshan01"
    welcome_message: str = ""

    # ---- 推理参数（语音场景和文本场景的取值完全不同，见 dialog.py 的说明）----
    temperature: float = Field(default=0.3, ge=0, le=2)
    max_tokens: int = Field(default=220, ge=50, le=1024)

    # ---- 检索参数 ----
    knowledge_limit: int = Field(default=2, ge=0, le=5, description="注入几条知识片段（1-3 最佳）")
    knowledge_tags: list[str] = Field(default_factory=list, description="知识库字段过滤（业务分类）")

    # ---- 前端展示 ----
    icon: str = ""
    interrupt_mode: bool = Field(default=True, description="是否允许用户打断 AI（语音体验关键）")

    # ---- 语音音色（TTS）----
    voice_type: str = ""
    speed_ratio: float = Field(default=1.0, ge=0.2, le=3.0)

    def public_dict(self) -> dict[str, Any]:
        """给前端 /scenes 用的结构（不含任何密钥）。"""
        return {
            "scene": {
                "id": self.scene_id,
                "name": self.name,
                "description": self.description,
                "icon": self.icon,
                "botName": self.bot_user_id,
                "isInterruptMode": self.interrupt_mode,
                "welcomeMessage": self.welcome_message,
            }
        }


class RoomJoinInfo(BaseModel):
    """一次进房所需的全部凭证（前端 joinRoom 直接用）。"""

    scene_id: str
    app_id: str
    room_id: str
    user_id: str
    token: str
    bot_user_id: str
    expire_at: int = Field(description="Token 过期时间（Unix 秒）")
    dry_run: bool = Field(default=False, description="离线 dry-run 模式下为 True（凭证不可用于真实进房）")

    def public_dict(self) -> dict[str, Any]:
        """对外输出时把 token 换成掩码版本 —— 日志/截图里泄露 token 等于泄露房间权限。"""

        return {
            "AppId": self.app_id,
            "RoomId": self.room_id,
            "UserId": self.user_id,
            "Token": self.token,
            "BotUserId": self.bot_user_id,
            "ExpireAt": self.expire_at,
            "DryRun": self.dry_run,
        }

    def masked(self) -> dict[str, Any]:
        from voice_service.rtc_token import mask_token

        data = self.public_dict()
        data["Token"] = mask_token(self.token)
        return data


class RtcCallResult(BaseModel):
    """一次 RTC OpenAPI 调用的结果。"""

    action: str
    ok: bool
    request_id: str = ""
    task_id: str = ""
    dry_run: bool = False
    error: str = ""
    payload: dict[str, Any] = Field(default_factory=dict, description="实际下发的配置（脱敏后）")


# =========================================================================== #
# 对话类模型
# =========================================================================== #
class LatencyBreakdown(BaseModel):
    """单轮延迟拆解 —— 语音场景的"性能"就是体感，必须可观测。

    语音链路和文本链路的差别在**首字延迟（TTFT）**：文本等 3 秒用户没感觉，
    语音等 3 秒就是"喂？在吗？"——所以这里把 RAG 与 LLM 分开计时，
    优化时才知道该压哪一段。
    """

    rag_ms: float = 0.0
    llm_ttft_ms: float = 0.0
    llm_total_ms: float = 0.0
    censor_ms: float = 0.0
    total_ms: float = 0.0
    chunks: int = 0

    def as_row(self) -> str:
        return (
            f"RAG {self.rag_ms:.0f}ms｜首字 {self.llm_ttft_ms:.0f}ms｜"
            f"LLM {self.llm_total_ms:.0f}ms｜合计 {self.total_ms:.0f}ms"
        )


class KnowledgeChunk(BaseModel):
    """一条知识片段（来自助贷政策库 / 外部知识库）。"""

    chunk_id: str
    text: str
    citation: str = ""
    score: float = 0.0
    dense_score: float = 0.0
    rerank_score: float = 0.0


class RetrievalResult(BaseModel):
    """一次检索的结果。"""

    query: str
    chunks: list[KnowledgeChunk] = Field(default_factory=list)
    context_block: str = ""
    citations: list[str] = Field(default_factory=list)
    elapsed_ms: float = 0.0
    hit: bool = True
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "hit": self.hit,
            "reason": self.reason,
            "chunks": [c.model_dump(mode="json") for c in self.chunks],
            "citations": self.citations,
            "elapsed_ms": round(self.elapsed_ms, 2),
        }


class CensorFinding(BaseModel):
    """内容安全命中项。"""

    rule_id: str
    category: str
    severity: Literal["high", "medium", "low"]
    matched: str
    suggestion: str = ""


class CensorVerdict(BaseModel):
    """内容安全结论。

    与项目七（素材）的区别：素材是"先审后发"，一次不过就不发；
    语音是"边说边发"，没有撤回的机会 —— 所以这里多一个 `safe_reply`：
    命中红线时**立刻用兜底话术替换**，而不是把违规内容念出去再道歉。
    """

    passed: bool = True
    needs_human: bool = False
    blocked: bool = False
    findings: list[CensorFinding] = Field(default_factory=list)
    sanitized_text: str = ""
    safe_reply: str = ""

    @property
    def max_severity(self) -> str:
        order = {"low": 0, "medium": 1, "high": 2}
        return max((f.severity for f in self.findings), key=lambda s: order[s], default="low")

    def to_text(self) -> str:
        if self.passed and not self.findings:
            return "内容安全：通过（未命中任何规则）"
        lines = [
            f"内容安全：{'拦截' if self.blocked else '通过'}"
            f"｜最高等级 {self.max_severity}｜命中 {len(self.findings)} 条"
            f"｜需人工 {self.needs_human}"
        ]
        for f in self.findings:
            lines.append(f"  · [{f.rule_id}/{f.severity}] {f.category}「{f.matched}」→ {f.suggestion}")
        if self.safe_reply:
            lines.append(f"  兜底话术：{self.safe_reply}")
        return "\n".join(lines)


class VoiceTurn(BaseModel):
    """一轮问答（用户一句 → AI 一段）。"""

    round_id: int
    user_text: str
    answer_text: str
    refused: bool = False
    needs_human: bool = False
    interrupted: bool = False
    citations: list[str] = Field(default_factory=list)
    prompt_version: str = ""
    latency: LatencyBreakdown = Field(default_factory=LatencyBreakdown)
    at: int = 0
    # 语音链路专有：这一轮 ASR 是否带情绪（来自火山 ASR 情绪检测）
    emotion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_id": self.round_id,
            "user_text": self.user_text,
            "answer_text": self.answer_text,
            "refused": self.refused,
            "needs_human": self.needs_human,
            "interrupted": self.interrupted,
            "citations": self.citations,
            "prompt_version": self.prompt_version,
            "latency": self.latency.model_dump(mode="json"),
            "at": self.at,
            "emotion": self.emotion,
        }

    def to_transcript(self) -> str:
        who = "客户" if not self.answer_text else "AI"
        return f"[第{self.round_id}轮] 客户：{self.user_text}\n           {who}：{self.answer_text}"


class VoiceSession(BaseModel):
    """一场通话。"""

    session_id: str
    scene_id: str
    room_id: str
    user_id: str
    bot_user_id: str = ""
    status: Literal["running", "stopped", "failed"] = "running"
    started_at: int = 0
    ended_at: int = 0
    turns: list[VoiceTurn] = Field(default_factory=list)

    @property
    def refused_count(self) -> int:
        return sum(1 for t in self.turns if t.refused)

    @property
    def handoff_count(self) -> int:
        return sum(1 for t in self.turns if t.needs_human)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "scene_id": self.scene_id,
            "room_id": self.room_id,
            "user_id": self.user_id,
            "bot_user_id": self.bot_user_id,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "turns": [t.to_dict() for t in self.turns],
            "summary": {
                "turns": len(self.turns),
                "refused": self.refused_count,
                "handoff": self.handoff_count,
            },
        }

    def to_transcript(self) -> str:
        head = f"会话 {self.session_id}｜场景 {self.scene_id}｜房间 {self.room_id}｜状态 {self.status}"
        if not self.turns:
            return head + "\n（暂无对话）"
        return head + "\n" + "\n".join(t.to_transcript() for t in self.turns)


# =========================================================================== #
# 提示词与评测模型
# =========================================================================== #
PromptStatus = Literal["active", "candidate", "archived"]


class PromptVersion(BaseModel):
    """提示词的一个版本。

    为什么提示词要版本化：语音场景的回复质量高度依赖提示词，
    而"改了提示词"是最容易出事又最说不清的一次变更。
    版本化 + Golden Set 回归，才能回答"这次改动到底变好了还是变坏了"。
    """

    key: str
    version: str
    system_prompt: str
    status: PromptStatus = "candidate"
    note: str = ""
    created_at: int = 0
    temperature: float = 0.3
    max_tokens: int = 220

    def render(self, *, knowledge_block: str = "", extra: str = "") -> str:
        """渲染最终 system prompt。

        注意：这里只做**字符串替换**，不做条件分支拼装 ——
        拼装逻辑一旦复杂，同一条用户问题就可能生成不同的前缀，
        前缀缓存（Prefix Caching）立刻失效，首字延迟会掉回 5 秒以上。
        """
        return (
            self.system_prompt.replace("{{knowledge}}", knowledge_block or "（无）")
            .replace("{{extra}}", extra or "")
            .strip()
        )


class EvalCase(BaseModel):
    """语音场景的一条评测用例。"""

    case_id: str
    question: str
    expect_keywords: list[str] = Field(default_factory=list)
    expect_mode: Literal["all", "any"] = "all"
    forbidden_keywords: list[str] = Field(default_factory=list)
    must_refuse: bool = False
    max_chars: int = Field(default=160, description="语音回复长度上限（超过就念不完）")
    scene_id: str = "consultant"


class EvalCaseResult(BaseModel):
    case_id: str
    question: str
    answer: str
    passed: bool
    score: float
    failures: list[str] = Field(default_factory=list)
    latency_ms: float = 0.0


class EvalReport(BaseModel):
    prompt_key: str
    version: str
    total: int
    passed: int
    score: float
    cases: list[EvalCaseResult] = Field(default_factory=list)

    def to_text(self) -> str:
        lines = [f"评测 {self.prompt_key}@{self.version}：{self.passed}/{self.total} 通过，得分 {self.score:.2f}"]
        for c in self.cases:
            mark = "✓" if c.passed else "✗"
            lines.append(f"  {mark} {c.case_id}（{c.score:.2f}）{('；'.join(c.failures)) if c.failures else ''}")
        return "\n".join(lines)


class GateDecision(BaseModel):
    """版本准入结论。"""

    allowed: bool
    reasons: list[str] = Field(default_factory=list)
    delta: float = 0.0
    current: float = 0.0
    baseline: float = 0.0
