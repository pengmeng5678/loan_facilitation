"""项目三的领域模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Role = Literal["business", "customer", "system"]
Severity = Literal["high", "medium", "low"]
Dimension = Literal["合规红线", "需求挖掘", "异议处理", "邀约促成"]

# 四维评分的默认权重。
# 为什么合规权重最高且是一票否决：助贷强监管，一次违规承诺可能引发监管处罚，
# 而"能力"是可以慢慢练的 —— 能力不足扣分，合规越线直接判不合格。
DIMENSION_WEIGHTS: dict[str, float] = {
    "合规红线": 0.35,
    "需求挖掘": 0.25,
    "异议处理": 0.20,
    "邀约促成": 0.20,
}


# =========================================================================== #
# 客户画像
# =========================================================================== #
class Persona(BaseModel):
    """陪练用的虚拟客户画像（来自数据中台的真实客户特征）。"""

    persona_id: str
    name: str
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    scene: str = Field("首触", description="首触 / 邀约 / 异议处理 / 征信解释")
    profile: str = Field("", description="客户背景描述")
    resistance: list[str] = Field(default_factory=list, description="抗性表现：会怎么'难缠'")
    common_objections: list[str] = Field(default_factory=list, description="常见异议")
    customer_id: str = Field("", description="关联的真实客户档案编号（用于取资质/征信事实）")


# =========================================================================== #
# 会话
# =========================================================================== #
class SessionMessage(BaseModel):
    """一条陪练对话消息。"""

    role: Role
    text: str
    ts: float = 0.0  # 相对秒数，用于计算响应时效

    def is_business(self) -> bool:
        return self.role == "business"


class RoleplaySession(BaseModel):
    """一次陪练会话。"""

    session_id: str
    persona: Persona
    trainee: str = "unknown"
    status: Literal["active", "finished"] = "active"
    messages: list[SessionMessage] = Field(default_factory=list)
    turn: int = 0
    created_at: str = ""
    finished_at: str | None = None

    @property
    def business_messages(self) -> list[SessionMessage]:
        return [m for m in self.messages if m.role == "business"]

    @property
    def customer_messages(self) -> list[SessionMessage]:
        return [m for m in self.messages if m.role == "customer"]


# =========================================================================== #
# 评分
# =========================================================================== #
class DimensionScore(BaseModel):
    """单个维度的评分。"""

    dimension: str
    score: float = Field(..., ge=0, le=1, description="0-1")
    weight: float
    passed: bool
    evidence: list[str] = Field(default_factory=list, description="支撑该评分的原文证据")
    notes: str = ""


class Violation(BaseModel):
    """一条违规记录（合规红线）。"""

    keyword: str
    excerpt: str
    message_index: int
    severity: Severity = "high"
    rule_id: str = ""


class ScoreCard(BaseModel):
    """一次陪练的评分卡。"""

    session_id: str
    persona_id: str
    trainee: str
    total_score: float = Field(..., description="0-100")
    passed: bool
    vetoed: bool = Field(False, description="是否因合规红线一票否决")
    dimensions: list[DimensionScore] = Field(default_factory=list)
    violations: list[Violation] = Field(default_factory=list)
    weak_points: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    judge_source: str = Field("rules", description="rules（确定性 rubric）| llm（模型判分）| blended")
    degraded: bool = Field(
        False,
        description="LLM Judge 输出异常（乱码/缺维度/越界），相关维度已回退规则分",
    )
    created_at: str = ""

    def to_text(self) -> str:
        lines = [
            f"评分卡 {self.session_id}｜学员 {self.trainee}｜客户 {self.persona_id}",
            f"总分 {self.total_score:.1f}/100 ｜ {'通过' if self.passed else '不通过'}"
            + ("（合规一票否决）" if self.vetoed else ""),
            f"判分来源：{self.judge_source}",
            "",
            "【四维得分】",
        ]
        for d in self.dimensions:
            flag = "✓" if d.passed else "✗"
            lines.append(f"  {flag} {d.dimension}: {d.score:.2f}（权重 {d.weight:.0%}）{d.notes}")
        if self.violations:
            lines.append("\n【违规记录】")
            for v in self.violations:
                lines.append(f"  ! {v.keyword} → {v.excerpt}")
        if self.weak_points:
            lines.append("\n【薄弱点】")
            lines.extend(f"  - {w}" for w in self.weak_points)
        if self.recommendations:
            lines.append("\n【改进建议】")
            lines.extend(f"  → {r}" for r in self.recommendations)
        return "\n".join(lines)


# =========================================================================== #
# 质检
# =========================================================================== #
class QCFinding(BaseModel):
    """一条质检发现。"""

    rule_id: str
    category: str = Field(..., description="违规承诺 / 敏感信息 / SOP缺失 / 响应时效")
    severity: Severity
    message_index: int
    excerpt: str
    detail: str = ""
    suggestion: str = ""
    # 稳定标识：LLM 精判按此回传结论。没有它就只能用 excerpt 当 key，
    # 一旦模型改写原文/截断，复核结果就会全部失配（历史 bug）。
    finding_id: str = ""


class QCReport(BaseModel):
    """一次会话质检报告。"""

    conversation_id: str
    agent: str = ""
    score: float = Field(100.0, description="100 分制，按发现扣分")
    findings: list[QCFinding] = Field(default_factory=list)
    checked_rules: int = 0
    stats: dict[str, int] = Field(default_factory=dict)
    needs_alert: bool = Field(False, description="是否存在 high 级别问题（需实时告警主管）")
    created_at: str = ""

    def to_text(self) -> str:
        lines = [
            f"质检报告 {self.conversation_id}｜业务员 {self.agent}｜得分 {self.score:.0f}/100",
            f"检查规则 {self.checked_rules} 条，命中 {len(self.findings)} 条发现"
            + ("（含高风险，需告警）" if self.needs_alert else ""),
        ]
        for f in self.findings:
            lines.append(f"  [{f.severity}] {f.category}·{f.rule_id}：{f.detail}")
            lines.append(f"       原文：{f.excerpt}")
            if f.suggestion:
                lines.append(f"       建议：{f.suggestion}")
        return "\n".join(lines)


# =========================================================================== #
# 话术挖掘
# =========================================================================== #
class MinedPhrase(BaseModel):
    """从成交对话中挖掘出的高价值话术。"""

    text: str
    score: float = Field(..., description="0-1，话术价值分")
    stage: str = Field("", description="所处阶段：资质问询 / 合规说明 / 异议处理 / 邀约")
    reason: str = ""
    source_conversation: str = ""
    customer_next_reaction: str = Field("", description="客户下一句反应（正向则加分）")


class MiningResult(BaseModel):
    """一次话术挖掘的整体结果。"""

    scanned: int = 0
    mined: list[MinedPhrase] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
