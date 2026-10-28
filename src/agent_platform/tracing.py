"""全链路 trace_id 传播。

要解决的问题：一次用户请求会穿过 route → plan → execute → tool → critique → respond，
现有 span 各自生成随机 id，跨层排障时无法把这些日志串成一条链路。
这里用 ContextVar 在**同一线程同一请求**内传播 trace_id，并用 `run_id` 作为默认值，
好处是：

    * 不需要给每个函数加参数（ContextVar 天然透传）；
    * 日志里带上 trace_id 就能把 API、节点、工具三段日志串起来；
    * 与 span 树互补：trace_id 回答"是不是同一次请求"，span_id 回答"链路里谁调谁"。

跨进程/异步边界（Celery、消息队列）必须显式把 trace_id 放进消息体再恢复，
否则 ContextVar 不会跨进程传播 —— 这是这套方案唯一需要业务配合的地方。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_trace_id: ContextVar[str] = ContextVar("agent_trace_id", default="")


def new_trace_id(prefix: str = "tr") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def current_trace_id() -> str:
    return _trace_id.get()


def ensure_trace_id(candidate: str = "") -> str:
    """取当前 trace_id；没有就用 candidate（通常是 run_id）或新生成一个。"""
    existing = _trace_id.get()
    if existing:
        return existing
    trace_id = candidate or new_trace_id()
    _trace_id.set(trace_id)
    return trace_id


@contextmanager
def trace_scope(trace_id: str = "") -> Iterator[str]:
    """在一个明确的 trace 作用域内执行（常用于脚本 / 后台任务入口）。"""
    token = _trace_id.set(trace_id or new_trace_id())
    try:
        yield _trace_id.get()
    finally:
        _trace_id.reset(token)
