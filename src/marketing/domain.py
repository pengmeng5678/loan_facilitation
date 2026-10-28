"""项目七的领域模型。

一条素材的完整生命周期在这里都能看到：
    Blueprint（高转化结构模板）
      → MaterialDraft（生成初稿，含合规结论与质量分）
        → 业务员选改 → 发布
          → TouchRecord（曝光/咨询/留资/成单）
            → AttributionReport（哪种钩子真的带来线索）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Channel = Literal["moments", "douyin", "xiaohongshu"]
Severity = Literal["high", "medium", "low"]
MaterialStatus = Literal["approved", "sanitized", "rejected", "pending"]
TouchStage = Literal["impression", "consult", "lead", "deal"]

CHANNEL_NAMES: dict[str, str] = {
    "moments": "朋友圈",
    "douyin": "抖音",
    "xiaohongshu": "小红书",
}


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def channel_name(channel: str) -> str:
    return CHANNEL_NAMES.get(channel, channel)


# =========================================================================== #
# 素材结构模板
# =========================================================================== #
class Blueprint(BaseModel):
    """高转化素材的**结构模板**：钩子 → 痛点 → 方案 → CTA。

    为什么要先有结构再让模型填充：
        直接让模型"写一条贷款广告"，它会写得很平（没有钩子、没有痛点、
        不敢给行动指令）。而结构模板把"怎么抓人"这件事固化成可复用资产，
        模型只负责把内容填得好看 —— 这也是业务员自己写不出来的那部分。
    """

    blueprint_id: str
    name: str
    channel: Channel
    theme: str = Field(..., description="主题，如 债务置换 / 资质科普 / 案例见证")
    hook: str = Field("", description="钩子：开头 3 秒/第一句要抓人")
    pain: str = Field("", description="痛点：戳中客户真实处境")
    solution: str = Field("", description="方案：怎么解决")
    cta: str = Field("", description="行动指令：引导咨询")
    tags: list[str] = Field(default_factory=list)
    source: str = Field("template", description="template | case")


# =========================================================================== #
# 合规
# =========================================================================== #
class ComplianceFinding(BaseModel):
    """一条合规发现。"""

    rule_id: str
    category: str = Field(..., description="承诺性话术 / 收益承诺 / 规避审查 / 敏感信息 / 绝对化用语")
    severity: Severity
    matched: str = Field("", description="命中的词/片段")
    excerpt: str = Field("", description="上下文原文")
    suggestion: str = ""
    source: str = Field("rules", description="rules | llm")


class ComplianceReport(BaseModel):
    """合规审核结论（先审后发的依据）。"""

    passed: bool = Field(..., description="是否可以直接发布")
    findings: list[ComplianceFinding] = Field(default_factory=list)
    checked_rules: int = 0
    needs_human: bool = Field(False, description="是否必须转人工复核（命中 high 级风险）")
    max_severity: Severity | None = None
    sanitized_text: str = Field("", description="自动改写后的合规文本（若已改写）")
    sanitized: bool = Field(False)

    def to_text(self) -> str:
        lines = [
            f"合规审核：{'通过' if self.passed else '不通过'}"
            + ("（需人工复核）" if self.needs_human else ""),
            f"检查规则 {self.checked_rules} 条，命中 {len(self.findings)} 条",
        ]
        for f in self.findings:
            lines.append(f"  [{f.severity}] {f.category}·{f.rule_id}：{f.matched} → {f.suggestion}")
        return "\n".join(lines)


# =========================================================================== #
# 素材
# =========================================================================== #
class MaterialDraft(BaseModel):
    """一条素材初稿。"""

    draft_id: str
    material_id: str = Field("", description="入库后的素材编号")
    channel: Channel
    theme: str = ""
    blueprint_id: str = ""
    title: str = ""
    body: str = Field("", description="正文 / 口播稿")
    hashtags: list[str] = Field(default_factory=list)
    cta: str = ""
    status: MaterialStatus = "pending"
    compliance: ComplianceReport | None = None
    quality_score: float = Field(0.0, ge=0, le=1)
    quality_notes: list[str] = Field(default_factory=list)
    source: str = Field("template", description="template | llm | case")
    created_at: str = Field(default_factory=now_str)

    def to_text(self) -> str:
        lines = [
            f"【{channel_name(self.channel)}｜{self.theme}】{self.title}",
            self.body,
        ]
        if self.hashtags:
            lines.append(" ".join(f"#{t}" for t in self.hashtags))
        if self.cta:
            lines.append(f"CTA：{self.cta}")
        lines.append(
            f"— 状态 {self.status}｜质量 {self.quality_score:.2f}｜"
            f"合规 {'通过' if (self.compliance and self.compliance.passed) else '未通过'}"
        )
        return "\n".join(lines)


# =========================================================================== #
# 案例
# =========================================================================== #
class CaseStudy(BaseModel):
    """从脱敏成交对话中提炼出的案例要素。"""

    case_id: str
    source_conversation: str = ""
    customer_masked: str = ""
    scene: str = Field("", description="需求场景，如 网贷多、月供压力大")
    path: str = Field("", description="解决路径，如 公积金置换")
    outcome: str = Field("", description="结果感受")
    quote: str = Field("", description="客户证言原话（已脱敏）")
    tags: list[str] = Field(default_factory=list)
    desensitize_hits: int = 0
    created_at: str = Field(default_factory=now_str)


# =========================================================================== #
# 激活
# =========================================================================== #
class ActivationTask(BaseModel):
    """一条未成单客户的激活任务（业务员一键发送）。"""

    task_id: str
    customer_id: str
    name_masked: str = ""
    dormant_days: int = 0
    trigger: str = Field("", description="触发原因（客户的犹豫点）")
    concern: str = Field("", description="客户关注点")
    channel: Channel = "moments"
    material_draft_id: str = ""
    material_title: str = ""
    suggested_send_at: str = Field("", description="建议发送时间（避开休息时段）")
    priority: float = Field(0.0, description="优先级（意向 × 沉睡程度）")
    reason: str = ""
    status: str = "pending"


class ActivationPlan(BaseModel):
    """一次激活计划。"""

    plan_id: str
    generated: int = 0
    tasks: list[ActivationTask] = Field(default_factory=list)
    skipped: list[dict[str, Any]] = Field(default_factory=list, description="被跳过的客户及原因（防骚扰）")
    stats: dict[str, int] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now_str)

    def to_text(self) -> str:
        lines = [f"激活计划 {self.plan_id}：生成 {self.generated} 条任务，跳过 {len(self.skipped)} 位客户"]
        for t in self.tasks:
            lines.append(
                f"  · {t.name_masked}（沉睡 {t.dormant_days} 天，{t.trigger}）→ "
                f"{channel_name(t.channel)}｜{t.material_title}｜建议 {t.suggested_send_at}"
                f"｜优先级 {t.priority:.2f}"
            )
        if self.skipped:
            lines.append("  跳过原因：" + "；".join(
                f"{s.get('name_masked') or s.get('customer_id')}({s.get('reason')})" for s in self.skipped
            ))
        return "\n".join(lines)


# =========================================================================== #
# 归因
# =========================================================================== #
class TouchRecord(BaseModel):
    """一次素材触达记录（归因的最小数据单元）。"""

    touch_id: str
    material_id: str
    channel: Channel
    customer_id: str = ""
    stage: TouchStage = "impression"
    ts: str = Field(default_factory=now_str)
    meta: dict[str, Any] = Field(default_factory=dict)


class AttributionReport(BaseModel):
    """素材效果归因报告。"""

    total_materials: int = 0
    total_touches: int = 0
    by_channel: dict[str, dict[str, float]] = Field(default_factory=dict)
    top_materials: list[dict[str, Any]] = Field(default_factory=list)
    lead_share: dict[str, float] = Field(default_factory=dict, description="各渠道线索占比")
    conversion_rate: float = Field(0.0, description="素材触达→成单的转化率")
    conclusions: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now_str)

    def to_text(self) -> str:
        lines = [f"素材归因报告：{self.total_materials} 条素材 / {self.total_touches} 次触达"]
        for ch, s in self.by_channel.items():
            # 注意字段名是单数 lead/deal（聚合层用的就是单数），
            # 这里写错会静默显示 0 —— 报表显示 0 但结论里说"留资最多"是最典型的口径事故。
            lines.append(
                f"  {channel_name(ch)}：曝光 {s.get('impression', 0):.0f}"
                f"｜咨询 {s.get('consult', 0):.0f}"
                f"｜留资 {s.get('lead', 0):.0f}｜成单 {s.get('deal', 0):.0f}"
                f"｜线索占比 {self.lead_share.get(ch, 0):.1%}"
            )
        if self.top_materials:
            lines.append("  最佳素材：")
            for m in self.top_materials[:5]:
                lines.append(
                    f"    - {m.get('title', '')[:28]}（{channel_name(str(m.get('channel', '')))}）"
                    f" 留资 {m.get('leads', 0)}"
                )
        lines.extend(f"  结论：{c}" for c in self.conclusions)
        return "\n".join(lines)


# =========================================================================== #
# 汇总
# =========================================================================== #
class MarketingStats(BaseModel):
    """素材推广服务运行态快照。"""

    blueprints: int = 0
    materials: int = 0
    approved: int = 0
    sanitized: int = 0
    rejected: int = 0
    cases: int = 0
    dormant_customers: int = 0
    activation_tasks: int = 0
    blocked_materials: int = Field(0, description="被合规拦截的素材数（对应简历里的'拦截 300+ 条'）")
    touches: int = 0
    channels: list[str] = Field(default_factory=list)
