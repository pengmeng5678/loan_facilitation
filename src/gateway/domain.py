"""项目六的领域模型。

这些数据结构是网关"可算账、可追踪、可回归"三种能力的数据载体：
    RouteDecision  —— 路由决策（走哪个档位、为什么）→ 成本治理的依据
    CostRecord     —— 每次调用一条成本记录 → "每会话/每任务单价"的来源
    EvalCase / EvalReport —— Golden Set 用例与评测结论 → 变更是否可发布
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# 基础类型
# --------------------------------------------------------------------------- #
ModelTier = Literal["small", "medium", "large"]
"""模型分档：轻任务走 small、常规走 medium、复杂推理走 large。"""

CachePolicy = Literal["off", "exact", "semantic"]
"""缓存策略。

    off      不查缓存（政策/利率这类**确定性要求高**的问答必须用它 —— 宁可真花钱）
    exact    只做精确命中（prompt 完全一致）
    semantic 允许语义近似命中（需过相似度阈值，且必须带 TTL）
"""

SpanKind = Literal["gateway", "route", "cache", "ratelimit", "trim", "llm", "fallback"]


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
class RouteDecision(BaseModel):
    """一次请求的路由决策。"""

    tier: ModelTier
    model: str
    complexity: float = Field(..., ge=0, description="复杂度得分（0-1+）")
    reason: str = ""
    signals: dict[str, Any] = Field(default_factory=dict, description="命中的复杂度信号")
    forced: bool = Field(False, description="是否由调用方强制指定档位")
    fallback_chain: list[str] = Field(default_factory=list, description="降级顺序（模型名）")


# --------------------------------------------------------------------------- #
# 成本
# --------------------------------------------------------------------------- #
class CostRecord(BaseModel):
    """一次 LLM 调用的成本记录（成本治理的最小粒度）。"""

    record_id: str
    run_id: str = ""
    thread_id: str = ""
    scope: str = Field("", description="业务作用域（如 agent 名 / 任务类型），用于配额与归因")
    task: str = Field("", description="任务标签，如 policy_qa / roleplay / judge")
    tier: ModelTier = "medium"
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float = Field(0.0, description="本次实际成本（¥）")
    baseline_cost: float = Field(0.0, description="不做治理的对照成本（按大模型单价估算）")
    saved: float = Field(0.0, description="本次节省（¥）")
    cache_status: str = Field("miss", description="hit_prefix | hit_semantic | miss")
    cache_policy: CachePolicy = "semantic"
    trimmed_tokens: int = Field(0, description="上下文裁剪省下的 token")
    fallbacks: int = Field(0, description="触发降级的次数")
    latency_ms: float = 0.0
    ok: bool = True
    error: str | None = None
    ts: str = Field(default_factory=now_str)

    @property
    def saved_percent(self) -> float:
        """节省比例。这是"月度支出下降 X%"的**可复现计算口径**。"""
        if self.baseline_cost <= 0:
            return 0.0
        return round((1 - self.cost / self.baseline_cost) * 100, 2)


# --------------------------------------------------------------------------- #
# 评测
# --------------------------------------------------------------------------- #
class EvalCase(BaseModel):
    """Golden Set 中的一条用例。

    说明：这里刻意**不**存"标准答案全文"，而是存可判定的期望
    （必须命中的关键词、必须拒答、禁止出现的词）—— 因为开放式问答没有唯一答案，
    用"可判定的期望"才能自动化。这是评测集能进 CI 的前提。
    """

    case_id: str
    scene: str = Field(..., description="场景，如 policy_qa / refusal / material_compliance")
    question: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    expect_keywords: list[str] = Field(default_factory=list, description="回答中必须命中的词（任一/全部见 expect_mode）")
    expect_mode: Literal["any", "all"] = "any"
    forbidden_keywords: list[str] = Field(default_factory=list, description="回答中不得出现的词（合规红线）")
    must_refuse: bool = Field(False, description="期望执行拒答（无依据时应拒答）")
    reference: str = Field("", description="参考回答（供 LLM-as-Judge 当锚点用）")
    weight: float = Field(1.0, gt=0)


class CaseResult(BaseModel):
    """单条用例的评测结果。"""

    case_id: str
    scene: str
    passed: bool
    score: float = Field(..., ge=0, le=1)
    hits: list[str] = Field(default_factory=list)
    misses: list[str] = Field(default_factory=list)
    violations: list[str] = Field(default_factory=list, description="命中了禁止词")
    actual_preview: str = ""
    reason: str = ""
    judge_source: str = "rules"


class EvalReport(BaseModel):
    """一次评测运行的报告。"""

    eval_id: str
    scene: str = "all"
    total: int = 0
    passed: int = 0
    failed: int = 0
    score: float = Field(0.0, description="加权通过率 0-1")
    case_results: list[CaseResult] = Field(default_factory=list)
    token_cost: float = 0.0
    duration_ms: float = 0.0
    judge_source: str = "rules"
    created_at: str = Field(default_factory=now_str)

    def failed_cases(self) -> list[CaseResult]:
        return [c for c in self.case_results if not c.passed]

    def to_text(self) -> str:
        lines = [
            f"评测报告 {self.eval_id}｜场景 {self.scene}",
            f"用例 {self.total} 条：通过 {self.passed} / 失败 {self.failed}"
            f"｜加权通过率 {self.score:.1%}｜判分来源 {self.judge_source}",
            f"耗时 {self.duration_ms:.0f} ms｜token 成本 ¥{self.token_cost:.4f}",
        ]
        for c in self.failed_cases():
            lines.append(f"  ✗ {c.case_id}（{c.scene}）{c.reason}")
            if c.misses:
                lines.append(f"      未命中：{c.misses}")
            if c.violations:
                lines.append(f"      违规词：{c.violations}")
            if c.actual_preview:
                lines.append(f"      实际：{c.actual_preview[:120]}")
        return "\n".join(lines)


class GateDecision(BaseModel):
    """CI 准入结论：本次变更能不能发布。"""

    allowed: bool
    reasons: list[str] = Field(default_factory=list)
    current_score: float = 0.0
    baseline_score: float = 0.0
    delta: float = 0.0
    regressions: list[str] = Field(default_factory=list, description="相比基线新失败的用例")


# --------------------------------------------------------------------------- #
# 汇总视图
# --------------------------------------------------------------------------- #
class GatewayStats(BaseModel):
    """网关运行态快照（供 /gateway/stats 与演示脚本使用）。"""

    enabled: bool = False
    calls: int = 0
    cache_hits: int = 0
    cache_hit_rate: float = 0.0
    routing: dict[str, int] = Field(default_factory=dict, description="各档位调用次数")
    total_cost: float = 0.0
    baseline_cost: float = 0.0
    saved: float = 0.0
    saved_percent: float = 0.0
    fallbacks: int = 0
    trimmed_tokens: int = 0
    cache: dict[str, Any] = Field(default_factory=dict)
    ratelimit: dict[str, Any] = Field(default_factory=dict)
    keys: dict[str, Any] = Field(default_factory=dict)
    breaker: dict[str, Any] = Field(default_factory=dict, description="provider 级熔断状态")
