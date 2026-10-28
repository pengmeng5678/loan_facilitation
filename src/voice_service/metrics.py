"""语音链路的延迟观测。

文本问答只看"总耗时"，语音不行 —— 客户感知的是**首字延迟**：
    RAG 慢 300ms，客户听不出来；
    LLM 首字慢 2s，客户已经在说"喂？"了。
所以这里的指标按段拆开，优化时才知道该压哪一段（对应"抓主要矛盾"）。

同时统计业务侧信号（拒答率、转人工率、被内容安全替换的比例、打断率）——
这些才是判断"这个语音客服能不能上线"的依据，光看延迟不够。
"""

from __future__ import annotations

from collections import deque
from typing import Any

from voice_service.domain import VoiceTurn


def percentile(values: list[float], p: float) -> float:
    """线性插值分位数（p 取 0~1）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * p
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return float(ordered[low] * (1 - frac) + ordered[high] * frac)


class VoiceMetrics:
    """进程内指标聚合（重启即清零；要长期留存请把 turn 同步到数据中台）。"""

    def __init__(self, *, window: int = 500) -> None:
        self.ttft: deque[float] = deque(maxlen=window)
        self.total: deque[float] = deque(maxlen=window)
        self.rag: deque[float] = deque(maxlen=window)
        self.counters: dict[str, int] = {}
        self.budget_alerts: int = 0

    def _inc(self, key: str, delta: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + delta

    def record(self, turn: VoiceTurn, *, replaced: bool = False) -> None:
        self.ttft.append(turn.latency.llm_ttft_ms)
        self.total.append(turn.latency.total_ms)
        self.rag.append(turn.latency.rag_ms)
        self._inc("turns")
        if turn.refused:
            self._inc("refused")
        if turn.needs_human:
            self._inc("handoff")
        if turn.interrupted:
            self._inc("interrupted")
        if replaced:
            self._inc("replaced_by_censor")
        if turn.latency.llm_ttft_ms > 2500:
            # 语音体验的分水岭：首字超过 2.5s 客户基本会开口打断
            self._inc("slow_ttft")
            self.budget_alerts += 1

    def snapshot(self) -> dict[str, Any]:
        turns = self.counters.get("turns", 0) or 1
        return {
            "turns": self.counters.get("turns", 0),
            "refused": self.counters.get("refused", 0),
            "handoff": self.counters.get("handoff", 0),
            "interrupted": self.counters.get("interrupted", 0),
            "replaced_by_censor": self.counters.get("replaced_by_censor", 0),
            "slow_ttft": self.counters.get("slow_ttft", 0),
            "refuse_rate": round(self.counters.get("refused", 0) / turns, 3),
            "handoff_rate": round(self.counters.get("handoff", 0) / turns, 3),
            "latency_ms": {
                "ttft_p50": round(percentile(list(self.ttft), 0.5), 1),
                "ttft_p95": round(percentile(list(self.ttft), 0.95), 1),
                "rag_p50": round(percentile(list(self.rag), 0.5), 1),
                "total_p50": round(percentile(list(self.total), 0.5), 1),
                "total_p95": round(percentile(list(self.total), 0.95), 1),
            },
        }
