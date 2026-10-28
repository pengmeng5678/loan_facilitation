"""缓存层 —— 成本治理三板斧之二（也是"最危险"的一招）。

两类缓存，风险完全不同：
    **前缀缓存（Prefix Cache）** —— prompt 完全一致才命中。安全：同样的输入
        必然得到同样的输出，命中即等价于"这次调用没必要再花钱"。
        实现上以"消息序列规范化后的哈希"为键（等价于前缀完全一致）。

    **语义缓存（Semantic Cache）** —— 语义相近即命中。省钱最猛，但**有三个坑**：
        ① 相似度阈值必须"宁漏不错"：把 0.8 相似的两个问题当同一个，
           会把 A 产品的利率答成 B 产品的 —— 这在助贷场景是事故；
        ② 必须带 TTL：政策/利率会变，缓存不能永不过期；
        ③ 必须带命名空间：`task`（政策问答 / 营销文案）或 `scope`（租户/业务线）
           不同，答案口径就不同。同一句"利率是多少"在不同作用域下答案不同，
           语义缓存跨作用域命中 = 把甲租户的答案发给乙租户。
        因此本模块默认阈值偏高（0.94），要求语义缓存按命名空间隔离，
        并且**允许业务显式关闭**（政策问答这类确定性要求高的场景就该走 cache="off"）。
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from typing import Any

from pydantic import BaseModel, Field

from agent_platform.llm.base import EmbeddingProvider, Message
from agent_platform.logging_config import get_logger
from gateway.domain import CachePolicy

logger = get_logger(__name__)


def prompt_key(messages: list[Message], model: str = "") -> str:
    """把消息序列规范化成一个稳定键。

    规范化 = 拼接 role+content 后做 md5。不用 Python 内置 hash()：
    它对 str 带随机盐，跨进程会漂移（详见 llm.base.stable_hash 的说明）。
    """
    raw = "\n".join(f"{m.role}:{m.content}" for m in messages)
    return hashlib.md5(f"{model}\x00{raw}".encode()).hexdigest()  # noqa: S324 - 非安全用途


class CacheEntry(BaseModel):
    """一条缓存记录。"""

    key: str
    kind: str  # prefix | semantic
    content: str
    namespace: str = "default"
    """语义隔离维度（scope:task）。不同命名空间之间**永不互相命中**。"""
    model: str = ""
    tier: str = "medium"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    query_text: str = ""
    vector: list[float] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
    expires_at: float = 0.0
    hits: int = 0

    def alive(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return self.expires_at <= 0 or now < self.expires_at


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        # 维度不一致直接视为不相似：宁可判定"未命中"（多花一次钱），
        # 也不能让两个不同维度的向量算出个没有意义的相似度。
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


class PrefixCache:
    """精确命中缓存（prompt 完全一致）。"""

    def __init__(self, *, ttl_seconds: float = 900.0, max_entries: int = 512) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: dict[str, CacheEntry] = {}
        self._hits = 0
        self._misses = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> CacheEntry | None:
        now = time.time()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            if not entry.alive(now):
                # 过期即删：TTL 的意义就在这里（政策会变，缓存不能永不过期）
                self._entries.pop(key, None)
                self._misses += 1
                return None
            entry.hits += 1
            self._hits += 1
            return entry

    def put(self, entry: CacheEntry) -> None:
        with self._lock:
            if self.max_entries > 0 and len(self._entries) >= self.max_entries:
                # 淘汰最旧的一条（简单 LRU 近似；生产用 Redis 自带淘汰策略）
                oldest = min(self._entries.values(), key=lambda e: e.created_at)
                self._entries.pop(oldest.key, None)
            self._entries[entry.key] = entry

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._entries),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 4) if total else 0.0,
                "ttl_seconds": self.ttl_seconds,
            }


class SemanticCache:
    """语义近似缓存（需过相似度阈值）。"""

    def __init__(
        self,
        embedder: EmbeddingProvider,
        *,
        threshold: float = 0.94,
        ttl_seconds: float = 900.0,
        max_entries: int = 512,
    ) -> None:
        self.embedder = embedder
        self.threshold = threshold
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._entries: list[CacheEntry] = []
        self._hits = 0
        self._misses = 0
        self._cross_scope_blocked = 0
        self._lock = threading.Lock()

    def get(
        self, query_text: str, *, namespace: str = "default"
    ) -> tuple[CacheEntry | None, float]:
        """查语义缓存，返回 (命中的 entry, 最高相似度)。

        返回相似度是为了让调用方/排障能看到"差一点就命中"的情况 ——
        这对校准阈值非常关键。

        **只在同一命名空间内比对**：跨作用域的高相似度不算命中，
        但会计入 `cross_scope_blocked` —— 这不是"白花钱"，而是刻意的隔离：
        甲租户的问题与乙租户的答案再像，也不能拿来复用。
        """
        now = time.time()
        try:
            qvec = self.embedder.embed_query(query_text)
        except Exception as exc:  # noqa: BLE001 - 向量化失败不能让网关挂掉，退化为不命中
            logger.warning("语义缓存向量化失败，退化为未命中：%s", exc)
            with self._lock:
                self._misses += 1
            return None, 0.0

        best: CacheEntry | None = None
        best_score = 0.0
        other_best = 0.0
        with self._lock:
            alive = [e for e in self._entries if e.alive(now)]
            self._entries = alive
            for entry in alive:
                score = _cosine(qvec, entry.vector)
                if entry.namespace != namespace:
                    other_best = max(other_best, score)
                    continue
                if score > best_score:
                    best_score, best = score, entry
            if best is not None and best_score >= self.threshold:
                best.hits += 1
                self._hits += 1
                return best, best_score
            if best is None and other_best >= self.threshold:
                self._cross_scope_blocked += 1
                logger.info(
                    "语义缓存跨命名空间命中被拦截：namespace=%s similarity=%.4f",
                    namespace, other_best,
                )
            self._misses += 1
        return None, best_score

    def put(self, entry: CacheEntry) -> None:
        if not entry.vector:
            return
        with self._lock:
            if self.max_entries > 0 and len(self._entries) >= self.max_entries:
                oldest = min(self._entries, key=lambda e: e.created_at)
                self._entries.remove(oldest)
            self._entries.append(entry)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._entries),
                "namespaces": len({e.namespace for e in self._entries}),
                "hits": self._hits,
                "misses": self._misses,
                "cross_scope_blocked": self._cross_scope_blocked,
                "hit_rate": round(self._hits / total, 4) if total else 0.0,
                "threshold": self.threshold,
                "ttl_seconds": self.ttl_seconds,
            }

    def calibrate(self, pairs: list[tuple[str, str, bool]]) -> dict[str, Any]:
        """阈值校准探针：用"该不该命中"的标注对实测相似度分布。

        参数 pairs：[(缓存中的问题, 探针问题, 语义上是否应命中), ...]
            - should_match=True  → 同义改写对（期望高相似度）
            - should_match=False → 不同意图对（期望低相似度，
              **这类是校准的关键**：阈值必须压在"最像的负例"之上，
              否则就会把 A 产品的利率答成 B 产品的 —— 缓存错答案比不缓存更糟）

        返回：每对的实际得分、正负例是否可分（min(正例) > max(负例)）、
        以及可分时的建议阈值（正负例边界的中点）。
        刻意**不改任何缓存状态**：校准是纯测量，不应污染线上缓存。
        """
        rows: list[dict[str, Any]] = []
        positives: list[float] = []
        negatives: list[float] = []
        for cached_text, probe_text, should_match in pairs:
            try:
                cvec = self.embedder.embed_query(cached_text)
                pvec = self.embedder.embed_query(probe_text)
            except Exception as exc:  # noqa: BLE001 - 探针失败不能污染统计
                rows.append(
                    {"cached": cached_text, "probe": probe_text, "error": str(exc)[:120]}
                )
                continue
            score = round(_cosine(pvec, cvec), 4)
            (positives if should_match else negatives).append(score)
            rows.append(
                {
                    "cached": cached_text,
                    "probe": probe_text,
                    "should_match": should_match,
                    "score": score,
                }
            )

        separable = bool(positives) and bool(negatives) and min(positives) > max(negatives)
        suggested = round((min(positives) + max(negatives)) / 2, 4) if separable else None
        return {
            "pairs": rows,
            "positive_scores": sorted(positives, reverse=True),
            "negative_scores": sorted(negatives, reverse=True),
            "separable": separable,
            "current_threshold": self.threshold,
            "suggested_threshold": suggested,
            "note": (
                "正负例可分：建议阈值取边界中点（宁漏不错可再上浮 0.01~0.02）"
                if separable
                else "正负例不可分：当前 embedding 对这组用例没有区分度，"
                "换更强的 embedding 模型后重测，而不是硬调阈值"
            ),
        }


class CacheLayer:
    """统一缓存层：按策略分发到前缀缓存 / 语义缓存。"""

    def __init__(
        self,
        *,
        embedder: EmbeddingProvider | None = None,
        ttl_seconds: float = 900.0,
        prefix_max_entries: int = 512,
        semantic_max_entries: int = 512,
        semantic_threshold: float = 0.94,
    ) -> None:
        self.prefix = PrefixCache(ttl_seconds=ttl_seconds, max_entries=prefix_max_entries)
        self.semantic = (
            SemanticCache(
                embedder,
                threshold=semantic_threshold,
                ttl_seconds=ttl_seconds,
                max_entries=semantic_max_entries,
            )
            if embedder is not None
            else None
        )

    def lookup(
        self,
        messages: list[Message],
        *,
        model: str,
        policy: CachePolicy,
        namespace: str = "default",
    ) -> tuple[CacheEntry | None, str, float]:
        """按策略查缓存，返回 (entry, 状态, 相似度)。

        状态取值：hit_prefix | hit_semantic | miss
        `namespace` 只影响语义缓存；前缀缓存的键已包含完整 prompt 哈希，
        同样的 prompt 必然对应同样的答案，无需再按作用域切分。
        """
        if policy == "off":
            return None, "miss", 0.0
        namespace = namespace or "default"

        key = prompt_key(messages, model)
        hit = self.prefix.get(key)
        if hit is not None:
            return hit, "hit_prefix", 1.0

        if policy == "semantic" and self.semantic is not None:
            query = _last_user_text(messages)
            found, score = self.semantic.get(query, namespace=namespace)
            if found is not None:
                return found, "hit_semantic", score
            return None, "miss", score

        return None, "miss", 0.0

    def store(
        self,
        messages: list[Message],
        *,
        model: str,
        policy: CachePolicy,
        content: str,
        tier: str,
        prompt_tokens: int,
        completion_tokens: int,
        namespace: str = "default",
    ) -> None:
        if policy == "off":
            return
        namespace = namespace or "default"

        key = prompt_key(messages, model)
        now = time.time()
        self.prefix.put(
            CacheEntry(
                key=key,
                kind="prefix",
                content=content,
                namespace=namespace,
                model=model,
                tier=tier,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                created_at=now,
                expires_at=now + self.prefix.ttl_seconds,
                query_text=_last_user_text(messages),
            )
        )

        if policy == "semantic" and self.semantic is not None:
            query = _last_user_text(messages)
            try:
                vector = self.semantic.embedder.embed_query(query)
            except Exception:  # noqa: BLE001
                return
            self.semantic.put(
                CacheEntry(
                    key=key,
                    kind="semantic",
                    content=content,
                    namespace=namespace,
                    model=model,
                    tier=tier,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    query_text=query,
                    vector=vector,
                    created_at=now,
                    expires_at=now + self.semantic.ttl_seconds,
                )
            )

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {"prefix": self.prefix.stats()}
        out["semantic"] = self.semantic.stats() if self.semantic is not None else {"enabled": False}
        return out

    def clear(self) -> None:
        self.prefix._entries.clear()  # noqa: SLF001 - 同类内部清理，语义清晰
        if self.semantic is not None:
            self.semantic._entries.clear()  # noqa: SLF001


def _last_user_text(messages: list[Message]) -> str:
    """取最后一条 user 消息作为"语义查询"。

    为什么不用整段 prompt：整段包含 system 指令，噪声太大，会让语义相似度
    整体偏高（不同问题都共享同一段 system），反而降低区分度。
    """
    for m in reversed(messages):
        if m.role == "user":
            return m.content
    return messages[-1].content if messages else ""
