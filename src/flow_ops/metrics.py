"""流程行为指标（项目八的"结果量化"数据源）。

对应简历 Result 表的四个指标，全部从库里现算，**不用手工统计**：

    漏跟进率      = 有未处理 SOP 预警的线索 / 活跃线索
    准备时长      = 到店前准备包的平均生成耗时（ms）
    重复操作拦截  = 幂等命中次数（审计里的 idempotent_hit）
    HITL 待审     = pending 审批数（+ 平均决策时长，衡量审批流是否堵）

**为什么准备时长要记 p95 而不是均值**：主管关心的是"最慢那几单
有没有拖到客户到店"，均值会把最慢的几单藏起来。
"""

from __future__ import annotations

from typing import Any

from agent_platform.logging_config import get_logger
from flow_ops.store import FlowStore

logger = get_logger(__name__)


def percentile(values: list[float], p: float) -> float:
    """朴素分位数（数据量小，线性插值足够，不引 numpy）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 1)
    k = (len(ordered) - 1) * p
    low = int(k)
    high = min(low + 1, len(ordered) - 1)
    frac = k - low
    return round(ordered[low] + (ordered[high] - ordered[low]) * frac, 1)


class FlowMetrics:
    """流程指标聚合。"""

    def __init__(self, store: FlowStore) -> None:
        self._store = store
        self._prepare_ms: list[float] = []  # 准备包耗时（内存累计，重启清零可接受）

    def record_prepare(self, elapsed_ms: float) -> None:
        self._prepare_ms.append(elapsed_ms)

    def snapshot(self) -> dict[str, Any]:
        stage_counts = self._store.count_by_stage()
        active_stages = {
            s: n for s, n in stage_counts.items() if s not in ("signed", "lost")
        }
        active_leads = sum(active_stages.values())

        open_alerts = len(self._store.list_alerts(include_acknowledged=False, limit=1000))
        overdue_leads = len(
            {a.lead_id for a in self._store.list_alerts(include_acknowledged=False, limit=1000)}
        )
        missed_followup_rate = round(overdue_leads / active_leads, 4) if active_leads else 0.0

        idempotent_hits = self._store.count_idempotent_hits()
        pending = self._store.list_approvals(status="pending", limit=1000)
        decided = self._store.list_approvals(status="", limit=1000)
        decision_latencies = [
            a.decided_at - a.created_at
            for a in decided
            if a.decided_at and a.status.value in ("approved", "rejected")
        ]

        prepare = {
            "count": len(self._prepare_ms),
            "avg_ms": round(sum(self._prepare_ms) / len(self._prepare_ms), 1)
            if self._prepare_ms else 0.0,
            "p95_ms": percentile(self._prepare_ms, 0.95),
        }

        snap: dict[str, Any] = {
            "leads": {
                "total": sum(stage_counts.values()),
                "active": active_leads,
                "by_stage": stage_counts,
            },
            "missed_followup_rate": missed_followup_rate,
            "sop_alerts_open": open_alerts,
            "idempotent_hits": idempotent_hits,
            "hitl": {
                "pending": len(pending),
                "avg_decision_minutes": round(
                    sum(decision_latencies) / len(decision_latencies) / 60, 1
                ) if decision_latencies else 0.0,
            },
            "prepare": prepare,
        }
        return snap
