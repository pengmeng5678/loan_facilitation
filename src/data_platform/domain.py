"""数据中台的领域模型。

四层数据资产各有一个模型，字段设计的原则是：
**下游要什么，就在这一层存什么**（而不是让下游自己再算一遍）。
比如 ConversationBlock 里直接带 `qa_pairs` / `quality_score` / `dataset_eligible`，
导出层与知识层就不需要重复解析对话。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# 分类与状态
# --------------------------------------------------------------------------- #
#: 9 级优先级分类（数字越小优先级越高，命中即止）。
#: **敏感(3) 刻意排在过短(5) 之前**：一条两个字的消息可能就是投诉苗头，
#: 风险不能因为"字少"被漏掉（见 filters.py 文件头）。
Category = Literal[
    "system",       # 1 系统消息（撤回/入群/拍一拍/红包）
    "media",        # 2 媒体消息（图片/语音/视频/文件，无文本）
    "sensitive",    # 3 敏感（投诉/维权/催收/监管）→ 可审但永不进训练集
    "spam",         # 4 垃圾广告（推销/加群/刷屏/外链）
    "too_short",    # 5 过短（无信息量）
    "chitchat",     # 6 闲聊（寒暄、表情、单字应答）
    "duplicate",    # 7 重复（同一内容反复出现）
    "normal",       # 8 普通有效业务对话
    "high_value",   # 9 高价值（成交/明确需求/资质完整/成功邀约）
]

CATEGORY_NAMES: dict[str, str] = {
    "system": "系统消息",
    "media": "媒体消息",
    "sensitive": "敏感对话",
    "spam": "垃圾广告",
    "too_short": "过短",
    "chitchat": "闲聊",
    "duplicate": "重复内容",
    "normal": "普通有效",
    "high_value": "高价值",
}

#: 分类优先级（与 filters.py 的判定顺序一致）
CATEGORY_PRIORITY: dict[str, int] = {
    "system": 1,
    "media": 2,
    "sensitive": 3,
    "spam": 4,
    "too_short": 5,
    "chitchat": 6,
    "duplicate": 7,
    "normal": 8,
    "high_value": 9,
}

Role = Literal["customer", "business", "system", "other"]
StagingStatus = Literal["pending", "approved", "rejected"]
ReviewAction = Literal["submit", "approve", "reject", "edit"]
ExportFormat = Literal["sharegpt", "alpaca", "openai", "rag_csv"]

#: 允许进入暂存区（待人工审核）的分类。
#: 注意包含 `sensitive` —— 投诉/维权对话必须让主管看到，
#: 但它的 dataset_eligible=False，永远不会进训练集。
KEEP_CATEGORIES: frozenset[str] = frozenset({"normal", "high_value", "sensitive"})

#: 脱敏规则类型（价格单独一类：它不是 PII，但是商业红线）
MaskKind = Literal["phone", "id_card", "bank_card", "email", "api_key", "name", "price"]
MaskLayer = Literal["clean", "write", "export"]


# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #
class MaskingFinding(BaseModel):
    """一次脱敏命中。start/end 是原文里的字符区间，便于审计与前端高亮。

    `preview` 存**脱敏后**的片段（而不是原文）——审计日志里再放一遍
    原始手机号，等于为了审计又泄露一次。
    """

    rule: str
    kind: MaskKind
    start: int
    end: int
    preview: str


class MaskedText(BaseModel):
    """脱敏结果。"""

    text: str
    findings: list[MaskingFinding] = Field(default_factory=list)
    price_hits: int = 0
    layers: list[MaskLayer] = Field(default_factory=list)

    @property
    def hit_count(self) -> int:
        return len(self.findings)


# --------------------------------------------------------------------------- #
# 原始层
# --------------------------------------------------------------------------- #
class RawMessage(BaseModel):
    """一条原始消息（raw_messages 表的一行，只增不改）。"""

    msg_id: str
    session_id: str
    sender_wxid: str = ""
    sender_name: str = ""
    role: Role = "other"
    content: str = ""
    ts: float = 0.0
    msg_type: str = "text"
    is_group: bool = False


class ChatSession(BaseModel):
    """一次会话（按 customer 维度聚合后的消息序列）。"""

    session_id: str
    customer_id: str = ""
    customer_name: str = ""
    is_group: bool = False
    messages: list[RawMessage] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# 清洗层
# --------------------------------------------------------------------------- #
class QAPair(BaseModel):
    """从对话里抽出的问答对。

    两种方向都收：
      · 业务员问 → 客户答（资质/征信/需求，做客户画像的原料）
      · 客户问 → 业务员答（产品/流程/费用，做问答库与陪练素材）
    """

    question: str
    answer: str
    asker: Role
    ts: float = 0.0


class BlockStats(BaseModel):
    turns: int = 0
    chars: int = 0
    customer_chars: int = 0
    business_chars: int = 0


class FilterDecision(BaseModel):
    """分类结论。`keep` 表示"是否进入暂存区待审"。

    `dataset_eligible` 与 `keep` 刻意分开：
    敏感对话**要进暂存区**（主管必须看到投诉苗头），但**永远不进训练集**。
    把这两个概念合成一个布尔值，是这类系统最常见的合规漏洞。
    """

    category: Category
    reason: str
    keep: bool
    dataset_eligible: bool = True
    risk: bool = False


class ConversationBlock(BaseModel):
    """清洗后的对话块 —— 数据中台的核心中间产物。"""

    block_id: str
    session_id: str
    customer_id: str = ""
    customer_name: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    messages: list[RawMessage] = Field(default_factory=list)
    category: Category = "normal"
    category_reason: str = ""
    quality_score: float = 0.0
    dataset_eligible: bool = True
    qa_pairs: list[QAPair] = Field(default_factory=list)
    findings: list[MaskingFinding] = Field(default_factory=list)
    price_hits: int = 0
    stats: BlockStats = Field(default_factory=BlockStats)

    @property
    def keepable(self) -> bool:
        """是否进入暂存区待审（丢弃类分类不进来，避免人工审一堆垃圾）。"""
        return self.category in KEEP_CATEGORIES


class StagedConversation(BaseModel):
    """暂存区条目（人工审核闸门的载体）。"""

    staging_id: str
    block_id: str
    status: StagingStatus = "pending"
    revision: int = 1
    category: Category = "normal"
    quality_score: float = 0.0
    dataset_eligible: bool = True
    session_id: str = ""
    customer_id: str = ""
    messages: list[RawMessage] = Field(default_factory=list)
    qa_pairs: list[QAPair] = Field(default_factory=list)
    findings_count: int = 0
    price_hits: int = 0
    reviewer: str = ""
    comment: str = ""
    created_at: str = ""
    updated_at: str = ""
    decided_at: str = ""

    @property
    def turns(self) -> int:
        return len(self.messages)


class ReviewRecord(BaseModel):
    """一次审核动作（review_audit 表，可审计、不可篡改）。"""

    audit_id: str
    staging_id: str
    action: ReviewAction
    from_status: str
    to_status: str
    reviewer: str
    comment: str = ""
    revision: int = 1
    created_at: str = ""


# --------------------------------------------------------------------------- #
# 复用层
# --------------------------------------------------------------------------- #
class ExportManifest(BaseModel):
    """一次导出的清单。`content_hash` 让"同输入同输出"可验证。"""

    export_id: str
    format: ExportFormat
    path: str
    rows: int
    bytes: int
    content_hash: str
    watermark: str
    staging_ids: list[str] = Field(default_factory=list)
    reused: bool = False
    created_at: str = ""


class KnowledgeChunk(BaseModel):
    """向量化切片。"""

    chunk_id: str
    staging_id: str
    session_id: str = ""
    text: str = ""
    scene: str = ""
    quality_score: float = 0.0
    vector: list[float] = Field(default_factory=list)


class KnowledgeArticle(BaseModel):
    """结构化知识文章（场景 / 客户话术 / 推荐回复 / 置信度）。

    `extractor` 标明来源是模型还是规则兜底 —— 面试被问"这条是模型提的还是规则提的"
    时能如实回答；也给下游一个"要不要人工复核"的依据。
    """

    article_id: str
    staging_id: str
    scene: str = ""
    customer_utterance: str = ""
    recommended_reply: str = ""
    confidence: float = 0.0
    extractor: Literal["llm", "rule"] = "rule"


# --------------------------------------------------------------------------- #
# 报表
# --------------------------------------------------------------------------- #
class IngestReport(BaseModel):
    source: str = ""
    session_id: str = ""
    #: 抽取批次号：raw_messages 逐条携带、ingest_batches 同 id —— 可追溯"哪批数据来自哪次抽取"
    batch_id: str = ""
    raw_messages: int = 0
    sessions: int = 0
    blocks_total: int = 0
    blocks_kept: int = 0
    blocks_dropped: int = 0
    staged_new: int = 0
    staged_existing: int = 0
    masked_findings: int = 0
    price_hits: int = 0
    by_category: dict[str, int] = Field(default_factory=dict)
    dry_run: bool = False
    elapsed_ms: float = 0.0

    def to_text(self) -> str:
        return (
            f"抽取 {self.raw_messages} 条消息 → {self.sessions} 个会话 → "
            f"{self.blocks_total} 个对话块（保留 {self.blocks_kept} / 丢弃 {self.blocks_dropped}）→ "
            f"新增待审 {self.staged_new}（已存在 {self.staged_existing}）；"
            f"脱敏 {self.masked_findings} 处（价格红线 {self.price_hits} 处）"
        )


class KnowledgeReport(BaseModel):
    chunks: int = 0
    articles: int = 0
    llm_articles: int = 0
    rule_articles: int = 0
    skipped: int = 0
    elapsed_ms: float = 0.0
    #: 是否增量构建（只处理上次水位之后新批准的条目），给调用方区分口径
    incremental: bool = False

    def to_text(self) -> str:
        head = "增量知识产物" if self.incremental else "知识产物"
        return (
            f"{head}：切片 {self.chunks} 条、文章 {self.articles} 条"
            f"（模型 {self.llm_articles} / 规则 {self.rule_articles}），跳过 {self.skipped}"
        )
