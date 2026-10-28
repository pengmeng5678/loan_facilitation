"""领域模型：产品、客户档案、检索命中、匹配结果、方案书。

这里刻意用 pydantic 显式建模，而不是到处传 dict：
    - 字段有类型、有说明，新人一眼看懂业务含义；
    - 上下游（匹配引擎 / 方案书 / API）共用同一套契约，改字段不会漏改；
    - 可序列化，直接进状态机、进数据库、进 API 响应。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

# 用途（客户资金需求）
Purpose = Literal["资金周转", "债务置换", "负债优化", "经营周转", "装修", "购车"]
# 资质
Qualification = Literal["公积金", "社保", "房产", "车产", "保单", "营业执照", "个税"]
# 客群
OccupationType = Literal["受薪", "自雇", "企业主"]


# =========================================================================== #
# 一、产品
# =========================================================================== #
class Product(BaseModel):
    """银行产品（一条产品目录）。字段即"准入条件 + 要素"，匹配引擎直接消费。"""

    code: str = Field(..., description="产品编号，如 BANK-A-XYD-001")
    name: str = Field(..., description="产品名称")
    bank: str = Field(..., description="所属银行")
    category: str = Field(..., description="品类：信用贷/抵押贷/经营贷/公积金贷/装修贷/车抵贷")

    # ---- 准入条件（硬性筛选依据）----
    purposes: list[str] = Field(default_factory=list, description="支持的用途")
    age_min: int = Field(22, description="年龄下限")
    age_max: int = Field(60, description="年龄上限")
    income_min: float = Field(0.0, description="月收入下限（元）")
    qualifications: list[str] = Field(default_factory=list, description="可用资质，任一满足即可")
    occupations: list[str] = Field(default_factory=list, description="可受理客群，空表示不限")
    credit_query_6m_max: int = Field(99, description="近6个月征信查询次数上限")
    overdue_count_max: int = Field(99, description="近12个月逾期次数上限")
    overdue_days_max: int = Field(999, description="最大逾期天数上限")
    debt_ratio_max: float = Field(1.0, description="负债收入比上限（月负债/月收入）")

    # ---- 产品要素（排序与预估依据）----
    amount_min: float = Field(0.0, description="起贷额度（元）")
    amount_max: float = Field(0.0, description="最高额度（元）")
    rate_min: float = Field(0.0, description="年化利率下限（%）")
    rate_max: float = Field(0.0, description="年化利率上限（%）")
    term_months: list[int] = Field(default_factory=list, description="可选期限（月）")
    quota_multiple: float = Field(0.0, description="额度测算倍数（如公积金基数的 40 倍）")
    quota_base: str = Field("", description="额度测算基数，如 公积金基数 / 月收入 / 房产评估价")

    # ---- 交付信息 ----
    materials: list[str] = Field(default_factory=list, description="所需资料清单")
    process: list[str] = Field(default_factory=list, description="办理流程")

    # ---- 时效（政策月月变，必须带生效日期）----
    effective_date: date = Field(..., description="政策生效日期")
    expire_date: date | None = Field(None, description="失效日期，None 表示仍有效")

    # ---- 图谱关系 ----
    substitutes: list[str] = Field(default_factory=list, description="可替代产品编号（用途相近且准入更宽）")

    def is_active(self, as_of: date) -> bool:
        """判断产品政策在 as_of 日期是否有效。"""
        if as_of < self.effective_date:
            return False
        return self.expire_date is None or as_of < self.expire_date

    def rate_display(self) -> str:
        """利率展示文案（合规：只给区间 + 免责）。"""
        return f"{self.rate_min:.2f}%–{self.rate_max:.2f}%（以银行审批为准）"


# =========================================================================== #
# 二、客户档案（来自项目一智能客服的槽位填充 / 项目四数据中台）
# =========================================================================== #
class CustomerProfile(BaseModel):
    """客户档案 —— 匹配引擎的输入。"""

    customer_id: str
    name_masked: str = Field("", description="脱敏姓名，如 张*生")
    age: int = Field(..., description="年龄")
    city: str = Field("", description="所在城市")
    occupation_type: OccupationType = Field("受薪", description="客群类型")
    monthly_income: float = Field(0.0, description="月收入（元）")
    qualifications: list[str] = Field(default_factory=list, description="客户现有资质")
    credit_query_6m: int = Field(0, description="近6个月征信查询次数")
    overdue_count_12m: int = Field(0, description="近12个月逾期次数")
    overdue_days_max: int = Field(0, description="最大逾期天数")
    monthly_debt: float = Field(0.0, description="月负债合计（含房贷/网贷）")
    loans_count: int = Field(0, description="在用贷款笔数（网贷笔数）")
    needs: list[str] = Field(default_factory=list, description="资金需求用途")
    desired_amount: float = Field(0.0, description="期望额度（元）")
    note: str = Field("", description="其他说明")

    @property
    def debt_ratio(self) -> float:
        """负债收入比。月收入为 0 时返回一个极大值，避免除零并把该客户判为高风险。"""
        if self.monthly_income <= 0:
            return 9.99
        return round(self.monthly_debt / self.monthly_income, 4)


# =========================================================================== #
# 三、检索命中与引用溯源
# =========================================================================== #
class Citation(BaseModel):
    """引用溯源 —— 助贷合规的硬要求：每条产品信息必须能跳回政策原文。"""

    policy_id: str = Field(..., description="政策条款唯一 ID")
    product_code: str = Field("", description="关联产品编号")
    clause_no: str = Field("", description="条款号，如 3.2")
    title: str = Field("", description="条款标题")
    effective_date: date | None = Field(None, description="该条款生效日期")
    source_file: str = Field("", description="源文件路径，供人工核对")
    snippet: str = Field("", description="原文摘录")

    def render(self) -> str:
        """渲染成人类可读的一行引用。"""
        parts = [f"[{self.policy_id}]"]
        if self.product_code:
            parts.append(f"产品 {self.product_code}")
        if self.clause_no:
            parts.append(f"第 {self.clause_no} 条")
        if self.effective_date:
            parts.append(f"生效 {self.effective_date}")
        return " · ".join(parts)


class RetrievedChunk(BaseModel):
    """一条检索结果（父子块里的"子块"命中，携带其父块内容供模型使用）。"""

    chunk_id: str
    text: str = Field(..., description="子块文本（用于精确命中）")
    parent_text: str = Field("", description="父块文本（用于提供完整上下文）")
    score: float = Field(0.0, description="融合后的相关性得分")
    bm25_score: float = 0.0
    dense_score: float = 0.0
    rerank_score: float = 0.0
    citation: Citation = Field(default_factory=lambda: Citation(policy_id=""))
    metadata: dict[str, Any] = Field(default_factory=dict)


# =========================================================================== #
# 四、匹配决策
# =========================================================================== #
class MatchCandidate(BaseModel):
    """一个候选产品的评分明细（可解释性：业务员能看懂为什么推荐它）。"""

    product_code: str
    product_name: str
    bank: str
    score: float = Field(..., description="综合得分（0-1）")
    pass_probability: float = Field(
        0.0,
        exclude=True,
        description="内部排序用的启发式数值（0-1）。**不对外暴露**：它不是银行审批结论",
    )
    pass_band: str = Field(
        "medium", description="通过率分档 high|medium|low（对外口径，不含百分比）"
    )
    pass_band_label: str = Field("中等", description="分档的中文展示名")
    pass_band_reason: str = Field(
        "", description="分档依据 + 免责口径（最终以银行审批为准）"
    )
    estimated_amount: float = Field(0.0, description="预估可贷额度（元）")
    rate_display: str = Field("", description="利率区间文案")
    reasons: list[str] = Field(default_factory=list, description="命中与优势说明")
    risks: list[str] = Field(default_factory=list, description="风险与不满足项提示")
    margin_detail: dict[str, Any] = Field(default_factory=dict, description="各准入条件的余量明细")


class MatchResult(BaseModel):
    """一次方案匹配的结果。"""

    matched: bool = Field(..., description="是否匹配到可用产品")
    customer_id: str = ""
    primary: MatchCandidate | None = Field(None, description="主力产品（防单点被拒必须有备选）")
    alternates: list[MatchCandidate] = Field(default_factory=list, description="备选产品")
    rejected_summary: list[dict[str, Any]] = Field(
        default_factory=list, description="被硬性条件筛掉的产品及原因（可解释、可回测）"
    )
    handoff_reason: str = Field("", description="无匹配时的转人工原因")
    weights: dict[str, float] = Field(default_factory=dict, description="本次排序使用的权重")

    def to_text(self) -> str:
        """渲染成业务员可读的方案摘要。"""
        if not self.matched:
            return f"未匹配到合适产品，建议转人工专家。原因：{self.handoff_reason}"
        lines = []
        if self.primary:
            p = self.primary
            lines.append(f"★ 主力方案：{p.product_name}（{p.bank}）")
            lines.append(f"  预估额度 {p.estimated_amount:,.0f} 元 ｜ 利率 {p.rate_display}")
            lines.append(f"  通过率分档 {p.pass_band_label}（{p.pass_band_reason}）")
            for r in p.reasons:
                lines.append(f"  · {r}")
        for i, alt in enumerate(self.alternates, 1):
            lines.append(f"○ 备选{i}：{alt.product_name}（{alt.bank}） 预估 {alt.estimated_amount:,.0f} 元")
        return "\n".join(lines)


# =========================================================================== #
# 五、方案书
# =========================================================================== #
class Proposal(BaseModel):
    """结构化的方案书（下游可直接渲染成 PDF / 企微卡片，也可被项目八复用）。"""

    proposal_id: str
    customer_id: str
    generated_at: str
    products: list[dict[str, Any]] = Field(default_factory=list)
    amount_range: str = ""
    rate_range: str = ""
    materials: list[str] = Field(default_factory=list)
    process: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    disclaimer: str = Field(
        "以上额度与利率为初步预估，最终以银行审批结果为准；本方案不构成任何放款承诺。"
    )

    def to_markdown(self) -> str:
        """渲染成 markdown 方案书（业务员一键发送客户）。"""
        lines = [
            f"# 助贷方案书（{self.proposal_id}）",
            "",
            f"- 客户：{self.customer_id}",
            f"- 生成时间：{self.generated_at}",
            f"- 预估额度：{self.amount_range}",
            f"- 预估利率：{self.rate_range}",
            "",
            "## 一、推荐产品组合",
        ]
        for p in self.products:
            lines.append(f"- **{p.get('name')}**（{p.get('bank')}）｜额度 {p.get('amount')}｜利率 {p.get('rate')}")
            if p.get("reason"):
                lines.append(f"  - 推荐理由：{p['reason']}")
        lines += ["", "## 二、所需资料"]
        lines += [f"{i}. {m}" for i, m in enumerate(self.materials, 1)] or ["- （以银行要求为准）"]
        lines += ["", "## 三、办理流程"]
        lines += [f"{i}. {s}" for i, s in enumerate(self.process, 1)] or ["- （以银行流程为准）"]
        lines += ["", "## 四、风险提示"]
        lines += [f"- {r}" for r in self.risk_notes]
        if self.citations:
            lines += ["", "## 五、政策依据"]
            lines += [f"- {c}" for c in self.citations]
        lines += ["", "---", f"> {self.disclaimer}"]
        return "\n".join(lines)
