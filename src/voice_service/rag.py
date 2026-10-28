"""语音场景的 RAG 检索策略层。

它不实现检索算法（那是项目二的知识库干的），它负责**语音场景特有的取舍**：

    1. limit 只取 1~3 条
       文本问答给 4~6 条没事，用户会读完；语音不行 —— 知识块越长，
       首字延迟越高、模型越容易"念资料"。实测 2 条是准确率与延迟的平衡点。

    2. 相关性闸门（低于阈值就当没检索到）
       语音里"我查一下"然后念一段不相关的话，比直接说"这个我不确定"糟糕得多。
       所以宁可不答、转人工 —— 这条和项目二的政策问答口径一致。

    3. 稳定排序（按 chunk_id，而不是相似度）
       为了命中 LLM 的前缀缓存。详见 prompt.build_knowledge_block 的说明。

    4. 长度硬预算（max_chars）
       模型可能返回超长片段（父块），必须截断 —— 但要在**句子边界**截，
       截半个句子会让模型把"利率 3.2%"读成"利率 3."。

设计上把"检索能力"作为函数注入（`retrieve_fn`），所以：
    * 生产可以接火山知识库 / VikingDB / 自建向量库；
    * 离线可以用项目二的政策检索；
    * 单元测试可以塞一个假函数，断言"策略"本身（这才是这个文件该测的东西）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from agent_platform.logging_config import get_logger
from voice_service.domain import KnowledgeChunk, RetrievalResult

logger = get_logger(__name__)

# 检索函数签名：与项目二 HybridRetriever.retrieve 对齐（多传的参数用默认值兜住）
RetrieveFn = Callable[..., list[Any]]

_SENTENCE_END = "。！？；\n"


def _truncate_at_sentence(text: str, budget: int) -> str:
    """在句子边界截断，最多使用 budget 个字符。"""
    if len(text) <= budget:
        return text
    window = text[:budget]
    cut = max(window.rfind(ch) for ch in _SENTENCE_END)
    if cut <= 0:
        return window
    return window[: cut + 1]


def _as_text(obj: Any) -> str:
    """取"给模型看的正文"：优先父块（上下文完整），退化为子块。"""
    parent = getattr(obj, "parent_text", "") or ""
    if parent:
        return parent
    return getattr(obj, "text", "") or ""


def _citation_of(obj: Any) -> tuple[str, str]:
    """返回 (chunk_id, 引用文案)。"""
    chunk_id = str(getattr(obj, "chunk_id", "") or "")
    citation = getattr(obj, "citation", None)
    if citation is None:
        return chunk_id, ""
    render = getattr(citation, "render", None)
    if callable(render):
        try:
            return chunk_id, str(render())
        except Exception:  # noqa: BLE001
            return chunk_id, ""
    return chunk_id, str(citation)


class VoiceRetriever:
    """语音检索策略器。"""

    def __init__(
        self,
        retrieve_fn: RetrieveFn,
        *,
        limit: int = 2,
        max_chars: int = 900,
        min_relevance: float = 0.25,
        enabled: bool = True,
    ) -> None:
        self.retrieve_fn = retrieve_fn
        self.limit = max(0, min(int(limit), 5))
        self.max_chars = max(200, int(max_chars))
        self.min_relevance = min_relevance
        self.enabled = enabled

    # ------------------------------------------------------------------ #
    def _call(self, query: str, limit: int) -> list[Any]:
        """调用注入的检索函数。

        兼容三种签名（都真实存在）：
            retrieve_fn(query, top_k=...)
            retrieve_fn(query, top_k=..., as_of=...)
            retrieve_fn(query, top_k=..., as_of=..., product_code=...)
        用 try/except TypeError 逐级降级 —— 比 inspect 签名更简单，
        且不会因为包装函数（functools.partial / lambda）丢掉签名信息而误判。
        """
        from datetime import date

        try:
            return list(self.retrieve_fn(query, top_k=limit, as_of=date.today(), product_code=None))
        except TypeError:
            pass
        try:
            return list(self.retrieve_fn(query, top_k=limit))
        except TypeError:
            return list(self.retrieve_fn(query))

    # ------------------------------------------------------------------ #
    def retrieve(
        self,
        query: str,
        *,
        limit: int | None = None,
        tags: list[str] | None = None,
    ) -> RetrievalResult:
        """检索并压成语音可用的知识块原料。"""
        started = time.perf_counter()
        if not self.enabled or not (query or "").strip():
            return RetrievalResult(query=query, hit=False, reason="检索未启用或问题为空", elapsed_ms=0.0)

        take = self.limit if limit is None else max(0, min(int(limit), 5))
        if take == 0:
            return RetrievalResult(query=query, hit=False, reason="场景配置不检索（limit=0）", elapsed_ms=0.0)

        try:
            raw = self._call(query, take)
        except Exception as exc:  # noqa: BLE001 - 检索挂了不能连带语音服务挂
            logger.exception("语音检索失败：%s", query[:40])
            return RetrievalResult(
                query=query,
                hit=False,
                reason=f"检索异常：{type(exc).__name__}",
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )

        chunks: list[KnowledgeChunk] = []
        seen: set[str] = set()
        for obj in raw:
            chunk_id, citation = _citation_of(obj)
            if chunk_id and chunk_id in seen:
                continue
            score = float(getattr(obj, "score", 0.0) or 0.0)
            if score < self.min_relevance:
                # 低于闸门：不进 prompt。宁可少答，也不能让模型拿弱相关条款硬编。
                continue
            tags_of_chunk = (getattr(obj, "metadata", {}) or {}).get("tags") or []
            if tags and tags_of_chunk and not set(tags) & set(tags_of_chunk):
                continue
            seen.add(chunk_id)
            chunks.append(
                KnowledgeChunk(
                    chunk_id=chunk_id or f"chunk_{len(chunks)}",
                    text=_as_text(obj),
                    citation=citation,
                    score=score,
                    dense_score=float(getattr(obj, "dense_score", 0.0) or 0.0),
                    rerank_score=float(getattr(obj, "rerank_score", 0.0) or 0.0),
                )
            )

        chunks = chunks[:take]
        # 长度预算：按 chunk_id 顺序消耗，避免"顺序随分数抖动"
        budget = self.max_chars
        for chunk in sorted(chunks, key=lambda c: c.chunk_id):
            chunk.text = _truncate_at_sentence(chunk.text, max(60, budget))
            budget -= len(chunk.text)
            if budget <= 0:
                break

        elapsed_ms = (time.perf_counter() - started) * 1000
        if not chunks:
            return RetrievalResult(
                query=query,
                hit=False,
                reason=f"无相关条款（阈值 {self.min_relevance:.2f}）",
                elapsed_ms=elapsed_ms,
            )

        from voice_service.prompt import build_knowledge_block

        return RetrievalResult(
            query=query,
            chunks=chunks,
            context_block=build_knowledge_block(chunks, max_chars=self.max_chars),
            citations=[c.citation for c in chunks if c.citation],
            elapsed_ms=elapsed_ms,
            hit=True,
        )
