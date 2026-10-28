"""第二级：向量路由（每个意图取标注句质心）。

适用场景：说法很多但语义集中（"能贷多少/额度/最多批多少"）。
门槛不是拍脑袋：`cosine >= 0.78` **且**与次优差距 `>= 0.06` 才采纳，
差一点就交给下一级 —— 因为向量路由最危险的失败模式是"两个意图都像"
时被强行二选一，差距门槛就是防这个。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from agent_platform.llm.base import EmbeddingProvider
from agent_platform.routing.base import RouteResult


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _centroid(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    sums = [0.0] * dim
    for vec in vectors:
        for i, value in enumerate(vec[:dim]):
            sums[i] += value
    return [value / len(vectors) for value in sums]


@dataclass
class EmbeddingRouter:
    """基于标注句质心的意图路由。"""

    embedder: EmbeddingProvider
    exemplars: dict[str, tuple[str, ...]]
    threshold: float = 0.78
    margin: float = 0.06
    source: str = "embedding"
    _centroids: dict[str, list[float]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._centroids = self._build_centroids()

    def _build_centroids(self) -> dict[str, list[float]]:
        centroids: dict[str, list[float]] = {}
        for intent, sentences in self.exemplars.items():
            if not sentences:
                continue
            vectors = self.embedder.embed_documents(list(sentences))
            centroids[intent] = _centroid(vectors)
        return centroids

    def scores(self, text: str, *, allowed: set[str]) -> list[tuple[str, float]]:
        if not text:
            return []
        qvec = self.embedder.embed_query(text)
        scored = [
            (intent, _cosine(qvec, centroid))
            for intent, centroid in self._centroids.items()
            if intent in allowed and centroid
        ]
        return sorted(scored, key=lambda kv: -kv[1])

    def route(self, text: str, *, allowed: set[str]) -> RouteResult:
        ranked = self.scores(text, allowed=allowed)
        if not ranked:
            return RouteResult(source=self.source)

        top_intent, top_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        decisive = top_score >= self.threshold and (top_score - runner_up) >= self.margin
        if decisive:
            return RouteResult(intents=((top_intent, round(top_score, 4)),), source=self.source)
        # 不采纳，但把打分留在结果里供观测/排障（source 标记为低置信）
        return RouteResult(
            intents=tuple((name, round(score, 4)) for name, score in ranked[:3]),
            source=f"{self.source}_low",
        )
