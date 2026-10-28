"""LLM 抽象层 —— 接口定义。

为什么要有这一层：
    业务代码不应该直接依赖某个厂商 SDK。全部通过 LLMProvider / EmbeddingProvider
    两个接口交互，好处是：
      1. 单元测试可以注入"离线确定性实现"（EchoLLM），不烧钱、不依赖网络、结果可复现；
      2. 生产可无缝切换 DashScope / DeepSeek / vLLM / Ollama（它们都兼容 OpenAI 协议）；
      3. 换厂商不用改业务代码。
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]


class Message(BaseModel):
    """一条对话消息。role 遵循 OpenAI 协议。"""

    role: Role
    content: str
    name: str | None = None  # tool 消息里表示工具名

    def to_openai(self) -> dict[str, Any]:
        """转成 OpenAI Chat Completions 的 message 结构。"""
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            payload["name"] = self.name
        return payload


class Usage(BaseModel):
    """token 用量，用于成本治理。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


class LLMResponse(BaseModel):
    """一次 LLM 调用的返回。"""

    content: str
    model: str
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str = "stop"
    latency_ms: float = 0.0


_ENCODER: Any = None


def _get_encoder() -> Any:
    """懒加载并缓存 tiktoken 编码器。

    get_encoding 首次调用要加载词表（几十毫秒），放模块级缓存避免
    高频调用的 estimate_tokens 反复付出这笔开销。
    """
    global _ENCODER
    if _ENCODER is None:
        import tiktoken

        _ENCODER = tiktoken.get_encoding("cl100k_base")
    return _ENCODER


def estimate_tokens(text: str) -> int:
    """估算文本 token 数。

    优先用 tiktoken 精确计数；不可用时退化为**分语种的经验公式**：
        - CJK 字符：约 1 token/字（BPE 对中文的压缩远低于英文，
          再按"字符数 / 4"算会把中文低估 4 倍，预算守卫形同虚设）；
        - 其他字符：约 4 字符 / token（英文经验值）。
    仅用于预算守卫与成本估算，不要求绝对精确；但**方向性必须对**——
    本项目中文语料为主，宁可略高估（多留预算）不可低估（烧穿预算）。
    """
    if not text:
        return 0
    try:  # pragma: no cover - 依赖可选包
        return len(_get_encoder().encode(text))
    except Exception:  # noqa: BLE001 - tiktoken 缺失/词表加载失败时兜底
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        other = len(text) - cjk
        return max(1, cjk + other // 4)


def stable_hash(text: str, mod: int) -> int:
    """跨进程稳定的哈希。

    注意：Python 内置 hash() 对 str 是带随机盐的（每个进程不同），
    用在向量化 / 分片这类需要"跨进程一致"的场景会导致结果漂移，
    所以这里统一走 md5。
    """
    return int.from_bytes(hashlib.md5(text.encode("utf-8")).digest()[:8], "big") % mod


class LLMProvider(ABC):
    """文本生成接口。"""

    name: str = "base"

    @abstractmethod
    def complete(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        """生成回复。

        参数：
            messages:    对话消息列表
            temperature: 采样温度（None 用配置默认）
            max_tokens:  输出上限
            json_schema: 传入则表示要求模型输出符合该 JSON Schema 的结构化结果
        """

    def stream_complete(
        self,
        messages: list[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        """流式生成（**可选能力**）。

        语音对话必须流式：TTS 要边收文本边合成，否则用户要等整段生成完
        才能听到第一个字（首字延迟 3-5 秒，体验直接崩）。

        默认实现退化为"一次产出全文" —— 这样任何只实现了 complete 的供应商
        （以及项目六的推理网关包装）都能直接跑语音链路，只是首字延迟偏高。
        真正做流式的实现（OpenAI 兼容 / EchoLLM）会覆写本方法。
        """
        resp = self.complete(messages, temperature=temperature, max_tokens=max_tokens)
        if resp.content:
            yield resp.content

    def complete_json(
        self,
        messages: list[Message],
        schema: dict[str, Any],
        *,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        """结构化输出便捷方法，返回已解析的 dict。

        约定：模型必须返回可解析 JSON；解析失败由调用方决定降级策略
        （业务侧的确定性实现不会依赖这里，见各服务的设计说明）。
        """
        import json

        resp = self.complete(messages, temperature=temperature, json_schema=schema)
        text = resp.content.strip()
        # 容错：模型偶尔会包 ```json ``` 或前后带解释文字
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end != -1 and end > start:
                return json.loads(text[start : end + 1])
            raise


class EmbeddingProvider(ABC):
    """文本向量化接口。"""

    name: str = "base"

    def __init__(self, dim: int) -> None:
        self.dim = dim

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量向量化（用于建库）。"""

    def embed_query(self, text: str) -> list[float]:
        """单条查询向量化。

        默认复用 embed_documents；某些模型（如 bge 系列）对 query/passage
        使用不同前缀，需要时在此覆写。
        """
        return self.embed_documents([text])[0]
