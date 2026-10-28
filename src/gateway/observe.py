"""网关可观测：span 级埋点 + 成本台账 + 会话轨迹回放。

对应简历里的可观测能力，解决的是 Agent 特有的难题：
    **错误不发生在最后一跳，而藏在中间某一步。**

比如"最终答案似是而非"，根因可能是第 3 步工具调用静默失败、
或者第 2 步路由把小任务丢给了小模型导致答偏 —— 只看最终回复永远查不出来。
所以这里把一次请求拆成一串 span（route → cache → trim → ratelimit → llm），
每个 span 记录**模型、输入输出 token、耗时、成本、成败**，
按 thread_id 串起来就是可回放的"决策轨迹"。

实现刻意轻量、零外部依赖，但概念与字段对齐 OpenTelemetry
（span / attribute / status）与 Langfuse 的 trace 结构，生产可直接替换。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from gateway.domain import CostRecord


# =========================================================================== #
# 一、span 级追踪
# =========================================================================== #
@dataclass
class Span:
    """一个执行单元。"""

    span_id: str
    name: str
    kind: str  # gateway | route | cache | ratelimit | trim | llm | fallback
    run_id: str
    thread_id: str
    start: float
    end: float = 0.0
    status: str = "ok"
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
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "start": self.start,
            "duration_ms": round(self.duration_ms, 2),
            "status": self.status,
            "error": self.error,
            "attributes": self.attributes,
        }


class GatewayTracer:
    """轻量 tracer（复用方式与平台 Tracer 一致：每次 run 前 bind 重置）。"""

    def __init__(self) -> None:
        self.run_id = "-"
        self.thread_id = "-"
        self._spans: list[Span] = []
        self._lock = threading.Lock()

    def bind(self, run_id: str, thread_id: str) -> None:
        with self._lock:
            self.run_id = run_id
            self.thread_id = thread_id
            self._spans.clear()

    @contextmanager
    def span(self, name: str, kind: str = "gateway", **attributes: Any) -> Iterator[Span]:
        sp = Span(
            span_id=f"gsp_{uuid.uuid4().hex[:10]}",
            name=name,
            kind=kind,
            run_id=self.run_id,
            thread_id=self.thread_id,
            start=time.perf_counter(),
            attributes=dict(attributes),
        )
        try:
            yield sp
            sp.status = "ok"
        except Exception as exc:  # noqa: BLE001 - 追踪只做记录，不吞业务异常
            sp.status = "error"
            sp.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            sp.end = time.perf_counter()
            with self._lock:
                self._spans.append(sp)

    def spans(self) -> list[Span]:
        with self._lock:
            return list(self._spans)

    def to_trajectory(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in sorted(self.spans(), key=lambda x: x.start)]

    def summary(self) -> dict[str, Any]:
        agg: dict[str, dict[str, Any]] = {}
        for s in self.spans():
            bucket = agg.setdefault(s.kind, {"count": 0, "total_ms": 0.0, "errors": 0})
            bucket["count"] += 1
            bucket["total_ms"] = round(bucket["total_ms"] + s.duration_ms, 2)
            if s.status == "error":
                bucket["errors"] += 1
        return agg

    def clear(self) -> None:
        with self._lock:
            self._spans.clear()


# =========================================================================== #
# 二、成本台账
# =========================================================================== #
class CostLedger:
    """成本台账：把每次调用的成本记下来，回答"钱花在哪"。"""

    def __init__(self) -> None:
        self._records: list[CostRecord] = []
        self._lock = threading.Lock()

    def record(self, rec: CostRecord) -> None:
        with self._lock:
            self._records.append(rec)

    def all(self) -> list[CostRecord]:
        with self._lock:
            return list(self._records)

    # ---- 聚合视图（这几个方法就是"每会话/每任务单价"的计算口径）---- #
    def total(self) -> dict[str, float]:
        records = self.all()
        cost = sum(r.cost for r in records)
        baseline = sum(r.baseline_cost for r in records)
        saved = sum(r.saved for r in records)
        return {
            "calls": len(records),
            "cost": round(cost, 6),
            "baseline_cost": round(baseline, 6),
            "saved": round(saved, 6),
            "saved_percent": round(saved / baseline * 100, 2) if baseline > 0 else 0.0,
        }

    def by(self, dimension: str) -> dict[str, dict[str, float]]:
        """按维度聚合。dimension ∈ {thread_id, task, scope, tier, model}"""
        buckets: dict[str, dict[str, float]] = {}
        for r in self.all():
            key = str(getattr(r, dimension, "") or "unknown")
            b = buckets.setdefault(
                key, {"calls": 0, "cost": 0.0, "baseline_cost": 0.0, "tokens": 0, "saved": 0.0}
            )
            b["calls"] += 1
            b["cost"] += r.cost
            b["baseline_cost"] += r.baseline_cost
            b["tokens"] += r.total_tokens
            b["saved"] += r.saved
        return {
            k: {kk: round(vv, 6) if isinstance(vv, float) else vv for kk, vv in v.items()}
            for k, v in buckets.items()
        }

    def per_session(self) -> dict[str, dict[str, float]]:
        """**每会话单价** —— 面试里最能体现"真做过生产系统"的口径。"""
        return self.by("thread_id")

    def cache_hit_rate(self) -> float:
        records = self.all()
        if not records:
            return 0.0
        hits = sum(1 for r in records if r.cache_status != "miss")
        return round(hits / len(records), 4)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


# =========================================================================== #
# 三、可观测门面
# =========================================================================== #
class GatewayObserver:
    """把追踪、台账、回放串起来，作为网关的观测出口。"""

    def __init__(self, *, store: Any | None = None, max_events_per_thread: int = 200) -> None:
        self.tracer = GatewayTracer()
        self.ledger = CostLedger()
        self.store = store
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._max_events = max_events_per_thread
        self._lock = threading.Lock()

    def begin(self, run_id: str, thread_id: str) -> None:
        self.tracer.bind(run_id, thread_id)
        self._append_event(thread_id, {"type": "run_start", "run_id": run_id, "ts": time.time()})

    def record_cost(self, rec: CostRecord) -> None:
        self.ledger.record(rec)
        if self.store is not None:
            try:
                self.store.add_cost(rec)
            except Exception:  # noqa: BLE001 - 落盘失败不能影响主链路
                pass

    def note(self, thread_id: str, event: dict[str, Any]) -> None:
        """记录一个业务事件（路由决策/缓存命中/降级等），用于轨迹回放。"""
        self._append_event(thread_id, {**event, "ts": time.time()})

    def replay(self, thread_id: str) -> list[dict[str, Any]]:
        """按会话回放完整决策轨迹。

        优先用内存事件（含路由/缓存/裁剪等细节）；进程重启后内存没了，
        退化用成本记录（仍能看到"哪一步花了多少钱"）。
        """
        with self._lock:
            events = list(self._events.get(thread_id, []))
        if events:
            return events
        if self.store is not None:
            return [
                {
                    "type": "cost",
                    "model": r.model,
                    "tier": r.tier,
                    "task": r.task,
                    "tokens": r.total_tokens,
                    "cost": r.cost,
                    "cache_status": r.cache_status,
                    "ok": r.ok,
                    "ts": r.ts,
                }
                for r in self.store.costs(thread_id=thread_id)
            ]
        return []

    def _append_event(self, thread_id: str, event: dict[str, Any]) -> None:
        if not thread_id or thread_id == "-":
            return
        with self._lock:
            bucket = self._events.setdefault(thread_id, [])
            bucket.append(event)
            if len(bucket) > self._max_events:
                del bucket[: len(bucket) - self._max_events]

    def trajectory(self) -> list[dict[str, Any]]:
        return self.tracer.to_trajectory()

    def stats(self) -> dict[str, Any]:
        return {
            "cost": self.ledger.total(),
            "cache_hit_rate": self.ledger.cache_hit_rate(),
            "by_tier": self.ledger.by("tier"),
            "by_task": self.ledger.by("task"),
            "spans": self.tracer.summary(),
        }
