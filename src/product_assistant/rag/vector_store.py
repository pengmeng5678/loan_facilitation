"""向量存储抽象与实现。

两种后端：
    LocalVectorStore  —— 进程内 numpy 矩阵，零依赖，用于本地/测试/小规模
    QdrantVectorStore —— 生产推荐，支持 HNSW、Int8 量化、元数据过滤、水平扩展

接口保持一致，切换只改配置（VECTOR_BACKEND）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from agent_platform.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class VectorHit:
    """一条向量检索命中。"""

    id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


class VectorStore(Protocol):
    """向量库接口。"""

    def upsert(self, ids: list[str], vectors: list[list[float]], payloads: list[dict]) -> None: ...

    def search(
        self, vector: list[float], top_k: int, *, allow: set[str] | None = None
    ) -> list[VectorHit]: ...

    def count(self) -> int: ...

    def clear(self) -> None: ...


class LocalVectorStore:
    """进程内向量库（numpy 矩阵 + 余弦相似度）。

    适用边界：几万级以下向量。超过这个量级请切 Qdrant —— 那时
    暴力检索的延迟会先于成本成为瓶颈（这也是向量库选型的判断依据）。
    """

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self._ids: list[str] = []
        self._payloads: list[dict[str, Any]] = []
        self._matrix: np.ndarray | None = None  # shape = (n, dim)
        self._index: dict[str, int] = {}

    def upsert(self, ids: list[str], vectors: list[list[float]], payloads: list[dict]) -> None:
        if not ids:
            return
        mat = np.asarray(vectors, dtype=np.float32)
        if mat.shape[1] != self.dim:
            raise ValueError(f"向量维度不匹配：期望 {self.dim}，实际 {mat.shape[1]}")

        for i, _id in enumerate(ids):
            if _id in self._index:
                # 覆盖写：政策更新当日入库会走到这里
                pos = self._index[_id]
                self._payloads[pos] = payloads[i]
                assert self._matrix is not None
                self._matrix[pos] = mat[i]
            else:
                self._index[_id] = len(self._ids)
                self._ids.append(_id)
                self._payloads.append(payloads[i])
                self._matrix = mat[i : i + 1] if self._matrix is None else np.vstack([self._matrix, mat[i : i + 1]])

    def search(
        self, vector: list[float], top_k: int, *, allow: set[str] | None = None
    ) -> list[VectorHit]:
        if self._matrix is None or len(self._ids) == 0:
            return []

        q = np.asarray(vector, dtype=np.float32)
        # 向量已 L2 归一化 → 点积即余弦相似度
        scores = self._matrix @ q

        if allow is not None:
            # 元数据预过滤：只对允许的 chunk_id 排序（对应"生效日期过滤"等场景）
            allowed_positions = [self._index[i] for i in allow if i in self._index]
            if not allowed_positions:
                return []
            order = sorted(allowed_positions, key=lambda p: -scores[p])[:top_k]
        else:
            order = np.argsort(-scores)[:top_k].tolist()

        return [
            VectorHit(id=self._ids[p], score=float(scores[p]), payload=self._payloads[p])
            for p in order
        ]

    def count(self) -> int:
        return len(self._ids)

    def clear(self) -> None:
        self._ids.clear()
        self._payloads.clear()
        self._index.clear()
        self._matrix = None


class QdrantVectorStore:
    """Qdrant 向量库适配（生产推荐）。

    默认使用本地嵌入式模式（path=None 时用内存模式），
    配置 QDRANT_URL 后即连服务端。
    """

    def __init__(self, dim: int, url: str = "", api_key: str = "", collection: str = "zhudai_policies") -> None:
        from qdrant_client import QdrantClient

        self.dim = dim
        self.collection = collection
        if url:
            self._client = QdrantClient(url=url, api_key=api_key or None)
            logger.info("Qdrant：连接服务端 %s", url)
        else:
            self._client = QdrantClient(":memory:")
            logger.info("Qdrant：使用内存模式")
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        from qdrant_client import models as qmodels

        existing = {c.name for c in self._client.get_collections().collections}
        if self.collection not in existing:
            # Int8 量化 + HNSW：对应简历里的"Int8 量化 / HNSW 混合检索"
            self._client.create_collection(
                collection_name=self.collection,
                vectors_config=qmodels.VectorParams(
                    size=self.dim, distance=qmodels.Distance.COSINE
                ),
                hnsw_config=qmodels.HnswConfigDiff(m=16, ef_construct=128),
                quantization_config=qmodels.ScalarQuantization(
                    scalar=qmodels.ScalarQuantizationConfig(
                        type=qmodels.ScalarType.INT8, always_ram=True
                    )
                ),
            )

    def upsert(self, ids: list[str], vectors: list[list[float]], payloads: list[dict]) -> None:
        from qdrant_client import models as qmodels

        points = [
            qmodels.PointStruct(id=_to_point_id(_id), vector=vec, payload=payload)
            for _id, vec, payload in zip(ids, vectors, payloads, strict=True)
        ]
        self._client.upsert(collection_name=self.collection, points=points, wait=True)

    def search(
        self, vector: list[float], top_k: int, *, allow: set[str] | None = None
    ) -> list[VectorHit]:
        from qdrant_client import models as qmodels

        query_filter = None
        if allow is not None:
            query_filter = qmodels.Filter(
                must=[qmodels.FieldCondition(key="chunk_id", match=qmodels.MatchAny(any=sorted(allow)))]
            )
        hits = self._client.search(
            collection_name=self.collection,
            query_vector=vector,
            limit=top_k,
            query_filter=query_filter,
            with_payload=True,
        )
        return [
            VectorHit(id=str(h.payload.get("chunk_id", h.id)), score=float(h.score), payload=h.payload or {})
            for h in hits
        ]

    def count(self) -> int:
        return int(self._client.count(self.collection).count)

    def clear(self) -> None:
        self._client.delete_collection(self.collection)
        self._ensure_collection()


def _to_point_id(chunk_id: str) -> int:
    """Qdrant 的 point id 需要 int 或 UUID，这里用稳定哈希转成 int。"""
    from agent_platform.llm.base import stable_hash

    return stable_hash(chunk_id, 2**62)


def build_vector_store(settings) -> VectorStore:
    """按配置构建向量库。"""
    if settings.vector_backend == "qdrant":
        return QdrantVectorStore(
            dim=settings.embedding_dim, url=settings.qdrant_url, api_key=settings.qdrant_api_key
        )
    return LocalVectorStore(dim=settings.embedding_dim)
