"""统一推理网关 —— 项目六的核心。

它实现的是 `LLMProvider` 接口，所以对业务代码来说**它就是一个普通模型**：
业务依旧是 `llm.complete(messages)`，但这一次调用背后自动获得了

    上下文裁剪 → 复杂度路由 → 缓存 → 限流 → 配额 → 密钥轮转 → 降级链 → 成本核算 → span 埋点

这正是"统一网关"的价值：**能力加在通道上，而不是散在每个业务里**。
换模型、加缓存、改路由策略，业务代码一行都不用动。

调用方可选地传入 task / thread_id / scope / tier / cache 来"告知上下文"，
不传也能跑（走安全默认值）。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from agent_platform.config import Settings, get_settings
from agent_platform.llm.base import (
    EmbeddingProvider,
    LLMProvider,
    LLMResponse,
    Message,
    Usage,
    estimate_tokens,
)
from agent_platform.llm.providers import EchoLLM, build_embedder
from agent_platform.logging_config import get_logger
from agent_platform.resilience import CircuitBreaker
from gateway.cache import CacheLayer
from gateway.domain import CachePolicy, CostRecord, GatewayStats, ModelTier, RouteDecision
from gateway.errors import (
    AllProvidersFailedError,
    GatewayQuotaExceededError,
    GatewayRateLimitedError,
)
from gateway.keys import KeyPool
from gateway.observe import GatewayObserver
from gateway.ratelimit import QuotaManager, RateLimiter
from gateway.router import ComplexityRouter
from gateway.store import GatewayStore
from gateway.trim import trim_messages

logger = get_logger(__name__)


# --------------------------------------------------------------------------- #
# 任务 → 缓存策略的默认映射
#
# 这张表是本项目最重要的"合规决策"之一：**不是所有省钱都值得**。
# 政策/利率/合规这类任务，答案错了就是事故，所以直接 cache=off（宁可花钱）；
# 而扮演、素材生成这类"语言任务"，语义近似可接受，才允许语义缓存。
#
# 可运维性：这张默认表属于网关的策略决策（基础设施知道"哪类任务危险"），
# 但新增任务类型不应要求改网关代码 —— Settings.gateway_task_cache_overrides
# 允许用环境变量按任务覆盖（见 complete() 里 _cache_policy_for）。
# --------------------------------------------------------------------------- #
_TASK_CACHE_DEFAULT: dict[str, CachePolicy] = {
    "policy_qa": "off",      # 利率/额度：绝不允许近似命中
    "match": "exact",        # 匹配是确定性计算结果，只做精确缓存
    "proposal": "exact",     # 方案书要发客户，不做近似
    "roleplay": "semantic",  # 扮演对话：语言相似可复用
    "judge": "exact",        # 评分要有可比性
    "material": "semantic",  # 素材文案：可复用
    "compliance": "off",     # 合规审核：绝不能因为缓存而漏判
    "activation": "semantic",
    "eval": "off",           # 评测必须真实调用，否则评测自己就被缓存污染了
    "default": "exact",
}

_VALID_CACHE_POLICIES: tuple[str, ...] = ("off", "exact", "semantic")


# =========================================================================== #
# 一、分档 provider
# =========================================================================== #
class _EchoTierProvider(EchoLLM):
    """离线确定性实现（按档位命名，便于在成本台账里区分档位）。"""

    def __init__(self, tier: str) -> None:
        super().__init__()
        self.name = f"echo-{tier}"
        self.tier = tier


class _OpenAITierProvider(LLMProvider):
    """OpenAI 兼容端点（每个档位一个模型名），带密钥轮转。"""

    def __init__(self, settings: Settings, tier: str, model: str, key_pool: KeyPool) -> None:
        self.settings = settings
        self.tier = tier
        self.model_name = model
        self.key_pool = key_pool
        self.name = f"openai-{tier}"
        self._clients: dict[str, Any] = {}

    def _client(self, api_key: str):
        """按密钥缓存 client：避免每次调用重建连接池（这是实打实的延迟优化）。"""
        if api_key in self._clients:
            return self._clients[api_key]
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url=self.settings.llm_base_url,
            timeout=self.settings.llm_timeout_seconds,
            max_retries=0,  # 重试统一由网关治理，避免双重重试放大成本
        )
        self._clients[api_key] = client
        return client

    def complete(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        started = time.perf_counter()
        key = self.key_pool.acquire(self.model_name) if not self.key_pool.is_empty else None
        if key is None and not self.key_pool.is_empty:
            # 全部密钥都在冷却期：明确报错，让降级链去处理，而不是拿个废密钥硬打
            raise GatewayRateLimitedError(f"密钥池全部冷却中（模型 {self.model_name}）")

        kwargs: dict[str, Any] = {
            "model": self.model_name,
            "messages": [m.to_openai() for m in messages],
            "temperature": self.settings.llm_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.settings.llm_max_tokens,
        }
        if json_schema is not None:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            if key is None:
                # 无密钥：交给 OpenAI SDK 的默认流程（本地自建 vLLM 常不需要密钥），
                # 具体客户端由 self._client() 统一构造（含超时与重试策略）。
                client = self._client("no-key")
            else:
                client = self._client(key)
            resp = client.chat.completions.create(**kwargs)
        except Exception:
            self.key_pool.report_failure(key)
            raise
        self.key_pool.report_success(key)

        choice = resp.choices[0]
        usage = resp.usage
        return LLMResponse(
            content=choice.message.content or "",
            model=resp.model or self.model_name,
            usage=Usage(
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                total_tokens=getattr(usage, "total_tokens", 0) or 0,
            ),
            finish_reason=choice.finish_reason or "stop",
            latency_ms=(time.perf_counter() - started) * 1000,
        )


def build_tier_providers(settings: Settings, key_pool: KeyPool) -> dict[str, LLMProvider]:
    """按配置构建三个档位的 provider。

    llm_provider=echo（默认）→ 三档都是离线确定性实现：
    这样**零密钥就能跑通整条网关链路**，测试也不需要 mock 网络。
    """
    tiers = ("small", "medium", "large")
    if settings.llm_provider == "openai":
        return {
            t: _OpenAITierProvider(settings, t, settings.gateway_tier_model(t), key_pool)
            for t in tiers
        }
    logger.info("网关分档 provider：echo 离线模式（无需密钥）")
    return {t: _EchoTierProvider(t) for t in tiers}


# =========================================================================== #
# 二、网关本体
# =========================================================================== #
class LLMGateway(LLMProvider):
    """统一推理网关。实现 LLMProvider，可无缝替换业务里的直连模型。"""

    name = "gateway"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        tier_providers: dict[str, LLMProvider] | None = None,
        embedder: EmbeddingProvider | None = None,
        store: GatewayStore | None = None,
        observer: GatewayObserver | None = None,
    ) -> None:
        self.settings = settings or get_settings()

        # ---- 密钥池（多密钥轮转）----
        self.key_pool = KeyPool(
            self.settings.gateway_api_key_list(),
            cooldown_seconds=self.settings.gateway_key_cooldown_seconds,
        )

        # ---- 分档 provider ----
        self.providers: dict[str, LLMProvider] = tier_providers or build_tier_providers(
            self.settings, self.key_pool
        )

        # ---- 对外暴露的 provider 名 ----
        # 关键：业务代码里有若干"是否为离线占位实现"的判断（如素材生成器、
        # 合规 LLM 精判），判断依据是 `provider.name != "echo"`。
        # 网关把底层的 echo 包起来之后，如果还叫 "gateway"，这些判断就会误判成
        # "接入了真实模型"，于是拿 echo 的输出当真实结果用。
        # 所以这里把名字透传成底层实现：echo 保持 "echo"，真实模型标注为
        # "gateway-openai"，既保住了离线判定，又能在日志里看出走的是网关。
        impl = "echo" if all(
            getattr(p, "name", "").startswith("echo") for p in self.providers.values()
        ) else "openai"
        self.impl_name = impl
        self.name = "echo" if impl == "echo" else f"gateway-{impl}"

        # ---- 成本治理组件 ----
        self.router = ComplexityRouter(
            threshold_medium=self.settings.gateway_tier_threshold_medium,
            threshold_large=self.settings.gateway_tier_threshold_large,
            tier_models={
                t: self.settings.gateway_tier_model(t) for t in ("small", "medium", "large")
            },
        )
        self.cache = CacheLayer(
            embedder=embedder if embedder is not None else self._build_embedder(),
            ttl_seconds=self.settings.gateway_cache_ttl_seconds,
            prefix_max_entries=self.settings.gateway_prefix_cache_max_entries,
            semantic_max_entries=self.settings.gateway_semantic_cache_max_entries,
            semantic_threshold=self.settings.gateway_semantic_cache_threshold,
        )
        self.limiter = RateLimiter(
            self.settings.gateway_rate_limit_per_minute, self.settings.gateway_rate_limit_burst
        )
        self.quota = QuotaManager(self.settings.gateway_quota_tokens_per_scope)

        # ---- provider 级熔断 ----
        # 按 provider.name 记账，而不是按"网关"整体：small 档挂了不该连坐 large，
        # 否则一个档位的上游故障会把整条降级链一起熔断，等于自己制造全站不可用。
        self.breaker = CircuitBreaker(
            failure_threshold=self.settings.circuit_failure_threshold,
            reset_seconds=self.settings.circuit_reset_seconds,
        )

        # ---- 观测 ----
        self.store = store if store is not None else GatewayStore(self.settings.gateway_sqlite_abs_path)
        self.observer = observer or GatewayObserver(store=self.store)

        # ---- 价格表与模型→档位索引 ----
        self.price_table = self.settings.gateway_price_table()
        self._model_tier: dict[str, str] = {
            self.settings.gateway_tier_model(t): t for t in ("small", "medium", "large")
        }

        # ---- 运行计数器 ----
        # 并发说明：complete() 可能被线程池并发调用（调度器 to_thread / FastAPI
        # threadpool），+= 在 CPython 里也不是原子的，计数必须过锁。
        self._counter_lock = threading.Lock()
        self._calls = 0
        self._cache_hits = 0
        self._routing: dict[str, int] = {}
        self._fallbacks = 0
        self._trimmed_tokens = 0

    def _build_embedder(self) -> EmbeddingProvider:
        return build_embedder(self.settings)

    # ------------------------------------------------------------------ #
    # 缓存策略解析（默认表 + 配置覆盖）
    # ------------------------------------------------------------------ #
    def _cache_policy_for(self, task: str) -> CachePolicy:
        """任务 → 缓存策略：显式配置覆盖 > 内置默认表。

        配置给了非法值时**回落默认表并告警**，而不是让一个手误把
        政策问答从 off 变成 semantic（那等于把合规开关交给运维手滑）。
        """
        override = (self.settings.gateway_task_cache_overrides or {}).get(task)
        if override is not None:
            if override in _VALID_CACHE_POLICIES:
                return override  # type: ignore[return-value]
            logger.warning(
                "任务 %s 的缓存策略覆盖值 %r 非法（合法值 %s），回落默认表",
                task, override, _VALID_CACHE_POLICIES,
            )
        return _TASK_CACHE_DEFAULT.get(task, _TASK_CACHE_DEFAULT["default"])

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def complete(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_schema: dict[str, Any] | None = None,
        task: str = "",
        thread_id: str = "-",
        run_id: str = "",
        scope: str = "",
        tier: ModelTier | None = None,
        cache: CachePolicy | None = None,
    ) -> LLMResponse:
        """走完整网关链路的文本生成。

        额外参数（都有默认值，业务可以不传）：
            task       任务标签 → 决定默认缓存策略、影响复杂度路由
            thread_id  会话 ID → 成本按会话归因、轨迹可回放
            scope      业务作用域 → 配额维度
            tier       强制档位（None = 由复杂度路由决定）
            cache      强制缓存策略（None = 按 task 的默认策略）
        """
        started = time.perf_counter()
        run_id = run_id or f"grun_{uuid.uuid4().hex[:10]}"
        policy: CachePolicy = cache if cache is not None else self._cache_policy_for(task)
        # 语义缓存的隔离维度：作用域（租户/业务线）+ 任务。
        # 同一句"利率是多少"在政策问答与营销文案里的口径不同，
        # 不做隔离就会把 A 场景的答案复用到 B 场景。
        cache_namespace = f"{scope or 'default'}:{task or 'default'}"
        with self._counter_lock:
            self._calls += 1
        self.observer.begin(run_id, thread_id)

        with self.observer.tracer.span("gateway.complete", kind="gateway", task=task) as gw_span:
            # ---- ① 上下文裁剪 ----
            with self.observer.tracer.span("trim", kind="trim") as sp:
                trim = trim_messages(
                    messages,
                    max_tokens=self.settings.gateway_max_context_tokens,
                    keep_recent_turns=self.settings.gateway_keep_recent_turns,
                    summarize_chars=self.settings.gateway_summarize_chars,
                )
                sp.attributes.update(
                    {
                        "original_tokens": trim.original_tokens,
                        "final_tokens": trim.final_tokens,
                        "saved_tokens": trim.saved_tokens,
                        "actions": trim.actions,
                    }
                )
            trimmed = trim.messages
            with self._counter_lock:
                self._trimmed_tokens += max(0, trim.saved_tokens)

            # ---- ② 复杂度路由 ----
            with self.observer.tracer.span("route", kind="route") as sp:
                decision = self.router.route(trimmed, task=task, force_tier=tier)
                sp.attributes.update(
                    {"tier": decision.tier, "model": decision.model, "complexity": decision.complexity}
                )
            with self._counter_lock:
                self._routing[decision.tier] = self._routing.get(decision.tier, 0) + 1
            self.observer.note(
                thread_id,
                {
                    "type": "route",
                    "tier": decision.tier,
                    "model": decision.model,
                    "complexity": decision.complexity,
                    "reason": decision.reason,
                    "task": task,
                },
            )

            # ---- ③ 缓存查询 ----
            with self.observer.tracer.span("cache.lookup", kind="cache") as sp:
                entry, cache_status, similarity = self.cache.lookup(
                    trimmed,
                    model=decision.model,
                    policy=policy,
                    namespace=cache_namespace,
                )
                sp.attributes.update(
                    {"status": cache_status, "policy": policy, "similarity": round(similarity, 4),
                     "namespace": cache_namespace}
                )

            if entry is not None:
                with self._counter_lock:
                    self._cache_hits += 1
                self.observer.note(
                    thread_id,
                    {
                        "type": "cache_hit",
                        "status": cache_status,
                        "similarity": round(similarity, 4),
                        "model": entry.model,
                        "tier": entry.tier,
                    },
                )
                return self._finish_from_cache(
                    entry=entry,
                    cache_status=cache_status,
                    policy=policy,
                    trimmed=trimmed,
                    decision=decision,
                    trim_saved=trim.saved_tokens,
                    run_id=run_id,
                    thread_id=thread_id,
                    scope=scope,
                    task=task,
                    started=started,
                )

            # ---- ④ 限流 ----
            with self.observer.tracer.span("ratelimit", kind="ratelimit") as sp:
                allowed = self.limiter.allow(decision.model)
                sp.attributes["allowed"] = allowed
            if not allowed:
                self.observer.note(thread_id, {"type": "rate_limited", "model": decision.model})
                self._record_cost(
                    run_id=run_id,
                    thread_id=thread_id,
                    scope=scope,
                    task=task,
                    tier=decision.tier,
                    model=decision.model,
                    prompt_tokens=0,
                    completion_tokens=0,
                    cache_status="miss",
                    policy=policy,
                    trim_saved=trim.saved_tokens,
                    fallbacks=0,
                    ok=False,
                    error=f"rate_limited:{decision.model}",
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                raise GatewayRateLimitedError(decision.model)

            # ---- ⑤ 配额（超限 → 降级到小模型，而不是拒绝服务）----
            est_tokens = estimate_tokens("\n".join(m.content for m in trimmed))
            quota_scope = scope or task or "default"
            with self.observer.tracer.span("quota", kind="ratelimit") as sp:
                if self.quota.consume(quota_scope, est_tokens):
                    sp.attributes["quota_ok"] = True
                else:
                    # 设计取舍：配额耗尽的正确姿势是"先降级保服务"，而不是立刻拒绝——
                    # 业务断了损失更大。只有降无可降（已经在最小档）时才 fail-closed，
                    # 抛 GatewayQuotaExceededError 让调用方显式决定怎么处理。
                    sp.attributes["quota_ok"] = False
                    sp.attributes["degrade_to"] = "small"
                    logger.warning("作用域 %s 配额耗尽，降级到 small 档", quota_scope)
                    self.observer.note(
                        thread_id, {"type": "quota_degraded", "scope": quota_scope, "from": decision.tier}
                    )
                    if decision.tier != "small":
                        decision = self.router.route(trimmed, task=task, force_tier="small")
                        with self._counter_lock:
                            self._routing["small"] = self._routing.get("small", 0) + 1
                    else:
                        # 已经是最小档，无档可降 —— 再"保服务"就等于无上限烧钱。
                        # 此时必须显式失败（fail-closed），把选择权交回调用方：
                        # 是排队重试、还是给客户一句明确的"稍后再问"。
                        self.observer.note(
                            thread_id, {"type": "quota_rejected", "scope": quota_scope}
                        )
                        self._record_cost(
                            run_id=run_id,
                            thread_id=thread_id,
                            scope=scope,
                            task=task,
                            tier=decision.tier,
                            model=decision.model,
                            prompt_tokens=est_tokens,
                            completion_tokens=0,
                            cache_status="miss",
                            policy=policy,
                            trim_saved=trim.saved_tokens,
                            fallbacks=0,
                            ok=False,
                            error=f"quota_exceeded:{quota_scope}",
                            latency_ms=(time.perf_counter() - started) * 1000,
                        )
                        raise GatewayQuotaExceededError(quota_scope, self.quota.remaining(quota_scope))

            # ---- ⑥ 调用（带降级链）----
            resp, used_model, tier_used, fallbacks, errors = self._call_with_fallback(
                decision=decision,
                trimmed=trimmed,
                temperature=temperature,
                max_tokens=max_tokens,
                json_schema=json_schema,
                thread_id=thread_id,
            )
            if resp is None:
                self._record_cost(
                    run_id=run_id,
                    thread_id=thread_id,
                    scope=scope,
                    task=task,
                    tier=decision.tier,
                    model=decision.model,
                    prompt_tokens=est_tokens,
                    completion_tokens=0,
                    cache_status="miss",
                    policy=policy,
                    trim_saved=trim.saved_tokens,
                    fallbacks=fallbacks,
                    ok=False,
                    error="; ".join(errors)[:300],
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                raise AllProvidersFailedError(decision.fallback_chain, errors)

            with self._counter_lock:
                self._fallbacks += fallbacks
            gw_span.attributes.update(
                {"tier": tier_used, "model": used_model, "fallbacks": fallbacks}
            )

            # ---- ⑦ 写缓存 ----
            with self.observer.tracer.span("cache.store", kind="cache") as sp:
                self.cache.store(
                    trimmed,
                    model=used_model,
                    policy=policy,
                    content=resp.content,
                    tier=tier_used,
                    prompt_tokens=resp.usage.prompt_tokens,
                    completion_tokens=resp.usage.completion_tokens,
                    namespace=cache_namespace,
                )
                sp.attributes["policy"] = policy
                sp.attributes["namespace"] = cache_namespace

            # ---- ⑧ 成本核算 ----
            self._record_cost(
                run_id=run_id,
                thread_id=thread_id,
                scope=scope,
                task=task,
                tier=tier_used,
                model=used_model,
                prompt_tokens=resp.usage.prompt_tokens,
                completion_tokens=resp.usage.completion_tokens,
                cache_status="miss",
                policy=policy,
                trim_saved=trim.saved_tokens,
                fallbacks=fallbacks,
                ok=True,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
            resp.latency_ms = (time.perf_counter() - started) * 1000
            return resp

    # ------------------------------------------------------------------ #
    # 流式入口
    # ------------------------------------------------------------------ #
    def stream_complete(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        task: str = "",
        thread_id: str = "-",
        scope: str = "",
        tier: ModelTier | None = None,
        cache: CachePolicy | None = None,
    ) -> Iterator[str]:
        """流式生成：**首个 token 之前**允许降级，开口之后锁定 provider。

        与 complete() 的三点差异（都是流式特有的取舍）：
          1. 不走缓存：半截答案命中没有意义，缓存还会掩盖首字延迟；
          2. 降级窗口只到首个 delta：已经念出去的半句话不能换人重来，
             否则客户会听到两个模型拼接出的答案；
          3. 中途失败不换档、不重试：把已生成部分收口返回，同时记失败成本
             并打日志，避免一条 SSE 直接 500。

        熔断与 complete() 共用同一个 breaker（按 provider.name 记账）。
        """
        started = time.perf_counter()
        run_id = f"grun_{uuid.uuid4().hex[:10]}"
        policy: CachePolicy = cache if cache is not None else self._cache_policy_for(task)
        with self._counter_lock:
            self._calls += 1
        self.observer.begin(run_id, thread_id)

        with self.observer.tracer.span("gateway.stream", kind="gateway", task=task) as gw_span:
            # ---- ① 上下文裁剪 ----
            with self.observer.tracer.span("trim", kind="trim") as sp:
                trim = trim_messages(
                    messages,
                    max_tokens=self.settings.gateway_max_context_tokens,
                    keep_recent_turns=self.settings.gateway_keep_recent_turns,
                    summarize_chars=self.settings.gateway_summarize_chars,
                )
                sp.attributes.update(
                    {
                        "original_tokens": trim.original_tokens,
                        "final_tokens": trim.final_tokens,
                        "saved_tokens": trim.saved_tokens,
                        "actions": trim.actions,
                    }
                )
            trimmed = trim.messages
            with self._counter_lock:
                self._trimmed_tokens += max(0, trim.saved_tokens)

            # ---- ② 复杂度路由 ----
            with self.observer.tracer.span("route", kind="route") as sp:
                decision = self.router.route(trimmed, task=task, force_tier=tier)
                sp.attributes.update(
                    {"tier": decision.tier, "model": decision.model, "complexity": decision.complexity}
                )
            with self._counter_lock:
                self._routing[decision.tier] = self._routing.get(decision.tier, 0) + 1
            self.observer.note(
                thread_id,
                {
                    "type": "route",
                    "tier": decision.tier,
                    "model": decision.model,
                    "complexity": decision.complexity,
                    "reason": decision.reason,
                    "task": task,
                    "stream": True,
                },
            )

            # ---- ③ 限流（流式必须限，否则一条长连接就是一次免费长生成）----
            with self.observer.tracer.span("ratelimit", kind="ratelimit") as sp:
                allowed = self.limiter.allow(decision.model)
                sp.attributes["allowed"] = allowed
            if not allowed:
                self.observer.note(thread_id, {"type": "rate_limited", "model": decision.model})
                self._record_cost(
                    run_id=run_id,
                    thread_id=thread_id,
                    scope=scope,
                    task=task,
                    tier=decision.tier,
                    model=decision.model,
                    prompt_tokens=0,
                    completion_tokens=0,
                    cache_status="miss",
                    policy=policy,
                    trim_saved=trim.saved_tokens,
                    fallbacks=0,
                    ok=False,
                    error=f"rate_limited:{decision.model}",
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                raise GatewayRateLimitedError(decision.model)

            est_prompt = estimate_tokens("\n".join(m.content for m in trimmed))
            errors: list[str] = []
            emitted: list[str] = []
            used_model = ""
            tier_used = decision.tier
            fallbacks = 0
            ok = False
            error: str | None = None

            try:
                # ---- ④ 首字前降级：失败可以换人，吐字后不再换 ----
                for idx, model_name in enumerate(decision.fallback_chain):
                    candidate_tier = self._model_tier.get(model_name, decision.tier)
                    provider = self.providers.get(candidate_tier)
                    if provider is None:
                        errors.append(f"{model_name}: 无对应 provider")
                        continue
                    if not self.breaker.allow(provider.name):
                        errors.append(f"{model_name}: CircuitOpen({provider.name})")
                        continue
                    if idx > 0:
                        fallbacks += 1
                        self.observer.note(
                            thread_id, {"type": "fallback", "from": decision.model, "to": model_name}
                        )
                        logger.warning("流式降级（首字前）：%s → %s", decision.model, model_name)

                    stream = provider.stream_complete(
                        trimmed, temperature=temperature, max_tokens=max_tokens
                    )
                    try:
                        first = next(stream)
                    except StopIteration:
                        # 模型一个字都没吐但也没报错：视为成功（空回答），不降级
                        self.breaker.record_success(provider.name)
                        used_model, tier_used, ok = model_name, candidate_tier, True
                        break
                    except Exception as exc:  # noqa: BLE001 - 首字前失败可以换下一档
                        self.breaker.record_failure(provider.name)
                        errors.append(f"{model_name}: {type(exc).__name__}: {exc}")
                        continue

                    self.breaker.record_success(provider.name)
                    used_model, tier_used, ok = model_name, candidate_tier, True
                    if first:
                        emitted.append(first)
                        yield first
                    try:
                        for chunk in stream:
                            if chunk:
                                emitted.append(chunk)
                                yield chunk
                    except Exception as exc:  # noqa: BLE001 - 已开口，只能收口
                        ok = False
                        error = f"stream_interrupted:{type(exc).__name__}: {exc}"
                        self.breaker.record_failure(provider.name)
                        logger.error(
                            "流式中断（已输出 %d 字，不再换档重试）：%s",
                            len("".join(emitted)),
                            exc,
                        )
                    break
                else:
                    error = "; ".join(errors)[:300]
                    raise AllProvidersFailedError(decision.fallback_chain, errors)
            finally:
                gw_span.attributes.update(
                    {
                        "tier": tier_used,
                        "model": used_model,
                        "fallbacks": fallbacks,
                        "stream": True,
                        "emitted_chars": len("".join(emitted)),
                    }
                )
                self._record_cost(
                    run_id=run_id,
                    thread_id=thread_id,
                    scope=scope,
                    task=task,
                    tier=tier_used,
                    model=used_model or decision.model,
                    prompt_tokens=est_prompt,
                    completion_tokens=estimate_tokens("".join(emitted)),
                    cache_status="miss",
                    policy=policy,
                    trim_saved=trim.saved_tokens,
                    fallbacks=fallbacks,
                    ok=ok,
                    error=error,
                    latency_ms=(time.perf_counter() - started) * 1000,
                )
                if fallbacks:
                    with self._counter_lock:
                        self._fallbacks += fallbacks

    # ------------------------------------------------------------------ #
    # 降级链
    # ------------------------------------------------------------------ #
    def _call_with_fallback(
        self,
        *,
        decision: RouteDecision,
        trimmed: list[Message],
        temperature: float | None,
        max_tokens: int | None,
        json_schema: dict[str, Any] | None,
        thread_id: str,
    ) -> tuple[LLMResponse | None, str, str, int, list[str]]:
        """按降级链依次尝试，返回 (响应, 实际模型, 实际档位, 降级次数, 错误列表)。"""
        errors: list[str] = []
        fallbacks = 0
        for idx, model_name in enumerate(decision.fallback_chain):
            tier = self._model_tier.get(model_name, decision.tier)
            provider = self.providers.get(tier)
            if provider is None:
                errors.append(f"{model_name}: 无对应 provider")
                continue
            if not self.breaker.allow(provider.name):
                # provider 级熔断：直接跳过这一档，成本是 0（没有真的发请求），
                # 但必须记进 errors —— 否则"全部失败"时调用方看不到真实原因。
                errors.append(f"{model_name}: CircuitOpen({provider.name})")
                continue
            if idx > 0:
                fallbacks += 1
                self.observer.note(
                    thread_id, {"type": "fallback", "from": decision.model, "to": model_name}
                )
                logger.warning("第 %d 次降级：%s → %s", idx, decision.model, model_name)
            with self.observer.tracer.span("llm", kind="llm", model=model_name, tier=tier) as sp:
                try:
                    resp = provider.complete(
                        trimmed,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        json_schema=json_schema,
                    )
                    sp.attributes["content_chars"] = len(resp.content)
                    sp.attributes["prompt_tokens"] = resp.usage.prompt_tokens
                    sp.attributes["completion_tokens"] = resp.usage.completion_tokens
                    self.breaker.record_success(provider.name)
                    return resp, model_name, tier, fallbacks, errors
                except Exception as exc:  # noqa: BLE001 - 单档失败要能继续降级
                    sp.status = "error"
                    sp.error = f"{type(exc).__name__}: {exc}"
                    self.breaker.record_failure(provider.name)
                    errors.append(f"{model_name}: {type(exc).__name__}: {exc}")
                    continue
        return None, "", "", fallbacks, errors

    # ------------------------------------------------------------------ #
    # 成本核算
    # ------------------------------------------------------------------ #
    def _cost(self, tier: str, prompt_tokens: int, completion_tokens: int) -> float:
        pin, pout = self.price_table.get(tier, self.price_table["medium"])
        return round(prompt_tokens / 1000 * pin + completion_tokens / 1000 * pout, 8)

    def _baseline_cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        """对照成本：假设不做任何治理（全部走基线大模型）。"""
        return self._cost(
            self.settings.gateway_baseline_tier, prompt_tokens, completion_tokens
        )

    def _record_cost(
        self,
        *,
        run_id: str,
        thread_id: str,
        scope: str,
        task: str,
        tier: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cache_status: str,
        policy: CachePolicy,
        trim_saved: int,
        fallbacks: int,
        ok: bool,
        latency_ms: float,
        error: str | None = None,
        force_cost: float | None = None,
    ) -> CostRecord:
        total = prompt_tokens + completion_tokens
        actual = self._cost(tier, prompt_tokens, completion_tokens) if force_cost is None else force_cost
        baseline = self._baseline_cost(prompt_tokens, completion_tokens)
        rec = CostRecord(
            record_id=f"cost_{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            thread_id=thread_id,
            scope=scope,
            task=task,
            tier=tier,  # type: ignore[arg-type]
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total,
            cost=round(actual, 8),
            baseline_cost=round(baseline, 8),
            saved=round(max(0.0, baseline - actual), 8),
            cache_status=cache_status,
            cache_policy=policy,
            trimmed_tokens=trim_saved,
            fallbacks=fallbacks,
            latency_ms=round(latency_ms, 2),
            ok=ok,
            error=error,
        )
        self.observer.record_cost(rec)
        return rec

    def _finish_from_cache(
        self,
        *,
        entry: Any,
        cache_status: str,
        policy: CachePolicy,
        trimmed: list[Message],
        decision: RouteDecision,
        trim_saved: int,
        run_id: str,
        thread_id: str,
        scope: str,
        task: str,
        started: float,
    ) -> LLMResponse:
        """缓存命中：成本记 0，但对照成本照记 —— 这样"saved"才是真实的节省额。"""
        prompt_tokens = estimate_tokens("\n".join(m.content for m in trimmed))
        completion_tokens = estimate_tokens(entry.content)
        self._record_cost(
            run_id=run_id,
            thread_id=thread_id,
            scope=scope,
            task=task,
            tier=entry.tier or decision.tier,
            model=entry.model or decision.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_status=cache_status,
            policy=policy,
            trim_saved=trim_saved,
            fallbacks=0,
            ok=True,
            latency_ms=(time.perf_counter() - started) * 1000,
            force_cost=0.0,
        )
        return LLMResponse(
            content=entry.content,
            model=f"{entry.model or decision.model}（{cache_status}）",
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            finish_reason="stop",
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    # ------------------------------------------------------------------ #
    # 对外查询
    # ------------------------------------------------------------------ #
    def route_preview(self, text: str, *, task: str = "") -> RouteDecision:
        """路由预览：不改任何状态，只告诉你会走哪个档位、为什么。

        这个接口在排障时非常有用 —— "为什么这个请求突然变贵了"，
        一看路由就明白。
        """
        return self.router.route([Message(role="user", content=text)], task=task)

    def stats(self) -> GatewayStats:
        total = self.observer.ledger.total()
        cache_stats = self.cache.stats()
        with self._counter_lock:
            calls = self._calls
            cache_hits = self._cache_hits
            routing = dict(self._routing)
            fallbacks = self._fallbacks
            trimmed_tokens = self._trimmed_tokens
        return GatewayStats(
            enabled=True,
            calls=calls,
            cache_hits=cache_hits,
            cache_hit_rate=round(cache_hits / calls, 4) if calls else 0.0,
            routing=routing,
            total_cost=total["cost"],
            baseline_cost=total["baseline_cost"],
            saved=total["saved"],
            saved_percent=total["saved_percent"],
            fallbacks=fallbacks,
            trimmed_tokens=trimmed_tokens,
            cache=cache_stats,
            ratelimit=self.limiter.stats(),
            keys=self.key_pool.stats(),
            breaker=self.breaker.snapshot(),
        )

    def reset_counters(self) -> None:
        """重置运行计数器（演示脚本用；不影响持久化的成本台账）。"""
        with self._counter_lock:
            self._calls = 0
            self._cache_hits = 0
            self._routing.clear()
            self._fallbacks = 0
            self._trimmed_tokens = 0
        self.observer.ledger.clear()
        self.observer.tracer.clear()
        self.breaker.reset()

    def close(self) -> None:
        self.store.close()


def build_gateway(settings: Settings | None = None, **kwargs: Any) -> LLMGateway:
    """工厂函数（与 build_llm / build_embedder 风格一致）。"""
    return LLMGateway(settings, **kwargs)
