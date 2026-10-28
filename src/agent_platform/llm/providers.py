"""LLM / Embedding 的具体实现与工厂。

包含四个实现：
    EchoLLM             离线确定性文本生成（默认，无需密钥，测试友好）
    OpenAICompatLLM     OpenAI 兼容端点（DashScope / DeepSeek / Ollama / vLLM）
    LocalHashEmbedding  离线确定性向量化（n-gram 哈希 + L2 归一化）
    OpenAICompatEmbedding OpenAI 兼容 /embeddings 端点

以及两个工厂：
    build_llm(settings)      按配置返回 LLMProvider
    build_embedder(settings) 按配置返回 EmbeddingProvider
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Iterator
from typing import Any

from agent_platform.config import Settings, get_settings
from agent_platform.errors import LLMFatalError, LLMTransientError
from agent_platform.llm.base import (
    EmbeddingProvider,
    LLMProvider,
    LLMResponse,
    Message,
    Usage,
    estimate_tokens,
    stable_hash,
)
from agent_platform.logging_config import get_logger

logger = get_logger(__name__)


# =========================================================================== #
# 一、离线确定性文本生成
# =========================================================================== #
class EchoLLM(LLMProvider):
    """离线确定性 LLM —— 不联网、不花钱、结果可复现。

    它的价值不是"假装成模型"，而是让平台在**没有任何密钥**的情况下：
      - 跑通完整编排链路（路由 → 规划 → 工具调用 → 评审 → 应答）；
      - 单元测试可以断言确定的输出；
      - 新人 clone 下来 5 分钟就能看到系统在动。

    它的行为规矩（很重要，业务侧要按这个约定写代码）：
      - 传 json_schema 时：按 schema 生成一份"结构正确"的默认对象；
      - 不传时：返回带上下文的简短文本。
    因此**关键业务判据（准入筛选、评分、质检）必须用确定性代码实现**，
    不能依赖这里"恰好"生成正确内容 —— 这是本仓库的一贯设计。
    """

    name = "echo"

    def complete(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        started = time.perf_counter()

        if json_schema is not None:
            payload = _default_from_schema(json_schema)
            content = json.dumps(payload, ensure_ascii=False)
        else:
            content = self._compose_text(messages)

        prompt_tokens = sum(estimate_tokens(m.content) for m in messages)
        completion_tokens = estimate_tokens(content)
        return LLMResponse(
            content=content,
            model="echo-offline",
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            finish_reason="stop",
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    @staticmethod
    def _compose_text(messages: list[Message]) -> str:
        """把最后一条用户消息压缩成一句可读的确认性回复。"""
        last_user = ""
        for m in reversed(messages):
            if m.role == "user":
                last_user = m.content.strip().replace("\n", " ")
                break
        snippet = last_user[:80] + ("…" if len(last_user) > 80 else "")
        return f"[离线模式] 已收到请求：{snippet}"

    def stream_complete(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        """离线流式：按标点切片吐出。

        为什么要切成多片：真实模型的 token 流也是这样一片一片来的。
        只有切碎，语音链路上的"边生成边切句 → 逐句过内容安全 → 逐句送 TTS"
        才会真的被执行到；否则一次吐全文，那些逻辑在离线环境下等于没测。
        """
        content = self.complete(messages, temperature=temperature, max_tokens=max_tokens).content
        buffer = ""
        for ch in content:
            buffer += ch
            if ch in "。！？；，":
                yield buffer
                buffer = ""
        if buffer:
            yield buffer


def _default_from_schema(schema: dict[str, Any]) -> Any:
    """根据 JSON Schema 生成一份类型正确的默认值。

    支持 object / array / string / number / integer / boolean。
    用于离线模式下的结构化输出占位。
    """
    if not isinstance(schema, dict):
        return None

    # 显式给了默认值优先
    if "default" in schema:
        return schema["default"]

    stype = schema.get("type")
    if isinstance(stype, list):  # 联合类型，取第一个非 null
        stype = next((t for t in stype if t != "null"), stype[0] if stype else "string")

    if stype == "object" or "properties" in schema:
        props = schema.get("properties", {})
        return {k: _default_from_schema(v) for k, v in props.items()}
    if stype == "array":
        # 默认返回空数组：离线模式不做"造数据"，避免下游把占位内容当成真实结果
        return []
    if stype == "integer":
        return 0
    if stype == "number":
        return 0.0
    if stype == "boolean":
        return False
    return ""


# =========================================================================== #
# 二、OpenAI 兼容文本生成
# =========================================================================== #
class OpenAICompatLLM(LLMProvider):
    """OpenAI 兼容协议的 LLM 客户端。

    可用端点举例：
        DashScope(通义千问)  https://dashscope.aliyuncs.com/compatible-mode/v1
        DeepSeek            https://api.deepseek.com/v1
        Ollama              http://localhost:11434/v1
        vLLM                http://localhost:8000/v1
    只要兼容 OpenAI 的 /chat/completions 即可直接用。
    """

    name = "openai"

    def __init__(self, settings: Settings) -> None:
        if not settings.llm_api_key:
            raise LLMFatalError(
                "LLM_PROVIDER=openai 但未提供 LLM_API_KEY。"
                "请在 .env 中配置密钥，或改用 LLM_PROVIDER=echo 以离线模式运行。"
            )
        # 延迟导入：离线模式下完全不触碰 openai 包，减少启动开销
        from openai import OpenAI

        self._settings = settings
        self._client = OpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,  # 重试交给平台的重试器统一治理，避免双重重试放大成本
        )

    def complete(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        started = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": [m.to_openai() for m in messages],
            "temperature": self._settings.llm_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self._settings.llm_max_tokens,
        }
        if json_schema is not None:
            # 用 JSON mode 约束输出，降低解析失败率
            kwargs["response_format"] = {"type": "json_object"}

        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # 分类成"可重试 / 致命"，供平台分级处理
            raise _classify_openai_error(exc) from exc

        choice = resp.choices[0]
        usage = resp.usage
        return LLMResponse(
            content=choice.message.content or "",
            model=resp.model,
            usage=Usage(
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                total_tokens=getattr(usage, "total_tokens", 0) or 0,
            ),
            finish_reason=choice.finish_reason or "stop",
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    def stream_complete(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        """真流式（SSE）。

        语音场景的主力路径：每收到一个 delta 就往上吐，上层按句切分后立刻送 TTS，
        首字延迟取决于**第一个 token 的时间**，而不是整段生成完的时间。
        """
        kwargs: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": [m.to_openai() for m in messages],
            "temperature": self._settings.llm_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self._settings.llm_max_tokens,
            "stream": True,
        }
        try:
            stream = self._client.chat.completions.create(**kwargs)
            for chunk in stream:
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = getattr(choices[0].delta, "content", None)
                if delta:
                    yield delta
        except Exception as exc:  # noqa: BLE001 - 分类后交给上层决定重试/降级
            raise _classify_openai_error(exc) from exc


def _classify_openai_error(exc: Exception) -> Exception:
    """把厂商异常映射成平台的分级异常。"""
    status = getattr(exc, "status_code", None)
    # 超时 / 限流 / 服务端错误 → 可重试
    if status in (408, 409, 429) or (isinstance(status, int) and status >= 500):
        return LLMTransientError(str(exc))
    if status in (401, 403, 404):  # 鉴权 / 模型不存在 → 不可重试
        return LLMFatalError(str(exc))
    # 网络层异常（httpx 连接错误）也归为可重试
    name = type(exc).__name__
    if "Timeout" in name or "Connect" in name or "Transport" in name:
        return LLMTransientError(str(exc))
    return LLMFatalError(str(exc))


# =========================================================================== #
# 三、离线确定性向量化
# =========================================================================== #
class LocalHashEmbedding(EmbeddingProvider):
    """离线确定性向量化 —— n-gram 哈希 + L2 归一化。

    原理：把文本切成 2-gram / 3-gram，用 md5 稳定哈希映射到固定维度上累加，
    最后做 L2 归一化。这样：
      - 完全离线、零依赖、结果稳定（同一文本永远同一向量）；
      - 相似文本共享大量 n-gram，余弦相似度天然偏高 —— 足以支撑
        "检索链路正确性"的测试与演示。
    生产环境请把 EMBEDDING_PROVIDER 切到 openai（text-embedding-v3 等）。
    """

    name = "local-hash"

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        normalized = "".join(ch for ch in text.lower() if not ch.isspace())
        if not normalized:
            return vec

        for n in (2, 3):
            for i in range(len(normalized) - n + 1):
                gram = normalized[i : i + n]
                idx = stable_hash(gram, self.dim)
                vec[idx] += 1.0

        # L2 归一化，使余弦相似度 = 点积
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


class OpenAICompatEmbedding(EmbeddingProvider):
    """OpenAI 兼容的 /embeddings 端点。"""

    name = "openai"

    def __init__(self, settings: Settings) -> None:
        if not settings.llm_api_key:
            raise LLMFatalError("EMBEDDING_PROVIDER=openai 但未提供 LLM_API_KEY。")
        from openai import OpenAI

        self._settings = settings
        self._client = OpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.embeddings.create(model=self._settings.embedding_model, input=texts)
        return [item.embedding for item in resp.data]


# =========================================================================== #
# 四、工厂
# =========================================================================== #
def build_llm(settings: Settings | None = None) -> LLMProvider:
    """按配置构建 LLMProvider。"""
    settings = settings or get_settings()
    if settings.llm_provider == "openai":
        logger.info("LLM 供应商：OpenAI 兼容端点 model=%s base_url=%s", settings.llm_model, settings.llm_base_url)
        return OpenAICompatLLM(settings)
    logger.info("LLM 供应商：echo（离线确定性模式，无需密钥）")
    return EchoLLM()


def build_embedder(settings: Settings | None = None) -> EmbeddingProvider:
    """按配置构建 EmbeddingProvider。"""
    settings = settings or get_settings()
    if settings.embedding_provider == "openai":
        logger.info("向量化：OpenAI 兼容端点 model=%s", settings.embedding_model)
        return OpenAICompatEmbedding(settings)
    logger.info("向量化：local-hash（离线确定性，dim=%d）", settings.embedding_dim)
    return LocalHashEmbedding(settings.embedding_dim)
