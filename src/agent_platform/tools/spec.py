"""工具（Tool）定义。

对应简历里的 "MCP 工具生态"：
    企业内部 REST / gRPC 服务 → 统一映射为 ToolSpec → 注册进 ToolRegistry
    → 按意图做作用域裁剪（scoping）→ 只把相关工具 schema 注入 prompt。

每个 ToolSpec 除了"怎么调"，还声明了治理必需的元数据：
    risk_level   风险等级（low/medium/high）—— high 必须过 HITL 审批
    idempotent   是否幂等 —— 非幂等工具的重试要格外谨慎
    tags         能力标签 —— 作用域裁剪的召回依据
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

RiskLevel = Literal["low", "medium", "high"]

# 工具处理函数签名：接收关键字参数，返回任意可序列化结果
ToolHandler = Callable[..., Any]


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的完整描述。"""

    name: str
    """工具唯一名，建议 namespace.action 形式，例如 crm.create_lead。"""

    description: str
    """给模型看的自然语言说明 —— 写清楚"什么时候该用它"，比写参数更重要。"""

    parameters: dict[str, Any]
    """入参的 JSON Schema（type=object）。用于喂给模型 + 运行时校验。"""

    handler: ToolHandler = field(repr=False, compare=False)
    """真正的执行函数。"""

    tags: tuple[str, ...] = ()
    """能力标签，用于作用域裁剪召回，例如 ("crm", "lead", "write")。"""

    risk_level: RiskLevel = "low"
    """风险等级。high 级别的工具会强制走 HITL 审批检查点。"""

    idempotent: bool = True
    """是否幂等。非幂等 + 可重试错误 = 需要幂等键保护。"""

    timeout_seconds: float = 10.0
    """单次调用超时（秒）。"""

    def to_openai_schema(self) -> dict[str, Any]:
        """转成 OpenAI function-calling 的工具描述结构。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def schema_text(self) -> str:
        """精简的文本形式，用于注入 prompt 时控制 token。

        为什么不用完整 JSON：工具多的时候完整 schema 非常占 token，
        裁剪后的文本形式能进一步压缩（对应简历里"单轮 prompt token -64%"）。
        """
        required = self.parameters.get("required", [])
        props = self.parameters.get("properties", {})
        args = ", ".join(f"{k}:{v.get('type', 'any')}" for k, v in props.items())
        mark = " *必填" if required else ""
        risk = f" risk={self.risk_level}" if self.risk_level != "low" else ""
        return f"- {self.name}({args}){mark}：{self.description}{risk}"


@dataclass
class ToolResult:
    """一次工具调用的结果。"""

    ok: bool
    tool: str
    data: Any = None
    error: str | None = None
    latency_ms: float = 0.0
    attempts: int = 1
    blocked: bool = False
    """是否被作用域/权限拦截（用于安全审计）。"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "data": self.data,
            "error": self.error,
            "latency_ms": round(self.latency_ms, 2),
            "attempts": self.attempts,
            "blocked": self.blocked,
        }


@dataclass
class ToolCallRecord:
    """工具调用审计记录（写入 state.tool_calls，供可观测与合规追溯）。"""

    tool: str
    arguments: dict[str, Any]
    ok: bool
    latency_ms: float
    step: int
    error: str | None = None
    blocked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "arguments": self.arguments,
            "ok": self.ok,
            "latency_ms": round(self.latency_ms, 2),
            "step": self.step,
            "error": self.error,
            "blocked": self.blocked,
            "ts": time.time(),
        }
