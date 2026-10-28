"""统一异常体系。

为什么要分层：平台对"失败"要做分级处理（可重试 / 可自修正 / 必须熔断转人工），
所以异常必须能一眼区分类型，而不是一律 raise Exception。
    - TransientError      → 指数退避重试
    - RecoverableError    → 回灌给模型自修正
    - FatalError          → 直接熔断并转人工
"""

from __future__ import annotations


class AgentPlatformError(Exception):
    """平台异常基类。所有自定义异常都继承它，便于统一捕获。"""


# --------------------------------------------------------------------------- #
# 配置类
# --------------------------------------------------------------------------- #
class ConfigError(AgentPlatformError):
    """配置缺失或非法。"""


# --------------------------------------------------------------------------- #
# LLM 类
# --------------------------------------------------------------------------- #
class LLMError(AgentPlatformError):
    """LLM 调用异常基类。"""


class LLMTransientError(LLMError):
    """LLM 临时故障（超时、限流、5xx）——可重试。"""


class LLMFatalError(LLMError):
    """LLM 致命故障（鉴权失败、模型不存在）——不可重试。"""


# --------------------------------------------------------------------------- #
# 工具类
# --------------------------------------------------------------------------- #
class ToolError(AgentPlatformError):
    """工具调用异常基类。"""


class ToolNotFoundError(ToolError):
    """工具未注册。"""


class ToolValidationError(ToolError):
    """入参不满足工具 schema——属于"可自修正"，应回灌给模型重新给参数。"""


class ToolExecutionError(ToolError):
    """工具执行失败。"""


class ToolPermissionError(ToolError):
    """工具不在当前作用域内 —— 对应"最小权限 / 过度代理防护"。"""


# --------------------------------------------------------------------------- #
# 编排与治理类
# --------------------------------------------------------------------------- #
class AgentNotFoundError(AgentPlatformError):
    """请求的业务 Agent 未注册。"""


class RunNotFoundError(AgentPlatformError):
    """线程（会话）不存在。"""


class StepBudgetExceeded(AgentPlatformError):
    """超过最大步数——熔断转人工，杜绝死循环烧钱。"""


class TokenBudgetExceeded(AgentPlatformError):
    """超过 token 预算——熔断转人工。"""


class CircuitOpenError(AgentPlatformError):
    """熔断器打开，拒绝调用。"""


class ApprovalRequired(AgentPlatformError):
    """高风险动作需要人工审批（HITL 检查点）。"""


class ApprovalTimeout(AgentPlatformError):
    """审批超时（按策略降级）。"""


# --------------------------------------------------------------------------- #
# 业务/检索类（供下游服务复用）
# --------------------------------------------------------------------------- #
class RetrievalError(AgentPlatformError):
    """检索失败。"""


class PolicyNotFoundError(AgentPlatformError):
    """未检索到可支撑的政策条款——按合规要求应拒答而非编造。"""
