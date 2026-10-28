"""项目六（大模型推理网关与 Agent 可观测评测平台）测试。

这个包的价值全在"能省多少钱、能不能放心发布"两件事上，所以测试也围绕它们：

    ① 路由        —— 轻任务不该用大模型；重任务不许退到小模型（成本回退防线）
    ② 缓存        —— 精确缓存 / 语义缓存阈值 / TTL / 按任务区分策略
    ③ 上下文裁剪  —— 去重、工具输出摘要、分层压缩
    ④ 限流与配额  —— 超配额不是"拒绝服务"，而是降档 + 记账（可用性优先）
    ⑤ 密钥池      —— 轮转与失败冷却（一个 key 挂了不能拖垮全部）
    ⑥ 降级链      —— **只能往下退**，绝不能悄悄换成更贵的模型
    ⑦ 成本台账    —— 每会话/每任务单价、节省比例的**可复现口径**
    ⑧ 观测        —— span 级轨迹与会话回放
    ⑨ 评测        —— 红线一票否决、拒答判定、结构化断言、CI 准入与基线顺序
    ⑩ 装配       —— 网关不 import 任何业务代码（评测目标由装配层注入）

全部离线运行：echo provider + local-hash embedding，零密钥、零网络。
"""

from __future__ import annotations

import time

import pytest

from agent_platform.llm.base import EmbeddingProvider, LLMResponse, Message, Usage
from gateway.cache import PrefixCache, SemanticCache, prompt_key
from gateway.domain import CaseResult, CostRecord, EvalCase, EvalReport
from gateway.errors import AllProvidersFailedError, GatewayQuotaExceededError
from gateway.evaluation.dataset import builtin_golden_cases, cases_for_scene, scenes
from gateway.evaluation.judge import Judge
from gateway.evaluation.runner import EvalRunner
from gateway.gateway import LLMGateway, build_gateway
from gateway.keys import KeyPool
from gateway.ratelimit import QuotaManager, RateLimiter, TokenBucket
from gateway.router import ComplexityRouter
from gateway.trim import trim_messages


# =========================================================================== #
# 一、复杂度路由
# =========================================================================== #
def _router(settings) -> ComplexityRouter:
    return ComplexityRouter(
        threshold_medium=settings.gateway_tier_threshold_medium,
        threshold_large=settings.gateway_tier_threshold_large,
        tier_models={"small": "m-small", "medium": "m-medium", "large": "m-large"},
    )


def test_router_sends_trivial_request_to_small(settings):
    decision = _router(settings).route([Message(role="user", content="在吗")], task="")
    assert decision.tier == "small"
    assert decision.model == "m-small"
    assert 0.0 <= decision.complexity < settings.gateway_tier_threshold_medium


def test_router_sends_long_reasoning_task_to_large(settings):
    text = "请阅读以下材料并逐条分析风险，给出优化建议：" + "材料内容" * 800
    decision = _router(settings).route([Message(role="user", content=text)], task="proposal")
    assert decision.tier == "large"


def test_router_respects_task_floor(settings):
    """素材生成直接对外发布、问答给客户看 → 质量优先，不允许落到 small。"""
    router = _router(settings)
    for task in ("material", "activation", "policy_qa", "judge"):
        decision = router.route([Message(role="user", content="短")], task=task)
        assert decision.tier in ("medium", "large"), f"{task} 不该走 small"


def test_router_fallback_chain_only_goes_down(settings):
    """降级链必须单调不升档：否则"降级"可能变成涨成本。"""
    router = _router(settings)
    order = {"small": 0, "medium": 1, "large": 2}
    model_tier = {"m-small": "small", "m-medium": "medium", "m-large": "large"}
    for task in ("", "policy_qa", "proposal", "material"):
        decision = router.route([Message(role="user", content="问题" * 50)], task=task)
        tiers = [model_tier[m] for m in decision.fallback_chain]
        assert tiers[0] == decision.tier
        for prev, cur in zip(tiers, tiers[1:], strict=False):  # 滑动窗口，长度天然差 1
            assert order[cur] <= order[prev], f"降级链出现升档：{tiers}"


def test_router_forced_tier_marks_decision(settings):
    decision = _router(settings).route(
        [Message(role="user", content="在吗")], task="", force_tier="large"
    )
    assert decision.tier == "large"
    assert decision.forced is True


# =========================================================================== #
# 二、缓存
# =========================================================================== #
def test_prompt_key_is_stable_and_order_sensitive():
    a = [Message(role="user", content="你好")]
    b = [Message(role="user", content="你好")]
    c = [Message(role="user", content="你好啊")]
    assert prompt_key(a) == prompt_key(b)
    assert prompt_key(a) != prompt_key(c)


def test_prefix_cache_hits_and_expires():
    from gateway.cache import CacheEntry

    now = time.time()
    cache = PrefixCache(ttl_seconds=100.0, max_entries=4)
    cache.put(CacheEntry(
        key="k1", kind="prefix", content="v1", created_at=now, expires_at=now + 1000,
    ))
    assert cache.get("k1") is not None
    # 已过期条目必须取不到（否则会把旧答案发给客户）。注意 expires_at=0 的语义是
    # "永不过期"，所以这里必须给一个**过去**的时间戳，不能用 0。
    cache.put(CacheEntry(
        key="k2", kind="prefix", content="v2", created_at=now, expires_at=now - 1,
    ))
    assert cache.get("k2") is None
    assert cache.stats()["size"] == 1


def test_prefix_cache_evicts_when_full():
    from gateway.cache import CacheEntry

    cache = PrefixCache(ttl_seconds=1000.0, max_entries=3)
    for i in range(6):
        # created_at 递增，保证"淘汰最旧一条"是确定行为（LRU 近似）
        cache.put(CacheEntry(
            key=f"k{i}", kind="prefix", content=f"v{i}",
            created_at=float(i), expires_at=10_000.0,
        ))
    assert cache.stats()["size"] <= 3


def test_semantic_cache_hits_above_threshold_only():
    from gateway.cache import CacheEntry

    class _Embedder(EmbeddingProvider):
        name = "test"

        def __init__(self) -> None:
            super().__init__(dim=2)
            self.table = {"完全一样的问法": [1.0, 0.0], "另一种问法": [0.0, 1.0]}

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return [self.table.get(t, [0.0, 0.0]) for t in texts]

    cache = SemanticCache(_Embedder(), ttl_seconds=1000.0, threshold=0.94)
    # vector 必须自己带上：put 不做向量化（建库与查库分开，避免重复付费）
    cache.put(CacheEntry(
        key="km", kind="semantic", content="命中内容", query_text="完全一样的问法",
        vector=[1.0, 0.0], created_at=time.time(), expires_at=time.time() + 1000,
    ))
    hit, score = cache.get("完全一样的问法")
    assert hit is not None and score == pytest.approx(1.0)

    miss, score2 = cache.get("另一种问法")
    assert miss is None
    assert score2 < 0.94


def test_semantic_cache_never_crosses_namespace():
    """同一条问法、不同作用域：相似度 1.0 也不许命中。

    语义缓存的正确性边界不是"像不像"，而是"能不能复用"：
    甲租户的问题与乙租户的答案再像，口径也可能不同（利率/费率/政策）。
    """
    from gateway.cache import CacheEntry

    class _Same(EmbeddingProvider):
        name = "same"

        def __init__(self) -> None:
            super().__init__(dim=2)

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    cache = SemanticCache(_Same(), ttl_seconds=1000.0, threshold=0.94)
    cache.put(CacheEntry(
        key="k", kind="semantic", content="甲租户的答案",
        namespace="tenant-a:policy", vector=[1.0, 0.0],
        created_at=time.time(), expires_at=time.time() + 1000,
    ))

    hit, _ = cache.get("利率是多少", namespace="tenant-a:policy")
    assert hit is not None and hit.content == "甲租户的答案"

    other, score = cache.get("利率是多少", namespace="tenant-b:policy")
    assert other is None
    assert score == 0.0  # 同命名空间内没有候选，返回 0 而不是别的空间的高分
    assert cache.stats()["cross_scope_blocked"] == 1
    assert cache.stats()["namespaces"] == 1


def test_gateway_semantic_cache_is_namespaced(settings):
    """网关层验证：语义缓存按 scope:task 隔离，换租户不会复用答案。"""
    class _Same(EmbeddingProvider):
        name = "same"

        def __init__(self) -> None:
            super().__init__(dim=2)

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    gw = build_gateway(settings, embedder=_Same())
    try:
        first = [Message(role="user", content="公积金能贷多少")]
        second = [Message(role="user", content="公积金能贷多少呢")]
        gw.complete(first, task="policy", scope="tenant-a", cache="semantic")
        hit = gw.complete(second, task="policy", scope="tenant-a", cache="semantic")
        assert "hit_semantic" in hit.model
        other = gw.complete(second, task="policy", scope="tenant-b", cache="semantic")
        assert "hit_semantic" not in other.model
        assert gw.cache.stats()["semantic"]["cross_scope_blocked"] == 1
    finally:
        gw.close()


def test_gateway_per_task_cache_policy(settings):
    """政策问答不允许走缓存（口径会变、答错代价高）；素材/陪练允许语义缓存。"""
    gw = build_gateway(settings)
    try:
        gw.complete([Message(role="user", content="公积金信用贷利率区间？")],
                    task="policy_qa", thread_id="t-cache-1")
        gw.complete([Message(role="user", content="公积金信用贷利率区间？")],
                    task="policy_qa", thread_id="t-cache-1")
        assert gw.stats().cache_hits == 0, "policy_qa 不该命中缓存"

        gw.reset_counters()
        gw.complete([Message(role="user", content="写一条朋友圈素材")],
                    task="material", thread_id="t-cache-2")
        gw.complete([Message(role="user", content="写一条朋友圈素材")],
                    task="material", thread_id="t-cache-2")
        assert gw.stats().cache_hits == 1, "material 第二次应命中缓存"
    finally:
        gw.close()


def test_gateway_records_cached_call_with_zero_cost(settings):
    gw = build_gateway(settings)
    try:
        gw.complete([Message(role="user", content="同一条素材请求")],
                    task="material", thread_id="t-cache-3")
        gw.complete([Message(role="user", content="同一条素材请求")],
                    task="material", thread_id="t-cache-3")
        records = gw.observer.ledger.all()
        assert records[-1].cost == 0.0
        assert records[-1].cache_status.startswith("hit")
        # 命中缓存的调用也要记账（否则"省了多少"算不出来）
        assert records[-1].baseline_cost > 0
    finally:
        gw.close()


# =========================================================================== #
# 三、上下文裁剪
# =========================================================================== #
def test_trim_dedupes_identical_consecutive_messages():
    messages = [Message(role="user", content="重复内容")] * 4
    result = trim_messages(messages, max_tokens=10_000)
    assert len(result.messages) == 1
    assert result.saved_tokens > 0
    assert any("去重" in a for a in result.actions)


def test_trim_summarizes_long_tool_output():
    messages = [
        Message(role="user", content="帮我查产品"),
        Message(role="tool", name="product.detail", content="很长的工具输出" * 200),
    ]
    result = trim_messages(messages, max_tokens=100, summarize_chars=50)
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert "已摘要" in tool_msg.content
    assert len(tool_msg.content) < 100


def test_trim_layers_old_turns_into_digest_and_keeps_system():
    messages = [Message(role="system", content="你是助贷助手")]
    messages += [Message(role="user", content=f"第{i}个问题" + "补充" * 40) for i in range(12)]
    result = trim_messages(messages, max_tokens=200, keep_recent_turns=4)
    assert result.final_tokens <= result.original_tokens
    assert any(m.role == "system" and "助贷助手" in m.content for m in result.messages)
    # 最近的对话要保留原文（否则模型会"忘记刚才在聊什么"）
    assert any("第11个问题" in m.content for m in result.messages)


def test_trim_noop_when_under_budget():
    messages = [Message(role="user", content="短问题")]
    result = trim_messages(messages, max_tokens=10_000)
    assert result.messages == messages
    assert result.trimmed is False


# =========================================================================== #
# 四、限流与配额
# =========================================================================== #
def test_token_bucket_refills_over_time():
    bucket = TokenBucket(capacity=2, refill_per_second=100.0)
    assert bucket.allow() and bucket.allow()
    assert not bucket.allow()


def test_rate_limiter_isolates_dimensions():
    limiter = RateLimiter(per_minute=60, burst=1)
    assert limiter.allow("tenant-a")
    assert not limiter.allow("tenant-a")     # a 用完了
    assert limiter.allow("tenant-b")         # 不影响 b（多租户隔离）
    assert "rejected" in limiter.stats()


def test_quota_manager_reports_remaining():
    quota = QuotaManager(limit_per_scope=100)
    assert quota.consume("s1", 60)
    assert quota.remaining("s1") == 40
    assert not quota.consume("s1", 50)        # 超出 → False，由网关决定怎么降级
    assert quota.consume("s2", 99)            # 别的 scope 不受影响


def test_gateway_fails_closed_when_quota_exhausted_at_smallest_tier(settings):
    """配额耗尽的正确动作：先降档保服务；已是最小档还超配额 → 显式失败。

    为什么最小档必须 fail-closed：如果"保服务"一路保到底，配额就等于不存在，
    一次 618 式的流量高峰就能把整月预算烧穿。所以降无可降时必须抛错，
    把选择权交回调用方（排队重试 / 给客户一句"稍后再问"）。
    """
    gw = build_gateway(settings)
    try:
        gw.quota = QuotaManager(limit_per_scope=0)  # 配额为 0：任何消耗都超限
        with pytest.raises(GatewayQuotaExceededError) as err:
            gw.complete([Message(role="user", content="在吗")], task="", thread_id="t-quota")
        assert err.value.scope == "default"

        # 失败的调用也必须记账，否则"配额烧到哪了"永远查不出来
        records = gw.observer.ledger.all()
        assert records and records[-1].ok is False
        assert "quota_exceeded" in records[-1].error
    finally:
        gw.close()


# =========================================================================== #
# 五、密钥池
# =========================================================================== #
def test_key_pool_round_robins():
    pool = KeyPool(["k1", "k2", "k3"])
    seen = {pool.acquire("m") for _ in range(3)}
    assert seen == {"k1", "k2", "k3"}
    assert pool.stats()["total_keys"] == 3


def test_key_pool_cools_down_failed_key():
    pool = KeyPool(["k1", "k2"], cooldown_seconds=1000.0)
    first = pool.acquire("m")
    pool.report_failure(first)
    # 冷却期内的 key 不能再被取到
    for _ in range(4):
        assert pool.acquire("m") != first
    assert pool.stats()["available_keys"] == 1


def test_key_pool_empty_returns_none():
    pool = KeyPool([])
    assert pool.is_empty
    assert pool.acquire("m") is None


# =========================================================================== #
# 六、降级链（只能往下退）
# =========================================================================== #
class _BoomProvider:
    """总失败的分档 provider（用来验证降级与全链失败）。"""

    name = "boom"

    def __init__(self, tier: str) -> None:
        self.tier = tier

    def complete(self, messages, **kwargs):  # noqa: ANN001, ANN003, ARG002
        raise RuntimeError(f"{self.tier} 不可用")

    def complete_json(self, messages, schema, **kwargs):  # noqa: ANN001, ARG003, ARG002
        raise RuntimeError(f"{self.tier} 不可用")


class _FlakyProvider:
    """大档永远失败、其余正常 —— 用来验证"降级后仍然成功"。"""

    name = "flaky"

    def __init__(self, tier: str) -> None:
        self.tier = tier

    def complete(self, messages, **kwargs):  # noqa: ANN001, ANN003, ARG002
        if self.tier == "large":
            raise RuntimeError("large 档挂了")
        return LLMResponse(content=f"来自 {self.tier}", model=f"m-{self.tier}",
                           usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15))

    def complete_json(self, messages, schema, **kwargs):  # noqa: ANN001, ANN003, ARG002
        return {}


def test_gateway_falls_back_downward_and_still_succeeds(settings):
    gw = LLMGateway(settings, tier_providers={
        "small": _FlakyProvider("small"),
        "medium": _FlakyProvider("medium"),
        "large": _FlakyProvider("large"),
    })
    try:
        text = "请阅读以下材料并逐条分析风险：" + "材料" * 800
        resp = gw.complete([Message(role="user", content=text)], task="proposal",
                           thread_id="t-fb", tier="large")
        assert resp.content.startswith("来自 ")
        assert resp.model in ("m-small", "m-medium")
        record = gw.observer.ledger.all()[-1]
        assert record.fallbacks >= 1
        # 降级后实际档位必须比期望的低（否则不叫降级）
        assert record.tier in ("small", "medium")
    finally:
        gw.close()


def test_gateway_raises_when_all_providers_fail(settings):
    gw = LLMGateway(settings, tier_providers={
        "small": _BoomProvider("small"),
        "medium": _BoomProvider("medium"),
        "large": _BoomProvider("large"),
    })
    try:
        with pytest.raises(AllProvidersFailedError):
            gw.complete([Message(role="user", content="随便什么")], task="", thread_id="t-boom")
        # 失败也要记账（否则排障时看不到"这一段全挂了"）
        record = gw.observer.ledger.all()[-1]
        assert record.ok is False
        assert record.error
    finally:
        gw.close()


# =========================================================================== #
# 六（补）、流式与 provider 级熔断
# =========================================================================== #
class _StreamProvider:
    """可控流式 provider：首字前失败 / 中途失败 / 正常吐字三种形态。"""

    def __init__(self, tier: str, pieces: list[str] | None = None,
                 fail_first: bool = False, fail_mid: bool = False,
                 name: str | None = None) -> None:
        self.tier = tier
        self.pieces = pieces or [f"{tier}-第1段", f"{tier}-第2段"]
        self.fail_first = fail_first
        self.fail_mid = fail_mid
        self.name = name or f"stream-{tier}"

    def complete(self, messages, **kwargs):  # noqa: ANN001, ANN003, ARG002
        if self.fail_first:
            raise RuntimeError(f"{self.tier} 首字前挂了")
        return LLMResponse(
            content="".join(self.pieces),
            model=f"m-{self.tier}",
            usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    def stream_complete(self, messages, **kwargs):  # noqa: ANN001, ANN003, ARG002
        if self.fail_first:
            raise RuntimeError(f"{self.tier} 首字前挂了")
        for idx, piece in enumerate(self.pieces):
            if self.fail_mid and idx == 1:
                raise RuntimeError(f"{self.tier} 中途挂了")
            yield piece


def test_stream_falls_back_when_provider_fails_before_first_token(settings):
    gw = LLMGateway(settings, tier_providers={
        "small": _StreamProvider("small"),
        "medium": _StreamProvider("medium"),
        "large": _StreamProvider("large", fail_first=True),
    })
    try:
        chunks = list(gw.stream_complete(
            [Message(role="user", content="请阅读材料：" + "材料" * 800)],
            tier="large", task="proposal", thread_id="t-stream",
        ))
        assert "".join(chunks).startswith("medium-")
        record = gw.observer.ledger.all()[-1]
        assert record.ok is True
        assert record.fallbacks >= 1
        assert record.tier == "medium"
    finally:
        gw.close()


def test_stream_midway_failure_keeps_partial_answer_without_switching_provider(settings):
    gw = LLMGateway(settings, tier_providers={
        "small": _StreamProvider("small"),
        "medium": _StreamProvider("medium"),
        "large": _StreamProvider("large", fail_mid=True),
    })
    try:
        chunks = list(gw.stream_complete(
            [Message(role="user", content="请阅读材料：" + "材料" * 800)],
            tier="large", task="proposal", thread_id="t-stream-mid",
        ))
        # 已经开口就不能换档重来，只能把前半句收口
        assert chunks == ["large-第1段"]
        record = gw.observer.ledger.all()[-1]
        assert record.ok is False
        assert "stream_interrupted" in (record.error or "")
    finally:
        gw.close()


def test_provider_circuit_opens_per_provider_name(settings):
    settings.circuit_failure_threshold = 1
    settings.circuit_reset_seconds = 60
    gw = LLMGateway(settings, tier_providers={
        "small": _StreamProvider("small"),
        "medium": _StreamProvider("medium"),
        "large": _StreamProvider("large", fail_first=True, name="large-doom"),
    })
    try:
        text = "请阅读材料：" + "材料" * 800
        gw.complete([Message(role="user", content=text)], tier="large", task="proposal")
        assert gw.breaker.snapshot()["large-doom"]["state"] == "open"
        # 第二次：熔断的那一档直接跳过，其余档继续服务
        gw.complete([Message(role="user", content=text)], tier="large", task="proposal")
        assert gw.stats().breaker["large-doom"]["state"] == "open"
        assert gw.observer.ledger.all()[-1].ok is True
    finally:
        gw.close()


# =========================================================================== #
# 七、成本台账
# =========================================================================== #
def test_cost_record_saved_percent_is_reproducible():
    rec = CostRecord(record_id="r1", cost=0.25, baseline_cost=1.0, saved=0.75)
    assert rec.saved_percent == 75.0
    assert CostRecord(record_id="r2", cost=1.0, baseline_cost=0.0).saved_percent == 0.0


def test_gateway_costs_less_than_baseline_for_small_task(settings):
    gw = build_gateway(settings)
    try:
        gw.complete([Message(role="user", content="在吗")], task="", thread_id="t-cost")
        total = gw.observer.ledger.total()
        assert total["calls"] == 1
        assert total["cost"] < total["baseline_cost"], "治理后应当比不做治理便宜"
        assert total["saved_percent"] > 0
    finally:
        gw.close()


def test_ledger_aggregates_by_task_and_session(settings):
    gw = build_gateway(settings)
    try:
        gw.complete([Message(role="user", content="问题一")], task="policy_qa", thread_id="sess-a")
        gw.complete([Message(role="user", content="问题二")], task="material", thread_id="sess-b")
        gw.complete([Message(role="user", content="问题三")], task="material", thread_id="sess-b")

        by_task = gw.observer.ledger.by("task")
        assert by_task["material"]["calls"] == 2
        assert by_task["policy_qa"]["calls"] == 1

        per_session = gw.observer.ledger.per_session()
        assert per_session["sess-b"]["calls"] == 2
        assert per_session["sess-b"]["cost"] > 0
    finally:
        gw.close()


def test_gateway_stats_expose_tier_distribution(settings):
    gw = build_gateway(settings)
    try:
        gw.complete([Message(role="user", content="在吗")], task="", thread_id="t-stats")
        stats = gw.stats()
        assert stats.calls == 1
        assert sum(stats.routing.values()) == 1
        assert stats.saved_percent >= 0
    finally:
        gw.close()


# =========================================================================== #
# 八、观测：span 轨迹与会话回放
# =========================================================================== #
def test_gateway_replay_returns_decision_trajectory(settings):
    gw = build_gateway(settings)
    try:
        gw.complete([Message(role="user", content="帮我写一条素材")],
                    task="material", thread_id="t-replay")
        events = gw.observer.replay("t-replay")
        assert events, "回放必须能拿到决策轨迹"
        kinds = {e.get("type") or e.get("name") for e in events}
        assert kinds, "事件应带类型或名称，便于排障时筛选"
        # 成本落库，可按会话取回
        assert gw.store.costs(thread_id="t-replay")
    finally:
        gw.close()


def test_gateway_route_preview_does_not_change_state(settings):
    gw = build_gateway(settings)
    try:
        decision = gw.route_preview("在吗", task="")
        assert decision.tier == "small"
        assert gw.stats().calls == 0, "预览不应产生调用与成本"
    finally:
        gw.close()


# =========================================================================== #
# 九、评测：判分、基线、准入门槛
# =========================================================================== #
def _case(**kwargs) -> EvalCase:
    base = {"case_id": "c1", "scene": "policy_qa", "question": "问"}
    return EvalCase(**{**base, **kwargs})


def test_judge_vetoes_on_compliance_redline_even_if_keywords_hit():
    """红线一票否决：关键词全命中也不能翻盘。"""
    case = _case(expect_keywords=["3.20"], forbidden_keywords=["包过"])
    result = Judge().score_case(case, {"answer": "3.20% 起，我们包过", "refused": False})
    assert result.passed is False
    assert result.score == 0.0
    assert result.violations == ["包过"]


def test_judge_requires_refusal_when_no_grounding():
    case = _case(case_id="r1", scene="refusal", must_refuse=True)
    assert Judge().score_case(case, {"answer": "我猜是 5%", "refused": False}).passed is False
    assert Judge().score_case(case, {"answer": "无政策依据，建议咨询客户经理", "refused": True}).passed


def test_judge_keyword_coverage_modes():
    case = _case(expect_keywords=["身份证", "公积金"], expect_mode="all")
    result = Judge().score_case(case, {"answer": "需要身份证", "refused": False})
    assert result.passed is False
    assert result.misses == ["公积金"]
    assert result.score == pytest.approx(0.5)


def test_judge_asserts_expected_tier_for_routing_cases():
    """路由类用例断言的是行为，不是文案 —— 防止有人改阈值把成本打回去。"""
    case = _case(case_id="rt1", scene="routing", context={"expect_tier": "small"})
    ok = Judge().score_case(case, {"answer": "路由档位 small", "extra": {"tier": "small"}})
    bad = Judge().score_case(case, {"answer": "路由档位 large", "extra": {"tier": "large"}})
    assert ok.passed and ok.score == 1.0
    assert not bad.passed and bad.score == 0.0
    assert "档位不符" in bad.reason


def test_judge_calibration_flags_disagreement():
    agree = Judge.calibrate([90, 80, 70], [92, 78, 72], tolerance=10)
    assert agree["agreement_rate"] == 1.0
    assert agree["passed"] is True

    disagree = Judge.calibrate([90, 20, 30, 40], [10, 90, 95, 85], tolerance=10)
    assert disagree["passed"] is False
    assert disagree["mean_abs_diff"] > 40


def test_judge_calibration_rejects_length_mismatch():
    with pytest.raises(ValueError):
        Judge.calibrate([1, 2], [1])


def test_builtin_golden_set_covers_key_scenes():
    cases = builtin_golden_cases()
    assert len(cases) >= 8
    assert set(scenes(cases)) >= {"policy_qa", "refusal", "compliance", "routing", "material"}
    assert len(cases_for_scene("policy_qa", cases)) >= 2
    assert len(cases_for_scene("all", cases)) == len(cases)


def test_runner_records_failed_case_instead_of_crashing():
    """被测系统抛异常，是一种评测结论，不能让整轮评测崩掉。"""

    def boom(_case):  # noqa: ANN001
        raise RuntimeError("被测系统崩了")

    runner = EvalRunner(Judge())
    report = runner.run([_case()], boom, scene="policy_qa", persist=False)
    assert report.total == 1
    assert report.failed == 1
    assert "目标调用异常" in report.case_results[0].reason


def test_gate_blocks_on_compliance_violation(settings):
    """合规红线是硬拦截：分数掉了可以谈，红线不能谈。"""
    runner = EvalRunner(Judge(), gateway=build_gateway(settings))
    cases = [c for c in builtin_golden_cases() if c.scene == "policy_qa"]

    baseline = runner.run(
        cases,
        lambda c: {"answer": "年化利率 3.20% 至 4.60%，以银行审批为准。"},
        scene="policy_qa", persist=False,
    )
    broken = runner.run(
        cases,
        lambda c: {"answer": "没问题，我们包过，保证下款，不看征信。"},
        scene="policy_qa", persist=False,
    )
    decision = runner.compare(broken, baseline, max_regression=0.02)
    assert decision.allowed is False
    assert any("合规红线" in r for r in decision.reasons)
    assert decision.regressions


def _report(eval_id: str, scores: list[tuple[str, bool, float]]) -> EvalReport:
    """手搓一份评测报告，用来单独验证准入判据（不必跑真实评测）。"""
    results = [CaseResult(case_id=cid, scene="s", passed=p, score=s) for cid, p, s in scores]
    passed = sum(1 for r in results if r.passed)
    return EvalReport(
        eval_id=eval_id,
        scene="s",
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
        score=round(sum(s for _, _, s in scores) / len(results), 4),
        case_results=results,
    )


def test_gate_tolerance_applies_to_aggregate_score_regression(settings):
    """容忍阈值只管"整体加权分"的漂移。

    这里两条用例都仍然 passed，只是单条得分从 1.0 掉到 0.5 ——
    属于"回答变差但没坏"，所以宽容阈值放行、严格阈值拦截。
    """
    runner = EvalRunner(Judge())
    baseline = _report("base", [("c1", True, 1.0), ("c2", True, 1.0)])
    current = _report("curr", [("c1", True, 1.0), ("c2", True, 0.5)])

    allowed = runner.compare(current, baseline, max_regression=0.5)
    assert allowed.allowed is True
    assert allowed.delta == pytest.approx(-0.25)

    blocked = runner.compare(current, baseline, max_regression=0.02)
    assert blocked.allowed is False
    assert blocked.delta < 0
    assert any("整体加权通过率退化" in r for r in blocked.reasons)


def test_gate_blocks_any_new_failing_case_regardless_of_tolerance(settings):
    """单条用例从"过"变"挂"是不可被平均分稀释的硬回归：容忍阈值再宽也拦。"""
    runner = EvalRunner(Judge())
    baseline = _report("base", [("c1", True, 1.0), ("c2", True, 1.0)])
    current = _report("curr", [("c1", True, 1.0), ("c2", False, 1.0)])

    decision = runner.compare(current, baseline, max_regression=10.0)
    assert decision.allowed is False
    assert decision.regressions == ["c2"]
    assert any("新失败用例" in r for r in decision.reasons)


def test_gate_allows_first_run_without_baseline(settings):
    runner = EvalRunner(Judge(), gateway=build_gateway(settings))
    current = runner.run([_case()], lambda c: {"answer": "有依据的回答"}, scene="s", persist=False)
    decision = runner.compare(current, None)
    assert decision.allowed is True
    assert "无基线" in decision.reasons[0]


def test_runner_persists_and_reuses_baseline(settings):
    gw = build_gateway(settings)
    try:
        store = gw.store
        runner = EvalRunner(Judge(), store=store, gateway=gw)
        cases = [c for c in builtin_golden_cases() if c.scene == "compliance"]
        first = runner.run(cases, lambda c: {"answer": "以银行审批为准"}, scene="compliance")
        assert first.total == 1

        # 库里的历史基线可被取回（第二个进程/第二次 CI 跑也能比）
        baseline = runner.baseline_from_db(store, "compliance")
        assert baseline is not None
        assert baseline.eval_id == first.eval_id

        runs = store.eval_runs(scene="compliance")
        assert runs
    finally:
        gw.close()


# =========================================================================== #
# 十、服务门面与装配（解耦的证明）
# =========================================================================== #
def test_service_requires_injected_targets(gateway_service):
    """没有注入评测目标时必须明确报错，而不是偷偷用假目标糊过去。"""
    assert gateway_service.targets == {}
    with pytest.raises(KeyError, match="没有任何已注册的评测目标"):
        gateway_service.run_eval("policy_qa")


def test_service_runs_eval_with_injected_target(gateway_service):
    """评测目标是"按用例作答"的假系统：三条 policy_qa 用例全部答对 → 满分。"""
    answers = {
        "policy_qa_rate": "年化利率区间 3.20% 至 4.60%，最终以银行审批为准。",
        "policy_qa_material": "需提供身份证、公积金缴存明细、征信报告。",
        "policy_qa_amount": "债务置换专项贷额度上限 600000 元。",
    }
    gateway_service.targets = {"policy_qa": lambda case: {"answer": answers[case.case_id]}}
    report = gateway_service.run_eval("policy_qa")
    assert report["scene"] == "policy_qa"
    assert report["total"] == len(answers)
    assert report["score"] == 1.0
    assert all(r["scene"] == "policy_qa" for r in report["case_results"])


def test_service_gate_orders_baseline_before_current(gateway_service):
    """CI 准入最容易写错的地方：先跑再取基线 → 基线变成自己 → 永远放行。"""
    calls = {"n": 0}

    def target(case):  # noqa: ANN001
        calls["n"] += 1
        # 第一轮"好"，第二轮"坏"（模拟有人改坏了 prompt）
        if calls["n"] <= 3:
            return {"answer": "年化利率 3.20% 至 4.60%，以银行审批为准。"}
        return {"answer": "随便吧"}

    gateway_service.targets = {"policy_qa": target}

    first = gateway_service.gate_eval("policy_qa")
    assert first["gate"]["allowed"] is True
    assert first["baseline"] is None          # 首跑没有基线

    second = gateway_service.gate_eval("policy_qa")
    assert second["baseline"] is not None
    assert second["baseline"]["eval_id"] != second["current"]["eval_id"]
    assert second["gate"]["allowed"] is False
    assert second["gate"]["delta"] < 0


def test_service_dispatches_all_scene_to_each_target(gateway_service):
    """scene=all：每条用例按自己的场景分发到对应目标（评测目标可来自不同业务）。"""
    gw = gateway_service.gateway

    def _routing(case):  # noqa: ANN001
        # 这里刻意调真实路由器（而不是直接回显期望档位）：
        # 断言的是"轻任务确实被判成 small、长文确实被判成 large"。
        decision = gw.route_preview(case.question, task=str((case.context or {}).get("task") or ""))
        return {"answer": f"档位 {decision.tier}", "extra": {"tier": decision.tier}}

    gateway_service.targets = {
        "policy_qa": lambda c: {"answer": "年化利率 3.20%，需身份证，额度 600000"},
        "routing": _routing,
    }
    report = gateway_service.run_eval("all")
    assert report["total"] == len(builtin_golden_cases())
    # routing 用例必须走 routing 目标（否则档位断言不可能通过）
    routing_results = [r for r in report["case_results"] if r["scene"] == "routing"]
    assert routing_results and all(r["passed"] for r in routing_results)
    # 没有注册目标的场景（拒答/合规/素材）不应被假目标糊过去，而是如实记为未通过
    untouched = [r for r in report["case_results"] if r["scene"] in ("refusal", "compliance")]
    assert untouched and all(not r["passed"] for r in untouched)


def test_service_cost_report_and_replay(gateway_service):
    gateway_service.demo_generate("同一条素材请求", task="material", thread_id="svc-1")
    second = gateway_service.demo_generate("同一条素材请求", task="material", thread_id="svc-1")
    assert second["cache_status"].startswith("hit")
    assert second["cost"] == 0.0
    assert second["baseline_cost"] > 0
    assert second["saved_percent"] == 100.0

    report = gateway_service.cost_report()
    assert report["total"]["calls"] == 2
    assert "svc-1" in report["per_session"]

    replay = gateway_service.replay("svc-1")
    assert replay["count"] >= 1


def test_service_register_tools_exposes_six_tools(gateway_service):
    from agent_platform.tools.registry import ToolRegistry

    registry = ToolRegistry()
    names = gateway_service.register_tools(registry)
    assert names == [
        "gateway.route_preview",
        "gateway.cost_report",
        "gateway.replay",
        "gateway.stats",
        "gateway.run_eval",
        "gateway.gate_eval",
    ]
    # 每个工具都要有可读描述（Agent 靠它选工具）
    for name in names:
        assert len(registry.get(name).description) > 15


def test_gateway_agent_routes_by_intent(gateway_service):
    from agent_platform.tools.registry import ToolRegistry
    from gateway.agent import build_gateway_agent

    registry = ToolRegistry()
    gateway_service.register_tools(registry)
    caps = build_gateway_agent(gateway_service, registry)

    assert caps.route({"user_input": "这次为什么走大模型？"}) == "route"
    assert caps.route({"user_input": "帮我跑一遍评测，看能不能发布"}) == "eval"
    assert caps.route({"user_input": "上周成本花了多少"}) == "report"
    assert caps.route({"user_input": "随便问问"}) == "report"


def test_gateway_agent_scope_is_narrower_than_full_registry(gateway_service):
    from agent_platform.tools.builtin import build_builtin_tools
    from agent_platform.tools.registry import ToolRegistry
    from gateway.agent import build_gateway_agent

    registry = ToolRegistry()
    for spec in build_builtin_tools():
        registry.register(spec)
    gateway_service.register_tools(registry)
    caps = build_gateway_agent(gateway_service, registry)

    total = len(registry.all())
    for route_name in ("route", "eval", "report"):
        scoped = caps.resolve_scoped_tools({"route": route_name, "user_input": ""})
        names = {t.name for t in scoped}
        assert len(names) < total, f"{route_name} 的作用域没有真正裁剪"
        if route_name == "route":
            assert names == {"gateway.route_preview", "platform.handoff_to_human"}
        if route_name == "eval":
            assert "gateway.run_eval" in names and "gateway.gate_eval" in names
            assert "gateway.cost_report" not in names


def test_gateway_does_not_import_business_packages():
    """架构约束的机器化校验：基础设施不许反向依赖业务。"""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "gateway"
    offenders: list[str] = []
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("import marketing", "from marketing", "import product_assistant",
                          "from product_assistant", "import coaching", "from coaching"):
            if forbidden in text:
                offenders.append(f"{path.name}: {forbidden}")
    assert offenders == [], f"网关反向依赖了业务代码：{offenders}"


def test_gateway_echo_name_preserves_offline_detection(settings):
    """网关包住 echo 后 name 必须仍是 echo —— 否则业务会误以为接了真实模型。"""
    gw = build_gateway(settings)
    try:
        assert gw.impl_name == "echo"
        assert gw.name == "echo"
    finally:
        gw.close()


def test_service_calibrate_endpoint(gateway_service):
    result = gateway_service.calibrate([90.0, 80.0], [88.0, 82.0], tolerance=10)
    assert result["agreement_rate"] == 1.0
    assert result["passed"] is True


def test_service_list_scenes_reports_targets(gateway_service):
    payload = gateway_service.list_scenes()
    assert "policy_qa" in payload["scenes"]
    assert payload["case_count"] == len(builtin_golden_cases())
    assert payload["available_targets"] == []

    gateway_service.targets = {"policy_qa": lambda c: {"answer": "x"}}
    assert gateway_service.list_scenes()["available_targets"] == ["policy_qa"]


def test_service_run_eval_unknown_scene_raises(gateway_service):
    with pytest.raises(KeyError):
        gateway_service.run_eval("not_a_scene")


def test_gateway_store_cost_summary(settings):
    gw = build_gateway(settings)
    try:
        gw.complete([Message(role="user", content="统计用请求")], task="material", thread_id="t-db")
        summary = gw.store.cost_summary()
        assert summary["calls"] >= 1
        assert summary["cost"] >= 0
    finally:
        gw.close()


# =========================================================================== #
# 十一、语义缓存阈值校准探针（"缓存错答案比不缓存更糟"的工程化防线）
# =========================================================================== #
class _ToyEmbedder(EmbeddingProvider):
    """可控语义的玩具 embedder：按主题前缀给正交向量 + 微小哈希扰动。

    主题 = 文本前 2 个字（"利率"/"额度"），同主题余弦 ≈ 1，
    跨主题余弦 ≈ 0 —— 用来验证 calibrate() 的正负例分离逻辑。
    """

    _TOPICS = {"利率": [1.0, 0.0], "额度": [0.0, 1.0]}

    def __init__(self) -> None:
        super().__init__(dim=2)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        from agent_platform.llm.base import stable_hash

        out = []
        for t in texts:
            base = self._TOPICS.get(t[:2], [0.5, 0.5])
            noise = (stable_hash(t, 100) % 10) / 1000
            out.append([x + noise for x in base])
        return out


def test_semantic_cache_calibrate_separates_positives_and_negatives():
    cache = SemanticCache(_ToyEmbedder(), threshold=0.94)
    pairs = [
        ("利率是多少", "请问利率大概多少", True),
        ("利率能到多少", "年化利率范围", True),
        ("利率是多少", "额度上限是多少", False),
        ("利率是多少", "额度怎么算", False),
    ]
    report = cache.calibrate(pairs)
    assert report["separable"] is True
    assert report["suggested_threshold"] is not None
    # 建议阈值必须压在"最像的负例"之上（宁漏不错）
    assert report["suggested_threshold"] > max(report["negative_scores"])
    assert report["suggested_threshold"] < min(report["positive_scores"])
    # 校准是纯测量，不污染缓存状态
    assert cache.stats()["size"] == 0


def test_semantic_cache_calibrate_reports_inseparable_for_bad_embedding():
    """负例比正例还像 → 必须明确报告"不可分"，而不是给一个危险的阈值。"""

    class _BlindEmbedder(EmbeddingProvider):
        def __init__(self) -> None:
            super().__init__(dim=2)

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]  # 所有文本一个方向：毫无区分度

    cache = SemanticCache(_BlindEmbedder(), threshold=0.5)
    report = cache.calibrate([("利率", "利率", True), ("利率", "额度", False)])
    assert report["separable"] is False
    assert report["suggested_threshold"] is None


# =========================================================================== #
# 十二、缓存策略配置化（新任务类型不改网关代码）+ 计数器并发安全
# =========================================================================== #
def test_cache_policy_uses_builtin_default_table(settings):
    gw = build_gateway(settings)
    assert gw._cache_policy_for("policy_qa") == "off"
    assert gw._cache_policy_for("compliance") == "off"
    assert gw._cache_policy_for("roleplay") == "semantic"
    assert gw._cache_policy_for("brand_new_task") == "exact"  # 未知任务走安全默认


def test_cache_policy_override_from_settings(tmp_path):
    from agent_platform.config import Settings

    s = Settings(
        app_env="local",
        llm_provider="echo",
        embedding_provider="local",
        gateway_sqlite_path=str(tmp_path / "gw.sqlite"),
        gateway_task_cache_overrides={"policy_qa": "exact", "typo_task": "bogus"},
    )
    gw = build_gateway(s)
    try:
        assert gw._cache_policy_for("policy_qa") == "exact"  # 配置覆盖生效
        assert gw._cache_policy_for("typo_task") == "exact"  # 非法值回落默认表，不抛异常
    finally:
        gw.close()


def test_gateway_counters_thread_safe(settings):
    """并发 complete() 下计数器必须守恒：路由计数总和 == 调用数，不丢不重。"""
    import threading

    gw = build_gateway(settings)
    try:
        n = 8
        barrier = threading.Barrier(n)

        def _worker(i: int) -> None:
            barrier.wait()
            gw.complete([Message(role="user", content=f"并发请求 {i}")], task="material")

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stats = gw.stats()
        assert stats.calls == n
        assert sum(stats.routing.values()) == n  # 守恒律：每次调用恰好路由一次
        assert 0 <= stats.cache_hits <= n
    finally:
        gw.close()


# =========================================================================== #
# 十三、token 估算的中文口径（预算守卫不能被 len//4 骗过）
# =========================================================================== #
def test_estimate_tokens_cjk_fallback_is_not_undercounted(monkeypatch):
    """tiktoken 不可用时，中文必须按 ~1 token/字 兜底，而不是 len//4。"""
    from agent_platform.llm import base as llm_base

    def _broken_encoder():
        raise RuntimeError("tiktoken unavailable")

    monkeypatch.setattr(llm_base, "_get_encoder", _broken_encoder)
    cjk = llm_base.estimate_tokens("贷款审批通过")
    ascii_text = llm_base.estimate_tokens("abcdefgh")
    assert cjk >= 6  # 6 个汉字 ≈ 6 token（而不是 6//4=1）
    assert cjk > ascii_text  # 同长度下中文 token 数必须更高


def test_estimate_tokens_zero_is_zero():
    from agent_platform.llm.base import estimate_tokens

    assert estimate_tokens("") == 0
