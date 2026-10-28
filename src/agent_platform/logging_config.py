"""日志配置。

生产可观测的第一层就是结构化日志。这里提供：
    - configure_logging()   统一初始化（幂等）
    - request_id / run_id 的上下文注入（通过 logging.Filter）
    - JSON 输出开关（LOG_JSON=1 时启用，便于 ELK / Loki 采集）
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextvars import ContextVar
from typing import Any

# 用 ContextVar 承载"当前请求/会话"的追踪标识，天然支持并发隔离
_run_id_var: ContextVar[str] = ContextVar("run_id", default="-")
_thread_id_var: ContextVar[str] = ContextVar("thread_id", default="-")


def bind_run_context(run_id: str | None = None, thread_id: str | None = None) -> None:
    """绑定当前上下文的 run_id / thread_id，后续所有日志自动携带。"""
    if run_id:
        _run_id_var.set(run_id)
    if thread_id:
        _thread_id_var.set(thread_id)


class _ContextFilter(logging.Filter):
    """把上下文变量注入到每条日志记录上。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.run_id = _run_id_var.get()
        record.thread_id = _thread_id_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    """JSON 行格式，字段固定，方便日志系统解析。"""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", "-"),
            "thread_id": getattr(record, "thread_id", "-"),
            "msg": record.getMessage(),
        }
        # 业务侧可通过 logger.info("...", extra={"extra_fields": {...}}) 附加结构化字段
        extra_fields = getattr(record, "extra_fields", None)
        if isinstance(extra_fields, dict):
            payload.update(extra_fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _TextFormatter(logging.Formatter):
    """人类可读格式（本地开发用）。"""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        base = (
            f"{time.strftime('%H:%M:%S', time.localtime(record.created))} "
            f"{record.levelname:<7} [{record.name}] "
            f"(run={getattr(record, 'run_id', '-')} thread={getattr(record, 'thread_id', '-')}) "
            f"{record.getMessage()}"
        )
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


_configured = False


def configure_logging(level: str | None = None, force: bool = False) -> None:
    """初始化根 logger（幂等）。

    参数：
        level: 日志级别，缺省读 LOG_LEVEL 环境变量，再缺省 INFO
        force: 为 True 时重复调用也会重建 handler（测试用）
    """
    global _configured
    if _configured and not force:
        return

    log_level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    use_json = os.getenv("LOG_JSON", "0") in {"1", "true", "True"}

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_JsonFormatter() if use_json else _TextFormatter())
    handler.addFilter(_ContextFilter())

    root = logging.getLogger()
    # 清掉历史 handler，避免重复输出（尤其在 notebook / 反复调用的场景）
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(log_level)

    # 第三方库降噪：这些库在 INFO 级别会刷屏，且对我们排障价值低
    for noisy in ("httpx", "httpcore", "urllib3", "openai", "qdrant_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """获取带命名空间的 logger。"""
    configure_logging()
    return logging.getLogger(name)
