"""BM25 关键词检索索引。

为什么混合检索里必须有 BM25：
    向量检索把文本压成语义向量，会把"低区分度但高价值"的稀有词平滑掉 ——
    产品名、产品编号、"先息后本""公积金信用贷"这类**专有名词**经常召不回。
    BM25 的精确词项匹配正好补这一块，两者用 RRF 融合后再 Rerank 是当前标准解。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from rank_bm25 import BM25Okapi

from agent_platform.logging_config import get_logger
from product_assistant.rag.chunking import Chunk
from product_assistant.rag.tokenize import tokenize

logger = get_logger(__name__)


@dataclass
class BM25Hit:
    index: int  # 在语料中的位置
    score: float


class BM25Index:
    """基于 rank_bm25 的索引封装。

    分词器是注入点而非硬编码：默认用本仓库的字符 n-gram 实现；
    做分词策略对比 / 换 jieba 等词典分词时传自定义 tokenizer 即可，
    索引与检索代码零改动（scripts/benchmark_tokenizer.py 就是这么对比的）。
    """

    def __init__(self, tokenizer: Callable[[str], list[str]] | None = None) -> None:
        self._tokenize = tokenizer or tokenize
        self._chunk_ids: list[str] = []
        self._bm25: BM25Okapi | None = None

    def build(self, chunks: list[Chunk]) -> None:
        """用子块语料重建索引。"""
        self._chunk_ids = [c.chunk_id for c in chunks]
        if not chunks:
            self._bm25 = None
            logger.warning("BM25 语料为空，索引未构建")
            return

        corpus = [self._tokenize(c.searchable_text) for c in chunks]
        # BM25Okapi 内部会按语料统计 idf；空文档（token 为 0）会让它除零，这里做兜底
        corpus = [toks or ["__empty__"] for toks in corpus]
        self._bm25 = BM25Okapi(corpus)
        logger.info("BM25 索引构建完成：%d 个 chunk", len(chunks))

    def search(self, query: str, top_k: int) -> list[BM25Hit]:
        """返回 top_k 命中（按 BM25 分降序）。"""
        if self._bm25 is None:
            return []
        tokens = self._tokenize(query) or ["__empty__"]
        scores = self._bm25.get_scores(tokens)
        # 取分数最高的 top_k；用 argsort 保证稳定
        order = sorted(range(len(scores)), key=lambda i: -float(scores[i]))[:top_k]
        return [BM25Hit(index=i, score=float(scores[i])) for i in order if scores[i] > 0]

    def chunk_id_at(self, index: int) -> str:
        return self._chunk_ids[index]

    def size(self) -> int:
        return len(self._chunk_ids)
