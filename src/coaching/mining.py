"""高转化话术挖掘。

对应简历项目三的"从成交客户的真实对话里自动提炼有效话术，回流话术库与陪练题库，
形成'真实对话 → 陪练素材 → 更好的对话'的数据闭环"。

挖掘判据（确定性，可解释）：
    1. **不能违规**：含承诺性表述的一律排除（这是硬前提，不是扣分项）；
    2. **有明确业务动作**：属于 资质问询 / 合规说明 / 异议处理 / 邀约 四类之一；
    3. **客户有正向反应**：业务员说完之后，客户的下一条消息是正向的
       —— 这是"这句话有效"的最直接证据（A/B 之外最可靠的自然实验）；
    4. **表达质量**：长度适中、信息密度够。
"""

from __future__ import annotations

import math
import re
from typing import Any

from agent_platform.llm.base import EmbeddingProvider
from agent_platform.logging_config import get_logger
from coaching.domain import MinedPhrase, MiningResult
from coaching.judge import _COMPLIANCE_MARKERS, VIOLATION_PATTERNS
from coaching.qc import _INVITE_MARKERS, _QUALIFICATION_MARKERS
from coaching.roleplay import detect_intents

logger = get_logger(__name__)

# 出现在业务员话术之后，说明客户被说动的正向信号
_POSITIVE_MARKERS = (
    "好的", "好的", "行", "可以", "明白", "对", "是的", "那我", "我去", "安排了",
    "有道理", "嗯嗯", "行吧", "那就", "你说得", "听起来",
)
# 客户开始提供信息，也是正向信号
_INFO_MARKERS = ("我公积金", "我社保", "我房子", "我月收入", "我查过", "我有", "大概")

_STAGE_ORDER = ("资质问询", "合规说明", "异议处理", "邀约")


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度（embedding 去重路径）。

    零向量 / 维度不一致一律按 0（不相似）处理：向量化结果异常时宁可少去重、不可误杀。
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class PhraseMiner:
    """话术挖掘器。"""

    def __init__(
        self,
        *,
        min_score: float = 0.5,
        min_length: int = 15,
        max_length: int = 300,
        dup_threshold: float = 0.8,
    ) -> None:
        self.min_score = min_score
        self.min_length = min_length
        self.max_length = max_length
        # 近似去重阈值：离线实现是"字符 2-gram Jaccard"，注入 embedding 后是余弦相似度，
        # 两套量纲不同 —— 换真实 embedding 后必须重新探测校准，不能沿用默认值
        self.dup_threshold = dup_threshold

    def mine(
        self, conversations: list[dict[str, Any]], *, embedder: EmbeddingProvider | None = None
    ) -> MiningResult:
        """从对话语料中挖掘高价值话术。

        embedder：可选的向量化实现（用于语义去重）。不传时退化为
        "字符 2-gram Jaccard ≥ 阈值"的确定性去重，离线可测、结果可复现。
        """
        mined: list[MinedPhrase] = []
        stats: dict[str, int] = {"won": 0, "lost": 0, "pending": 0, "skipped_violation": 0}

        for conv in conversations:
            outcome = str(conv.get("outcome", ""))
            stats[outcome] = stats.get(outcome, 0) + 1

            # 只从"已成交"的对话里挖 —— 未成单对话里的表达不具备成功证据
            if outcome != "won":
                continue

            messages = conv.get("messages", [])
            for idx, msg in enumerate(messages):
                if msg.get("role") != "business":
                    continue

                text = msg.get("text", "").strip()

                # ---- 硬前提：不能含违规承诺 ----
                if any(re.search(p, text) for p, _ in VIOLATION_PATTERNS):
                    stats["skipped_violation"] = stats.get("skipped_violation", 0) + 1
                    continue

                if not (self.min_length <= len(text) <= self.max_length):
                    continue

                stage = self._detect_stage(text, messages, idx)
                if stage is None:
                    continue

                score, reason, reaction = self._score(text, messages, idx, stage)
                if score < self.min_score:
                    continue

                mined.append(
                    MinedPhrase(
                        text=text,
                        score=round(score, 3),
                        stage=stage,
                        reason=reason,
                        source_conversation=str(conv.get("conversation_id", "")),
                        customer_next_reaction=reaction,
                    )
                )

        # 去重两步走：
        #   第一步 精确去重：字面完全一致保留最高分（原有行为）；
        #   第二步 近似去重："利率很低"和"咱们利率特别低"这类同义话术不应重复入库。
        #           优先用注入的 embedding 判语义相似；没注入则用字符 2-gram Jaccard
        #           的确定性实现兜底 —— 向量化挂了也不能让重复话术灌进话术库。
        best: dict[str, MinedPhrase] = {}
        for p in mined:
            key = p.text.strip()
            if key not in best or p.score > best[key].score:
                best[key] = p
        ordered, dropped_dupes = self._dedupe_similar(list(best.values()), embedder=embedder)

        by_stage: dict[str, int] = {}
        for p in ordered:
            by_stage[p.stage] = by_stage.get(p.stage, 0) + 1

        result = MiningResult(
            scanned=len(conversations),
            mined=ordered,
            stats={
                **stats,
                "dropped_dupes": dropped_dupes,
                **{f"stage_{k}": v for k, v in by_stage.items()},
            },
        )
        logger.info(
            "话术挖掘完成：扫描 %d 段对话，产出 %d 条话术，近似去重丢弃 %d 条",
            len(conversations), len(ordered), dropped_dupes,
        )
        return result

    # ------------------------------------------------------------------ #
    # 内部分析
    # ------------------------------------------------------------------ #
    def _dedupe_similar(
        self, candidates: list[MinedPhrase], *, embedder: EmbeddingProvider | None = None
    ) -> tuple[list[MinedPhrase], int]:
        """近似去重：按分数从高到低扫，与任何已保留话术相似即丢弃。

        为什么按分数贪心而不是聚类：需求是"每组重复保留得分最高的一条"，
        从高分往低分先到先得天然满足；候选量级（成单对话的命中句）不大，
        两两比对足够，不值得引入聚类复杂度。
        返回 (去重后的话术列表, 丢弃数)。
        """
        ordered = sorted(candidates, key=lambda p: -p.score)
        if len(ordered) < 2:
            return ordered, 0

        # 相似度实现：优先 embedding（语义准），失败/未注入退化为字符 2-gram（确定性）
        use_embedding = embedder is not None
        vectors: list[Any] = []
        if use_embedding:
            try:
                vectors = embedder.embed_documents([p.text for p in ordered])
            except Exception:  # noqa: BLE001 - 向量化挂了不能拖垮挖掘，退化离线实现
                logger.warning("话术向量化失败，退化为字符 2-gram Jaccard 去重", exc_info=True)
                use_embedding = False
        if not use_embedding:
            vectors = [self._char_bigrams(p.text) for p in ordered]

        kept: list[MinedPhrase] = []
        kept_vectors: list[Any] = []
        dropped = 0
        for phrase, vector in zip(ordered, vectors, strict=True):
            if any(
                self._similarity(vector, kept_vector, use_embedding) >= self.dup_threshold
                for kept_vector in kept_vectors
            ):
                dropped += 1  # 重复组里分数较低的那条直接丢弃，不再回库
                continue
            kept.append(phrase)
            kept_vectors.append(vector)
        return kept, dropped

    @staticmethod
    def _char_bigrams(text: str) -> set[str]:
        """字符 2-gram 集合：中文语义去重最朴素的确定性实现，离线可测、结果可复现。"""
        return {text[i : i + 2] for i in range(len(text) - 1)}

    @staticmethod
    def _similarity(a: Any, b: Any, use_embedding: bool) -> float:
        """两条话术的相似度：embedding 路径用余弦，退化路径用 Jaccard。"""
        if use_embedding:
            return _cosine_similarity(a, b)
        union = a | b
        if not union:
            return 0.0
        return len(a & b) / len(union)

    @staticmethod
    def _detect_stage(text: str, messages: list[dict], idx: int) -> str | None:
        """判断这句话属于哪个业务阶段。不属于四类之一则返回 None。"""
        intents = detect_intents(text)

        # 优先次序：合规说明 > 资质问询 > 邀约 > 异议处理（异议需结合上下文）
        if any(m in text for m in _COMPLIANCE_MARKERS):
            return "合规说明"
        if any(k in text for k in _QUALIFICATION_MARKERS) and "ask_qualification" in intents:
            return "资质问询"
        if any(k in text for k in _INVITE_MARKERS):
            return "邀约"
        # 异议处理：前一条客户消息是异议，本条是回应
        prev_customer = next(
            (m for m in reversed(messages[:idx]) if m.get("role") == "customer"), None
        )
        if prev_customer and PhraseMiner._is_objection(prev_customer.get("text", "")):
            return "异议处理"
        return None

    @staticmethod
    def _is_objection(text: str) -> bool:
        markers = ("考虑", "别家", "太高", "太贵", "保证", "骗人", "忙", "没时间", "白跑", "对比", "不信")
        return any(m in text for m in markers)

    def _score(
        self, text: str, messages: list[dict], idx: int, stage: str
    ) -> tuple[float, str, str]:
        """给话术打分，并说明为什么它有效。"""
        score = 0.5  # 基线：属于有效阶段 + 未违规
        reasons: list[str] = [f"属于「{stage}」关键动作"]

        # ---- 客户下一条消息的反应（最重要的证据）----
        next_customer = next(
            (m.get("text", "") for m in messages[idx + 1 :] if m.get("role") == "customer"),
            "",
        )
        if any(m in next_customer for m in _POSITIVE_MARKERS):
            score += 0.3
            reasons.append("客户随后给出正向回应")
        elif any(m in next_customer for m in _INFO_MARKERS):
            score += 0.25
            reasons.append("客户随后主动提供资质信息")

        # ---- 表达质量 ----
        if 20 <= len(text) <= 120:
            score += 0.1
            reasons.append("篇幅适中、信息密度合适")
        if text.count("？") + text.count("?") >= 1:
            score += 0.05  # 用提问引导对话是优秀销售话术的特征
            reasons.append("以提问引导对话")

        # ---- 阶段特有加分 ----
        if stage == "合规说明":
            score += 0.1
            reasons.append("主动做合规风险提示，降低后续预期落差")
        if stage == "邀约" and any(k in text for k in ("周六", "周日", "明天", "上午", "下午", "几点")):
            score += 0.1
            reasons.append("邀约包含具体时间锚点")

        return min(1.0, score), "；".join(reasons), next_customer[:60]
