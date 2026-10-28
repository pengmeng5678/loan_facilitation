"""父子块（Parent-Child）切分。

对应简历原文："父子块策略——检索用小块保证精准命中，喂给模型用大块保证上下文完整"。

做法：
    - **父块** = 一条完整的政策条款（业务语义完整的最小单元）
    - **子块** = 把父块按滑动窗口切成小块（如 120 字，重叠 30 字）
    检索时用子块做向量/BM25 匹配（精准），命中后把**父块完整文本**交给模型（上下文完整）。

为什么必须这样做：
    政策条款里的准入条件常常跨句（"年龄 22-60 周岁，且近 6 个月征信查询不超过 8 次"），
    如果只把切碎的小块喂给模型，模型会看到半句话，极易答错；
    反之如果整块做向量，块太长会让向量被"语义平均"掉，命中率下降。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from product_assistant.domain.models import Citation

# 子块窗口参数（按中文字符计）
CHILD_SIZE = 120
CHILD_OVERLAP = 30


@dataclass
class PolicyDocument:
    """一条政策条款（父块单位）。"""

    policy_id: str
    text: str
    product_code: str = ""
    clause_no: str = ""
    title: str = ""
    effective_date: date | None = None
    expire_date: date | None = None
    source_file: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Chunk:
    """一个子块（检索单位），携带父块与引用信息。"""

    chunk_id: str
    text: str  # 子块文本（用于检索匹配）
    parent_text: str  # 父块完整文本（用于喂模型）
    citation: Citation
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def searchable_text(self) -> str:
        """参与检索的文本：子块 + 标题 + 产品编号。

        把标题和产品编号拼进去，是为了让"产品名 / 条款号"这类
        高区分度短词也能被 BM25 精确命中（纯子块正文里可能不含产品名）。
        """
        parts = [self.text]
        if self.citation.title:
            parts.append(self.citation.title)
        if self.citation.product_code:
            parts.append(self.citation.product_code)
        if self.citation.clause_no:
            parts.append(f"第{self.citation.clause_no}条")
        return " ".join(parts)


def _stable_id(*parts: str) -> str:
    raw = "|".join(parts)
    return "ck_" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:14]


def chunk_document(doc: PolicyDocument) -> list[Chunk]:
    """把一条政策条款切成若干子块，全部指向同一个父块。"""
    text = (doc.text or "").strip()
    if not text:
        return []

    citation = Citation(
        policy_id=doc.policy_id,
        product_code=doc.product_code,
        clause_no=doc.clause_no,
        title=doc.title,
        effective_date=doc.effective_date,
        source_file=doc.source_file,
        snippet=text[:120],
    )

    chunks: list[Chunk] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(n, start + CHILD_SIZE)
        child = text[start:end]
        chunks.append(
            Chunk(
                chunk_id=_stable_id(doc.policy_id, str(start), child),
                text=child,
                parent_text=text,  # 父块 = 完整条款
                citation=citation,
                metadata={
                    **doc.metadata,
                    "product_code": doc.product_code,
                    "clause_no": doc.clause_no,
                    "effective_date": doc.effective_date.isoformat() if doc.effective_date else None,
                    "expire_date": doc.expire_date.isoformat() if doc.expire_date else None,
                    "parent_id": doc.policy_id,
                },
            )
        )
        if end >= n:
            break
        start = end - CHILD_OVERLAP  # 保留重叠，避免句子被切断在边界

    return chunks
