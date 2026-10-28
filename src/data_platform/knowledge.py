"""知识产物：向量切片 + 结构化文章。

两个产物，对应两种用法：
    knowledge_chunks    原样切片 + 向量 → 检索（RAG 的原料）
    knowledge_articles  结构化提炼（场景/客户话术/推荐回复）→ 陪练题库与话术库

**为什么文章必须有规则兜底**：
    LLM 结构化输出会失败（模型不支持 JSON、返回被截断、字段缺失）。
    如果失败就丢样本，知识库会"时多时少"，下游无法复现。
    所以这里的设计是：**模型失败 → 规则从 QA 对里抽一条**，
    并在 `extractor` 字段如实标注来源。宁可置信度低一点，
    也不能让管线因为模型抖动而不可用。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

from agent_platform.llm.base import EmbeddingProvider, LLMProvider, Message
from agent_platform.logging_config import get_logger
from data_platform.domain import KnowledgeArticle, KnowledgeChunk, KnowledgeReport
from data_platform.filters import ConversationFilter
from data_platform.masking import MaskingEngine
from data_platform.store import DataPlatformStore

logger = get_logger(__name__)

_ARTICLE_SCHEMA = {
    "type": "object",
    "properties": {
        "scene": {"type": "string", "description": "客户所处业务场景，如 债务置换/征信解读/利率费用/资质准入/邀约到店/流程资料"},
        "customer_utterance": {"type": "string", "description": "客户的原话（一句，保留口语）"},
        "recommended_reply": {"type": "string", "description": "业务员的推荐回复（合规、不承诺）"},
        "confidence": {"type": "number", "description": "0-1 置信度"},
    },
    "required": ["scene", "customer_utterance", "recommended_reply", "confidence"],
}

_SENTENCE_END = re.compile(r"(?<=[。！？!?；;\n])")


class KnowledgeBuilder:
    """从已批准对话里构建检索与题库产物。"""

    def __init__(
        self,
        store: DataPlatformStore,
        *,
        llm: LLMProvider,
        embedder: EmbeddingProvider,
        conv_filter: ConversationFilter,
        masker: MaskingEngine,
        chunk_chars: int = 300,
        chunk_overlap: int = 60,
    ) -> None:
        self.store = store
        self.llm = llm
        self.embedder = embedder
        self.filter = conv_filter
        self.masker = masker
        self.chunk_chars = max(chunk_chars, 80)
        self.chunk_overlap = max(0, min(chunk_overlap, chunk_chars // 2))

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def build(self, *, limit: int = 0, since: str | None = None) -> KnowledgeReport:
        """构建知识产物。

        since=None：全量重建（原行为，兼容现有调用）；
        since=水位：**增量** —— 只处理该时刻之后批准的条目。store 层的
        replace_chunks / save_articles 本来就是"先按 staging_id 删再插"的
        upsert 语义，所以新条目天然是"向量库 upsert + 文章按 source 去重更新"，
        旧条目不会被重算，ETL 不再随数据量线性变慢。

        **增量模式不处理删除**：条目事后被驳回/撤销时，它的残留切片要靠
        全量重建（since=None）清理 —— 这是"增量快"换来的明确取舍。
        """
        started = _now_ms()
        if since is None:
            staged = self.store.approved_staged(eligible_only=True)
        else:
            staged = self.store.approved_staged_since(since, eligible_only=True)
        if limit:
            staged = staged[:limit]
        if not staged:
            return KnowledgeReport(elapsed_ms=_now_ms() - started, incremental=since is not None)

        chunks: list[KnowledgeChunk] = []
        texts: list[str] = []
        owners: list[tuple[str, str, str, float]] = []  # (staging_id, session_id, scene, quality)
        for item in staged:
            masked_turns = []
            for msg in item.messages:
                if msg.role not in ("customer", "business"):
                    continue
                masked = self.masker.mask(msg.content, layer="write")
                masked_turns.append(
                    f"{'客户' if msg.role == 'customer' else '业务员'}：{masked.text}"
                )
            text = "\n".join(masked_turns)
            if not text.strip():
                continue
            scene = self.filter.scene_of(text)
            for piece in self._split(text):
                texts.append(piece)
                owners.append((item.staging_id, item.session_id, scene, item.quality_score))

        if texts:
            vectors = self._embed(texts)
            for idx, (piece, vector) in enumerate(zip(texts, vectors, strict=False)):
                staging_id, session_id, scene, quality = owners[idx]
                chunks.append(KnowledgeChunk(
                    chunk_id=f"chk_{_sha1(f'{staging_id}|{idx}|{piece[:20]}')[:12]}",
                    staging_id=staging_id,
                    session_id=session_id,
                    text=piece,
                    scene=scene,
                    quality_score=quality,
                    vector=vector,
                ))
            self.store.replace_chunks(chunks, created_at=datetime.now().isoformat(timespec="seconds"))

        articles = [self._propose(item) for item in staged]
        self.store.save_articles(articles, created_at=datetime.now().isoformat(timespec="seconds"))
        llm_count = sum(1 for a in articles if a.extractor == "llm")
        return KnowledgeReport(
            chunks=len(chunks),
            articles=len(articles),
            llm_articles=llm_count,
            rule_articles=len(articles) - llm_count,
            skipped=max(0, len(staged) - len(articles)),
            elapsed_ms=_now_ms() - started,
            incremental=since is not None,
        )

    # ------------------------------------------------------------------ #
    # 检索
    # ------------------------------------------------------------------ #
    def search(self, query: str, *, top_k: int = 3) -> list[dict[str, object]]:
        """向量检索（余弦相似度）。空库返回空列表，不抛异常。"""
        chunks = self.store.list_chunks(limit=2000)
        if not chunks or not query.strip():
            return []
        try:
            query_vec = self.embedder.embed_query(query)
        except Exception as exc:  # noqa: BLE001 - 检索失败降级为关键词匹配
            logger.warning("查询向量化失败，降级关键词匹配：%s", exc)
            return self._keyword_search(query, chunks, top_k=top_k)

        scored: list[tuple[float, KnowledgeChunk]] = []
        for chunk in chunks:
            score = _cosine(query_vec, chunk.vector)
            if score > 0:
                scored.append((score, chunk))
        if not scored:
            return self._keyword_search(query, chunks, top_k=top_k)
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            {
                "chunk_id": c.chunk_id,
                "staging_id": c.staging_id,
                "scene": c.scene,
                "text": c.text,
                "score": round(score, 4),
            }
            for score, c in scored[: max(1, top_k)]
        ]

    def _keyword_search(
        self, query: str, chunks: list[KnowledgeChunk], *, top_k: int
    ) -> list[dict[str, object]]:
        """离线兜底：按字符重合度排序（不依赖向量，永远可用）。"""
        tokens = [t for t in re.split(r"\s+", query.strip()) if t]
        scored = []
        for chunk in chunks:
            hits = sum(1 for t in tokens if t and t in chunk.text)
            if hits:
                scored.append((float(hits), chunk))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            {
                "chunk_id": c.chunk_id,
                "staging_id": c.staging_id,
                "scene": c.scene,
                "text": c.text,
                "score": score,
            }
            for score, c in scored[: max(1, top_k)]
        ]

    # ------------------------------------------------------------------ #
    # 文章提炼
    # ------------------------------------------------------------------ #
    def _propose(self, staged) -> KnowledgeArticle:
        text = "\n".join(
            f"{'客户' if m.role == 'customer' else '业务员'}：{m.content}"
            for m in staged.messages
            if m.role in ("customer", "business")
        )
        rule_scene = self.filter.scene_of(text)
        try:
            payload = self.llm.complete_json(
                [
                    Message(role="system", content=(
                        "你是助贷业务的知识工程师。从对话里提炼一条可复用的知识："
                        "客户在什么场景说了什么、业务员应该怎么回。"
                        "回复必须合规：不承诺放款、不承诺具体利率、不编造额度。"
                        "只输出 JSON。"
                    )),
                    Message(role="user", content=f"对话如下：\n{text[:2000]}"),
                ],
                _ARTICLE_SCHEMA,
            )
            article = self._article_from_payload(staged, payload, fallback_scene=rule_scene)
            if article is not None:
                return article
        except Exception as exc:  # noqa: BLE001 - 模型/解析失败一律走规则兜底
            logger.info("文章提炼走规则兜底（%s）：%s", staged.staging_id, exc)
        return self._rule_article(staged, rule_scene)

    def _article_from_payload(
        self, staged, payload: dict, *, fallback_scene: str
    ) -> KnowledgeArticle | None:
        """校验模型输出：字段齐、非空、置信度合法才收。"""
        try:
            scene = str(payload.get("scene") or fallback_scene).strip()[:32]
            utterance = str(payload.get("customer_utterance") or "").strip()
            reply = str(payload.get("recommended_reply") or "").strip()
            confidence = float(payload.get("confidence") or 0)
        except (TypeError, ValueError):
            return None
        if not utterance or not reply:
            return None
        # 模型输出也要过脱敏：模型可能把对话里的手机号"复述"进来
        utterance = self.masker.mask(utterance, layer="write").text
        reply = self.masker.mask(reply, layer="write").text
        return KnowledgeArticle(
            article_id=f"art_{_sha1(staged.staging_id + utterance)[:12]}",
            staging_id=staged.staging_id,
            scene=scene or fallback_scene,
            customer_utterance=utterance,
            recommended_reply=reply,
            confidence=round(max(0.0, min(1.0, confidence)), 3),
            extractor="llm",
        )

    def _rule_article(self, staged, scene: str) -> KnowledgeArticle:
        """规则兜底：从已抽好的 QA 对里挑一条"客户问 → 业务员答"。

        为什么优先客户提问：
            知识文章是给业务员/陪练用的，"客户这么问、你这么答"最有复用价值；
            业务员问客户（资质问询）属于流程动作，复用性弱。
        """
        pairs = [p for p in staged.qa_pairs if p.asker == "customer"]
        if not pairs:
            pairs = list(staged.qa_pairs)
        if pairs:
            pair = pairs[0]
            utterance, reply = pair.question, pair.answer
        else:
            # 连 QA 都没有：退到"客户最后一句 + 业务员最后一句"
            customers = [m for m in staged.messages if m.role == "customer"]
            businesses = [m for m in staged.messages if m.role == "business"]
            utterance = customers[-1].content if customers else ""
            reply = businesses[-1].content if businesses else ""
        confidence = round(min(0.75, max(0.2, staged.quality_score * 0.85)), 3)
        return KnowledgeArticle(
            article_id=f"art_{_sha1(staged.staging_id + 'rule')[:12]}",
            staging_id=staged.staging_id,
            scene=scene,
            customer_utterance=utterance,
            recommended_reply=reply,
            confidence=confidence,
            extractor="rule",
        )

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _split(self, text: str) -> list[str]:
        """按句子边界切片（保留重叠，避免答案被切断）。"""
        sentences = [s for s in _SENTENCE_END.split(text) if s.strip()]
        chunks: list[str] = []
        buf = ""
        for sentence in sentences:
            if len(buf) + len(sentence) <= self.chunk_chars:
                buf += sentence
                continue
            if buf.strip():
                chunks.append(buf.strip())
            # 重叠：把上一块尾部带进下一块
            tail = buf[-self.chunk_overlap:] if self.chunk_overlap else ""
            buf = tail + sentence
            while len(buf) > self.chunk_chars:
                chunks.append(buf[: self.chunk_chars].strip())
                buf = buf[self.chunk_chars - self.chunk_overlap:]
        if buf.strip():
            chunks.append(buf.strip())
        return [c for c in chunks if len(c) >= 10]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        try:
            vectors = self.embedder.embed_documents(texts)
        except Exception as exc:  # noqa: BLE001 - 向量失败不阻断切片入库
            logger.warning("向量化失败，切片以空向量入库（检索降级关键词）：%s", exc)
            return [[] for _ in texts]
        return [list(v) for v in vectors]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _now_ms() -> float:
    import time

    return time.perf_counter() * 1000


__all__ = ["KnowledgeBuilder"]
