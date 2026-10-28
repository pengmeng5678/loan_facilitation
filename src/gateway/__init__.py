"""项目六：大模型推理网关与 Agent 可观测评测平台。

这个包是**基础设施**，不产生任何业务功能。它的价值是让项目二/三这种业务
Agent 跑得**更省、更稳、更敢改**：

    统一网关（怎么调） → 成本治理（花多少钱） → 可观测（哪儿坏了） → 评测（敢不敢改）

设计上刻意与业务解耦：网关只认 `LLMProvider` 接口和"一段文本 + 一个任务标签"，
不知道什么是"助贷产品"或"话术陪练"。所以它可以被任何业务复用，
业务也可以在没有网关的情况下照常运行（见 config.gateway_enabled 开关）。
"""

from gateway.domain import (
    CachePolicy,
    CaseResult,
    CostRecord,
    EvalCase,
    EvalReport,
    GatewayStats,
    ModelTier,
    RouteDecision,
)
from gateway.gateway import LLMGateway, build_gateway
from gateway.store import GatewayStore

__all__ = [
    "CachePolicy",
    "CaseResult",
    "CostRecord",
    "EvalCase",
    "EvalReport",
    "GatewayStats",
    "LLMGateway",
    "GatewayStore",
    "ModelTier",
    "RouteDecision",
    "build_gateway",
]
