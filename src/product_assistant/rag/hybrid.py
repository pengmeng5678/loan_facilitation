"""混合检索器：BM25 + 稠密向量 → RRF 融合 → Rerank → 时效过滤 → 引用溯源。

这是项目二检索链路的全部核心，也是面试最常深挖的地方。
每个环节对应一个真实问题：
    BM25            → 专有名词召回失效
    RRF 融合        → 两路检索的分数不可比（不能简单相加）
    Rerank          → 召回宽松后需要精细排序
    生效日期过滤    → 政策月月变，绝不能用旧利率误导客户
    引用溯源        → 助贷合规，"有据可查"是底线
    阈值拒答        → 无政策依据时必须拒答，而不是编造
"""

from __future__ import annotations

from datetime import date

from agent_platform.errors import RetrievalError
from agent_platform.llm.base import EmbeddingProvider
from agent_platform.logging_config import get_logger
from product_assistant.domain.models import RetrievedChunk
from product_assistant.rag.bm25_index import BM25Index
from product_assistant.rag.chunking import Chunk
from product_assistant.rag.tokenize import content_tokens
from product_assistant.rag.vector_store import VectorStore

logger = get_logger(__name__)

# RRF（Reciprocal Rank Fusion）的平滑常数，业界常用 60
RRF_K = 60


class LocalReranker:
    """轻量 Reranker：用"词项重合度 + 数字/编号一致 + 短语命中"给候选精排。

    生产替换点：真实环境应换成 bge-reranker / Cohere Rerank 这类交叉编码器
    （把 query 与 doc 拼一起过一遍模型，精度更高但更贵）。
    这里用确定性实现，保证离线可跑、结果可复现、测试可断言。
    """

    def score(self, query: str, chunk: Chunk) -> float:
        # 只用"多字词/英文词"算重合度：单字噪声太大，会让无关问题也拿高分
        q_tokens = set(content_tokens(query))
        d_tokens = set(content_tokens(chunk.searchable_text))
        if not q_tokens or not d_tokens:
            return 0.0

        overlap = len(q_tokens & d_tokens) / len(q_tokens)

        # 产品编号 / 条款号 精确命中是强信号，额外加权
        bonus = 0.0
        if chunk.citation.product_code and chunk.citation.product_code.lower() in query.lower():
            bonus += 0.25
        if chunk.citation.clause_no and chunk.citation.clause_no in query:
            bonus += 0.15
        # 标题命中
        if chunk.citation.title and chunk.citation.title in query:
            bonus += 0.10

        return min(1.0, overlap + bonus)


class HybridRetriever:
    """混合检索器。"""

    def __init__(
        self,
        embedder: EmbeddingProvider,
        store: VectorStore,
        *,
        rrf_k: int = RRF_K,
        rrf_weight: float = 0.5,
        min_score_ratio: float = 0.35,
        min_rerank_score: float = 0.15,
        min_dense_score: float = 0.20,
        bm25_recall_multiplier: int = 2,
    ) -> None:
        """
        参数：
            rrf_weight:       融合得分里 RRF 的占比（其余给 Reranker）
            min_score_ratio:  相对阈值 —— 低于"最高分 × 该比例"的结果丢弃（做截断用）
            min_rerank_score: **绝对相关性阈值（词项层面）** —— 拒答判据之一
            min_dense_score:  **绝对相关性阈值（语义层面）** —— 拒答判据之一
            bm25_recall_multiplier: BM25 原始召回深度 = recall_k × 该系数

        为什么拒答判据不能挂在"融合分"上：
            融合分里的 RRF 部分按 max 归一化后，**排第一名的永远是 1.0**，
            于是"最低分门槛"形同虚设——无关问题也会被当作有依据。
            必须用**绝对量**（rerank 的词项重合度 / dense 的余弦相似度）来判"有没有依据"。

        为什么 BM25 召回深度刻意比向量深一档（bm25_recall_multiplier=2）：
            两路召回在进入 RRF 前，**参与融合的有效深度都对齐到 recall_k**
            （BM25 先搜 recall_k×2、过滤后截到 recall_k；向量库在 search 内部
            就按 allow 过滤、最多返回 recall_k）。BM25 之所以要搜得更深，
            是因为它的过滤发生在召回之后（时效/产品过滤会扔掉一部分候选），
            搜浅了过滤完就凑不满 recall_k；而 BM25 索引是内存倒排，搜深一倍
            几乎零成本。向量检索成本随 k 增长，不值得同样搜深。
            **RRF 只看"过滤后列表里的名次"，两路名次口径一致，融合无偏差。**

        阈值校准说明：
            默认值针对离线 LocalHashEmbedding + LocalReranker 校准：
                相关问题  rerank ≥ 0.40，dense 0.27~0.48
                无关问题  rerank ≈ 0.00，dense 0.20~0.30
            可见"词项层"的区分度远高于"密集向量层"（local-hash 的余弦有较高基线），
            所以主判据是 min_rerank_score，min_dense_score 只作为语义补充分支。
            **换成真实 Embedding（text-embedding-v3 等）后必须重新探测并校准这两个值**
            （用 scripts/calibrate_rag.py，它会输出正负例分离度与建议阈值）。
        """
        self.embedder = embedder
        self.store = store
        self.bm25 = BM25Index()
        self.reranker = LocalReranker()
        self.rrf_k = rrf_k
        self.rrf_weight = rrf_weight
        self.min_score_ratio = min_score_ratio
        self.min_rerank_score = min_rerank_score
        self.min_dense_score = min_dense_score
        self.bm25_recall_multiplier = max(1, bm25_recall_multiplier)

        self._chunks: dict[str, Chunk] = {}

    # ------------------------------------------------------------------ #
    # 建库
    # ------------------------------------------------------------------ #
    def index(self, chunks: list[Chunk]) -> None:
        """把子块灌进向量库 + BM25 索引。"""
        if not chunks:
            logger.warning("索引语料为空")
            return

        self._chunks = {c.chunk_id: c for c in chunks}
        ids = [c.chunk_id for c in chunks]
        texts = [c.searchable_text for c in chunks]
        vectors = self.embedder.embed_documents(texts)
        payloads = [
            {
                "chunk_id": c.chunk_id,
                "product_code": c.citation.product_code,
                "clause_no": c.citation.clause_no,
                "policy_id": c.citation.policy_id,
                "text": c.text,
                **c.metadata,
            }
            for c in chunks
        ]
        self.store.upsert(ids, vectors, payloads)
        self.bm25.build(chunks)
        logger.info("检索索引就绪：向量库 %d 条 / BM25 %d 条", self.store.count(), self.bm25.size())

    # ------------------------------------------------------------------ #
    # 检索
    # ------------------------------------------------------------------ #
    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        as_of: date | None = None,
        product_code: str | None = None,
        recall_multiplier: int = 4,
    ) -> list[RetrievedChunk]:
        """执行一次混合检索。

        参数：
            as_of:        政策时间点。只有"生效日期 <= as_of < 失效日期"的条款参与检索。
                          **这是助贷最要命的一环：用过期政策给客户报价 = 客诉 + 合规风险。**
            product_code: 限定产品，用于"查某产品条款"这类精确诉求。
        """
        if not self._chunks:
            raise RetrievalError("检索索引为空，请先执行 index() 或运行 seed 脚本灌入政策数据。")

        # ---- ① 时效 / 产品 过滤：先算出"允许参与检索的 chunk 集合" ----
        allowed = self._allowed_ids(as_of=as_of, product_code=product_code)
        if not allowed:
            logger.info("时效/产品过滤后无可用条款（as_of=%s, product_code=%s）", as_of, product_code)
            return []

        recall_k = max(top_k * recall_multiplier, top_k)

        # ---- ② BM25 召回 ----
        # 多搜 bm25_recall_multiplier 倍再过滤截断：时效/产品过滤发生在召回后，
        # 搜浅了过滤完凑不满 recall_k（见 __init__ docstring 的深度对齐说明）
        bm25_hits: list[tuple[str, float, int]] = []
        for rank, hit in enumerate(
            self.bm25.search(query, recall_k * self.bm25_recall_multiplier), start=1
        ):
            cid = self.bm25.chunk_id_at(hit.index)
            if cid in allowed:
                bm25_hits.append((cid, hit.score, rank))
            if len(bm25_hits) >= recall_k:
                break

        # ---- ③ 稠密向量召回 ----
        q_vec = self.embedder.embed_query(query)
        dense_hits = self.store.search(q_vec, recall_k, allow=allowed)
        dense_ranked = [(h.id, h.score, rank) for rank, h in enumerate(dense_hits, start=1)]
        dense_score_by_id = {h.id: h.score for h in dense_hits}

        # ---- ④ RRF 融合（两路分数不可比，用排名倒数融合）----
        rrf: dict[str, float] = {}
        for cid, _score, rank in bm25_hits:
            rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (self.rrf_k + rank)
        for cid, _score, rank in dense_ranked:
            rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (self.rrf_k + rank)

        if not rrf:
            logger.info("双路召回均为空：query=%r", query[:60])
            return []

        max_rrf = max(rrf.values())

        # ---- ⑤ Rerank 精排 ----
        results: list[RetrievedChunk] = []
        bm25_score_by_id = {cid: s for cid, s, _ in bm25_hits}
        for cid, rrf_score in rrf.items():
            chunk = self._chunks.get(cid)
            if chunk is None:
                continue
            rerank_score = self.reranker.score(query, chunk)
            normalized_rrf = rrf_score / max_rrf if max_rrf > 0 else 0.0
            final = self.rrf_weight * normalized_rrf + (1 - self.rrf_weight) * rerank_score
            results.append(
                RetrievedChunk(
                    chunk_id=cid,
                    text=chunk.text,
                    parent_text=chunk.parent_text,
                    score=round(final, 4),
                    bm25_score=round(bm25_score_by_id.get(cid, 0.0), 4),
                    dense_score=round(dense_score_by_id.get(cid, 0.0), 4),
                    rerank_score=round(rerank_score, 4),
                    citation=chunk.citation,
                    metadata=chunk.metadata,
                )
            )

        results.sort(key=lambda r: r.score, reverse=True)

        # ---- ⑥ 相关性闸门：判"有没有政策依据"（合规要求：宁可拒答不编造）----
        if not results:
            return []

        best = results[0]
        is_relevant = (
            best.rerank_score >= self.min_rerank_score or best.dense_score >= self.min_dense_score
        )
        if not is_relevant:
            logger.info(
                "判定无政策依据：top rerank=%.4f(<%.2f) 且 dense=%.4f(<%.2f)，query=%r",
                best.rerank_score, self.min_rerank_score,
                best.dense_score, self.min_dense_score, query[:60],
            )
            return []

        # 相对截断：与最佳结果差距过大的直接丢弃（避免混入弱相关噪声）
        cutoff = best.score * self.min_score_ratio
        filtered = [r for r in results if r.score >= cutoff]

        # 同一父块只保留最高分的子块（避免上下文里塞重复条款）
        deduped: list[RetrievedChunk] = []
        seen_parents: set[str] = set()
        for r in filtered:
            parent = r.citation.policy_id
            if parent in seen_parents:
                continue
            seen_parents.add(parent)
            deduped.append(r)

        return deduped[:top_k]

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _allowed_ids(self, *, as_of: date | None, product_code: str | None) -> set[str]:
        """计算通过"时效 + 产品"过滤的 chunk 集合。"""
        allowed: set[str] = set()
        for cid, chunk in self._chunks.items():
            if product_code and chunk.citation.product_code != product_code:
                continue
            if as_of is not None:
                eff = chunk.citation.effective_date
                exp_raw = chunk.metadata.get("expire_date")
                if eff and as_of < eff:
                    continue
                if exp_raw:
                    exp = date.fromisoformat(exp_raw)
                    if as_of >= exp:
                        continue
            allowed.add(cid)
        return allowed

    def stats(self) -> dict[str, object]:
        return {
            "chunks": len(self._chunks),
            "vector_store": self.store.count(),
            "bm25": self.bm25.size(),
            "rrf_k": self.rrf_k,
            "rrf_weight": self.rrf_weight,
            "min_rerank_score": self.min_rerank_score,
            "min_dense_score": self.min_dense_score,
            "bm25_recall_multiplier": self.bm25_recall_multiplier,
        }

    # ------------------------------------------------------------------ #
    # 阈值校准（换真实 Embedding 后必须重跑）
    # ------------------------------------------------------------------ #
    def calibrate(self, labeled: list[tuple[str, bool]], *, recall_k: int = 10) -> dict[str, object]:
        """拒答闸门校准：给一组 (问题, 是否有政策依据) 标注，输出打分分布。

        返回每个问题的 (top rerank 分, top dense 分)，并给出能分开
        "有依据 / 无依据" 两类的建议阈值区间。**纯测量，不改任何状态。**

        面试要点：阈值不是拍脑袋 —— 相关问题的 rerank 与无关问题必须
        存在分离带（如 0.40 vs 0.00），阈值取分离带内靠下位置（宁可
        多答一次让引用兜底，不可把该答的拒了）。
        """
        relevant_rerank: list[float] = []
        relevant_dense: list[float] = []
        irrelevant_rerank: list[float] = []
        irrelevant_dense: list[float] = []
        detail: list[dict[str, object]] = []
        for query, is_relevant in labeled:
            chunks = self.retrieve(query, top_k=recall_k)
            top_rerank = chunks[0].rerank_score if chunks else 0.0
            top_dense = max((c.dense_score for c in chunks), default=0.0)
            detail.append(
                {
                    "query": query,
                    "relevant": is_relevant,
                    "top_rerank": round(top_rerank, 4),
                    "top_dense": round(top_dense, 4),
                    "retrieved": len(chunks),
                }
            )
            if is_relevant:
                relevant_rerank.append(top_rerank)
                relevant_dense.append(top_dense)
            else:
                irrelevant_rerank.append(top_rerank)
                irrelevant_dense.append(top_dense)

        def _suggest(rel: list[float], irr: list[float]) -> dict[str, object]:
            if not rel or not irr:
                return {"separable": False, "note": "标注集缺少正例或负例"}
            lo, hi = max(irr), min(rel)
            return {
                "separable": lo < hi,
                "irrelevant_max": round(lo, 4),
                "relevant_min": round(hi, 4),
                "suggested_threshold": round((lo + hi) / 2, 4) if lo < hi else None,
            }

        return {
            "detail": detail,
            "rerank": _suggest(relevant_rerank, irrelevant_rerank),
            "dense": _suggest(relevant_dense, irrelevant_dense),
        }
