"""数据中台异常。

继承 `AgentPlatformError`，于是 API 层的统一异常处理器能直接把它
映射成结构化 HTTP 错误（不需要每个路由自己 try/except）。
"""

from __future__ import annotations

from agent_platform.errors import AgentPlatformError


class DataPlatformError(AgentPlatformError):
    """数据中台异常基类。"""


class InvalidTransition(DataPlatformError):
    """非法的审核状态流转（例：rejected 未经编辑直接 approve）。"""


class ReviewRequired(DataPlatformError):
    """数据尚未通过人工审核，不允许进入下游。"""


class SourceUnavailable(DataPlatformError):
    """数据源不可用（路径不存在 / 表结构不符）。"""


class ExportBlocked(DataPlatformError):
    """导出前置校验未通过（脱敏残留 / 无已批准数据）。"""
