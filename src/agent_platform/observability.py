"""可观测：链路追踪（tracing）与指标（metrics）。

对应简历里的"可观测"：
    - span 级埋点：每个节点/工具调用都是一个 span，记录耗时、token、成本、状态；
    - 支持按会话回放完整决策轨迹 —— 定位"第 3 步静默失败导致最终答案似是而非"；
    - 指标快照可直接暴露给 /metrics（Prometheus 格式由 api 层做转换）。

实现刻意保持轻量、无外部依赖，但接口对齐 OpenTelemetry 的概念
（span / attribute / status），生产接入 OTel SDK 时替换实现即可。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from agent_platform.logging_config import get_logger
from agent_platform.tracing import current_trace_id, ensure_trace_id

logger = get_logger(__name__)


# =========================================================================== #
# 一、链路追踪
# =========================================================================== #
@dataclass
class Span:
    """一个执行单元（节点 / 工具调用 / LLM 调用）。"""

    span_id: str
    name: str
    kind: str  # node | tool | llm | graph
    parent_id: str | None
    run_id: str
    thread_id: str
    start: float
    end: float = 0.0
    trace_id: str = ""
    status: str = "ok"  # ok | error
    error: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        return max(0.0, (self.end - self.start) * 1000) if self.end else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "name": self.name,
            "kind": self.kind,
            "parent_id": self.parent_id,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "trace_id": self.trace_id,
            "start": self.start,
            "duration_ms": round(self.duration_ms, 2),
            "status": self.status,
            "error": self.error,
            "attributes": self.attributes,
        }


class Tracer:
    """极简 tracer：记录 span 列表，支持导出为决策轨迹。"""

    def __init__(self, run_id: str = "-", thread_id: str = "-", metrics: Metrics | None = None) -> None:
        self.run_id = run_id
        self.thread_id = thread_id
        self.metrics = metrics
        self._spans: list[Span] = []
        self._lock = threading.Lock()

    def bind(self, run_id: str, thread_id: str) -> None:
        """把 tracer 绑定到新的一次 run，并清空历史 span。

        为什么可以这样复用同一个 tracer 实例：图的节点在编译时就闭包捕获了
        deps，无法每次运行重新注入；所以采用"每次 run 前重置"的方式。
        注意这意味着**一个 runtime 实例不应被多线程并发复用** ——
        生产部署请按"每 worker 独立进程 + PostgreSQL Checkpointer 共享状态"
        的方式横向扩展（见 docker-compose 与 docs/architecture.md）。
        """
        with self._lock:
            self.run_id = run_id
            self.thread_id = thread_id
            self._spans.clear()
        # trace_id 用 run_id 兜底：同一次 run 内所有 span/日志天然可串联
        ensure_trace_id(run_id)

    @contextmanager
    def span(self, name: str, kind: str = "node", **attributes: Any) -> Iterator[Span]:
        """上下文管理器：进入即开 span，退出自动收尾（含异常标记）。"""
        trace_id = ensure_trace_id(self.run_id if self.run_id != "-" else "")
        sp = Span(
            span_id=f"sp_{uuid.uuid4().hex[:10]}",
            name=name,
            kind=kind,
            parent_id=None,
            run_id=self.run_id,
            thread_id=self.thread_id,
            trace_id=trace_id,
            start=time.perf_counter(),
            attributes=dict(attributes),
        )
        try:
            yield sp
            sp.status = "ok"
        except Exception as exc:  # noqa: BLE001 - 追踪不能吞掉业务异常，只做记录后重抛
            sp.status = "error"
            sp.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            sp.end = time.perf_counter()
            with self._lock:
                self._spans.append(sp)
            if self.metrics is not None:
                self.metrics.observe(f"span_{name}_ms", sp.duration_ms)
                if sp.status == "error":
                    self.metrics.incr(f"span_{name}_error_total")
            logger.info(
                "span %s kind=%s trace_id=%s duration_ms=%.2f status=%s",
                name, kind, trace_id, sp.duration_ms, sp.status,
                extra={"trace_id": trace_id, "span": name, "kind": kind},
            )

    def spans(self) -> list[Span]:
        with self._lock:
            return list(self._spans)

    def to_trajectory(self) -> list[dict[str, Any]]:
        """导出为"决策轨迹"——按开始时间排序的 span 字典列表。

        这就是排障时看的"第 N 步到底发生了什么"。
        """
        return [s.to_dict() for s in sorted(self.spans(), key=lambda x: x.start)]

    def summary(self) -> dict[str, Any]:
        """按 kind 聚合耗时/数量，用于一眼看瓶颈在哪。"""
        agg: dict[str, dict[str, Any]] = {}
        for s in self.spans():
            bucket = agg.setdefault(s.kind, {"count": 0, "total_ms": 0.0, "errors": 0})
            bucket["count"] += 1
            bucket["total_ms"] += s.duration_ms
            if s.status == "error":
                bucket["errors"] += 1
        for v in agg.values():
            v["total_ms"] = round(v["total_ms"], 2)
        return agg

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()


# =========================================================================== #
# 二、指标
# =========================================================================== #
class Metrics:
    """极简指标收集：计数器 + 直方图（保留原始样本以便算 P95）。

    内部按 (name, labels) 二元组存储，`counters()` 再拼回 `name{k=v}` 字符串键
    （保持既有快照格式不变）；`structured_*` 给出结构化样本，供
    Prometheus 渲染直接用 —— 渲染时不该再去反解析字符串。
    """

    def __init__(self) -> None:
        # key = (metric_name, ((label, value), ...) 排序后)
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = {}
        self._lock = threading.Lock()

    def incr(self, name: str, value: float = 1.0, **labels: Any) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def observe(self, name: str, value: float, **labels: Any) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._histograms.setdefault(key, []).append(value)

    @staticmethod
    def _key(name: str, labels: dict[str, Any]) -> tuple[str, tuple[tuple[str, str], ...]]:
        return (name, tuple(sorted((str(k), str(v)) for k, v in labels.items())))

    @staticmethod
    def _render_key(key: tuple[str, tuple[tuple[str, str], ...]]) -> str:
        """(name, labels) → 'name{k=v,k2=v2}'（与历史快照格式一致）。"""
        name, labels = key
        if not labels:
            return name
        label_str = ",".join(f"{k}={v}" for k, v in labels)
        return f"{name}{{{label_str}}}"

    def counters(self) -> dict[str, float]:
        with self._lock:
            return {self._render_key(k): v for k, v in self._counters.items()}

    def structured_counters(self) -> list[tuple[str, dict[str, str], float]]:
        """[(name, {label: value}, value)] —— Prometheus /metrics 渲染用。"""
        with self._lock:
            return [(name, dict(labels), v) for (name, labels), v in self._counters.items()]

    def structured_histograms(self) -> list[tuple[str, dict[str, str], list[float]]]:
        with self._lock:
            return [(name, dict(labels), list(samples)) for (name, labels), samples in self._histograms.items()]

    def histogram_stats(self) -> dict[str, dict[str, float]]:
        """返回 count / avg / p50 / p95 / max。"""
        out: dict[str, dict[str, float]] = {}
        with self._lock:
            items = list(self._histograms.items())
        for key, samples in items:
            if not samples:
                continue
            ordered = sorted(samples)
            n = len(ordered)
            out[self._render_key(key)] = {
                "count": float(n),
                "avg": round(sum(ordered) / n, 2),
                "p50": round(ordered[int(n * 0.5)], 2),
                "p95": round(ordered[min(n - 1, int(n * 0.95))], 2),
                "max": round(ordered[-1], 2),
            }
        return out

    def snapshot(self) -> dict[str, Any]:
        return {"counters": self.counters(), "histograms": self.histogram_stats()}

    def clear(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()
