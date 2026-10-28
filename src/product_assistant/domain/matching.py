"""结构化方案匹配引擎（项目二的核心）。

为什么这块必须用"确定性代码"而不是让 LLM 自由发挥：
    助贷的准入是**硬性条款**（征信查询次数、逾期天数、负债比…），
    判断错一条就是"客户白跑一趟 + 浪费一次征信查询"。
    所以：
        - 硬性准入筛选、排序打分、组合建议  —— 全部确定性（可回测、可解释、可审计）
        - LLM 只负责把结果"讲成人话"（方案书文案）
    这与仓库的整体设计原则一致：业务判据用代码，语言表达用模型。

匹配三步（对应简历里的"硬性筛选 → 多目标排序 → 组合建议"）：
    1) 硬性准入筛选：任一条件不满足即淘汰，并记录淘汰原因（可解释性）
    2) 多目标排序：预估通过率（权重最高）→ 利率 → 额度，权重可配置
    3) 组合建议：主力 + 备选（防单点被拒），无结果则转人工，绝不硬配
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from agent_platform.logging_config import get_logger
from product_assistant.domain.models import (
    CustomerProfile,
    MatchCandidate,
    MatchResult,
    Product,
)

logger = get_logger(__name__)

# 上限字段用到的一个"无限制"哨兵值：>= 该值视为不设限
_UNLIMITED = 99

#: 额度测算基数的"收入类"关键词。命中才允许"基数 × 倍数"，
#: 其余（房产评估价 / 年开票额 …）档案里没有对应字段，只能给区间上界
_INCOME_QUOTA_BASES = ("月收入", "公积金基数", "社保基数", "个税", "工资", "打卡")


class MatchingWeights(BaseModel):
    """多目标排序权重。

    默认把"通过率"放最高（过不了别的都白搭），其次利率（客户最关心），
    最后额度。线上的权重应当用历史成交数据回测确定，这里给出的是初始值。
    """

    pass_probability: float = Field(0.60, ge=0, le=1, description="预估通过率权重")
    rate: float = Field(0.25, ge=0, le=1, description="利率优势权重")
    amount: float = Field(0.15, ge=0, le=1, description="额度满足度权重")

    def normalized(self) -> MatchingWeights:
        total = self.pass_probability + self.rate + self.amount
        if total <= 0:
            return MatchingWeights()
        return MatchingWeights(
            pass_probability=self.pass_probability / total,
            rate=self.rate / total,
            amount=self.amount / total,
        )


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _headroom(limit: float, actual: float) -> float:
    """'余量'归一化：0 表示刚好卡在上限，1 表示完全没用到。

    用于把"离红线的距离"折算成通过率的一个因子。
    """
    if limit <= 0:
        return 1.0
    return _clamp((limit - actual) / limit)


class MatchingEngine:
    """方案匹配引擎。无状态，可安全复用。"""

    def __init__(self, weights: MatchingWeights | None = None, top_k: int = 2) -> None:
        self.weights = (weights or MatchingWeights()).normalized()
        self.top_k = top_k

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def match(
        self,
        profile: CustomerProfile,
        products: list[Product],
        *,
        as_of: date | None = None,
    ) -> MatchResult:
        """执行一次完整匹配。"""
        as_of = as_of or date.today()

        # 只考虑在 as_of 当天有效的产品（政策过期的一律不能推荐）
        active = [p for p in products if p.is_active(as_of)]
        rejected: list[dict] = []

        evaluated: list[tuple[Product, MatchCandidate]] = []
        for product in active:
            ok, reason = self._hard_filter(profile, product)
            if not ok:
                rejected.append(
                    {"product_code": product.code, "product_name": product.name, "reason": reason}
                )
                continue
            evaluated.append((product, self._score(profile, product, max_amounts=None)))

        if not evaluated:
            result = MatchResult(
                matched=False,
                customer_id=profile.customer_id,
                rejected_summary=rejected,
                handoff_reason=(
                    f"共评估 {len(active)} 个有效产品，全部不满足硬性准入条件。"
                    "建议转人工专家复核资质或调整为抵押类方案。"
                ),
                weights=self.weights.model_dump(),
            )
            logger.info("匹配失败：%s", result.handoff_reason)
            return result

        # 利率归一化需要在整个候选集上做 min-max，所以要二次打分
        rates = [p.rate_min for p, _ in evaluated]
        rate_lo, rate_hi = min(rates), max(rates)
        scored = [
            (p, self._score(profile, p, rate_range=(rate_lo, rate_hi)))
            for p, _ in evaluated
        ]
        scored.sort(key=lambda item: item[1].score, reverse=True)

        primary = scored[0][1]
        alternates = [c for _, c in scored[1 : self.top_k]]

        # 若主力之外的备选与主力是"可替代关系"，在理由里点出来（体现图谱价值）
        return MatchResult(
            matched=True,
            customer_id=profile.customer_id,
            primary=primary,
            alternates=alternates,
            rejected_summary=rejected,
            weights=self.weights.model_dump(),
        )

    # ------------------------------------------------------------------ #
    # 第一步：硬性准入筛选
    # ------------------------------------------------------------------ #
    @staticmethod
    def _hard_filter(profile: CustomerProfile, product: Product) -> tuple[bool, str]:
        """返回 (是否通过, 未通过原因)。

        条件是"与"关系：任一不满足即淘汰。
        这些条件在真实业务里对应银行的产品准入大纲。
        """
        if not (product.age_min <= profile.age <= product.age_max):
            return False, f"年龄 {profile.age} 不在受理区间 [{product.age_min}, {product.age_max}]"

        if profile.monthly_income < product.income_min:
            return False, f"月收入 {profile.monthly_income:,.0f} 低于门槛 {product.income_min:,.0f}"

        if product.qualifications and not (set(product.qualifications) & set(profile.qualifications)):
            return False, f"缺少所需资质（需任一：{'/'.join(product.qualifications)}）"

        if product.occupations and profile.occupation_type not in product.occupations:
            return False, f"客群类型 {profile.occupation_type} 不在受理范围 {'/'.join(product.occupations)}"

        if product.credit_query_6m_max < _UNLIMITED and profile.credit_query_6m > product.credit_query_6m_max:
            return False, (
                f"近6月征信查询 {profile.credit_query_6m} 次 > 上限 {product.credit_query_6m_max} 次"
            )

        if product.overdue_count_max < _UNLIMITED and profile.overdue_count_12m > product.overdue_count_max:
            return False, (
                f"近12月逾期 {profile.overdue_count_12m} 次 > 上限 {product.overdue_count_max} 次"
            )

        if profile.overdue_days_max > product.overdue_days_max:
            return False, f"最大逾期 {profile.overdue_days_max} 天 > 上限 {product.overdue_days_max} 天"

        if profile.debt_ratio > product.debt_ratio_max:
            return False, f"负债收入比 {profile.debt_ratio:.2f} > 上限 {product.debt_ratio_max:.2f}"

        if product.purposes and not (set(product.purposes) & set(profile.needs)):
            return False, f"用途不匹配（产品支持 {'/'.join(product.purposes)}，客户需求 {'/'.join(profile.needs) or '未填'}）"

        return True, ""

    # ------------------------------------------------------------------ #
    # 第二步：打分
    # ------------------------------------------------------------------ #
    def _score(
        self,
        profile: CustomerProfile,
        product: Product,
        *,
        rate_range: tuple[float, float] | None = None,
        max_amounts: None = None,
    ) -> MatchCandidate:
        """给一个已通过硬筛的产品打分，并生成可解释的理由与风险提示。"""
        pass_probability, margins = self._estimate_pass_probability(profile, product)
        band, band_label, band_reason = self._pass_band(margins)
        estimated_amount = self._estimate_amount(profile, product)

        # 利率得分：利率越低越好；候选集内做 min-max 归一
        if rate_range and rate_range[1] > rate_range[0]:
            rate_score = 1.0 - (product.rate_min - rate_range[0]) / (rate_range[1] - rate_range[0])
        else:
            rate_score = 0.8
        rate_score = _clamp(rate_score)

        # 额度得分：满足客户期望额度的程度
        if profile.desired_amount > 0:
            amount_score = _clamp(estimated_amount / profile.desired_amount)
        else:
            amount_score = _clamp(estimated_amount / product.amount_max) if product.amount_max else 0.5

        w = self.weights
        score = (
            w.pass_probability * pass_probability
            + w.rate * rate_score
            + w.amount * amount_score
        )

        reasons = self._build_reasons(profile, product, band_label)
        risks = self._build_risks(profile, product)

        return MatchCandidate(
            product_code=product.code,
            product_name=product.name,
            bank=product.bank,
            score=round(score, 4),
            pass_probability=round(pass_probability, 4),
            pass_band=band,
            pass_band_label=band_label,
            pass_band_reason=band_reason,
            estimated_amount=estimated_amount,
            rate_display=product.rate_display(),
            reasons=reasons,
            risks=risks,
            margin_detail=margins,
        )

    @staticmethod
    def _pass_band(margins: dict[str, float]) -> tuple[str, str, str]:
        """把内部启发式数值折算成对外分档（高/中/低）。

        **为什么不对外给百分比**：`0.55 + 0.40*avg` 只是一个基于准入余量的
        启发式映射，没有任何历史审批数据校准。业务员拿"78%"去跟客户沟通，
        等于给出一个无法兑现的承诺（客户被拒就是投诉/纠纷）。
        对外只说分档与依据，并明确"最终以银行审批为准"。
        内部数值仍保留用于排序（见 pass_probability 的 exclude 设置）。
        """
        avg = sum(margins.values()) / len(margins) if margins else 0.0
        if avg >= 0.30:
            return (
                "high",
                "较高",
                "资质与准入条件匹配度较高；最终以银行实际审批为准，不构成放款承诺",
            )
        if avg >= 0.10:
            return (
                "medium",
                "中等",
                "基本符合准入条件，补充材料后由银行审批；最终以银行实际审批为准",
            )
        return (
            "low",
            "偏低",
            "部分准入条件余量不足，建议先优化负债与征信查询；最终以银行实际审批为准",
        )

    @staticmethod
    def _estimate_pass_probability(
        profile: CustomerProfile, product: Product
    ) -> tuple[float, dict[str, float]]:
        """预估通过率（启发式）。

        **重要说明**：这是基于"离准入红线还有多少余量"的启发式估计，
        不是银行真实审批模型。上线后应当用历史放款/拒件数据训练一个
        校准模型替换它 —— 保留同一个函数签名即可（这是设计上的替换点）。
        """
        margins: dict[str, float] = {}

        if product.credit_query_6m_max < _UNLIMITED:
            margins["征信查询余量"] = round(
                _headroom(product.credit_query_6m_max, profile.credit_query_6m), 3
            )
        if product.overdue_count_max < _UNLIMITED:
            margins["逾期次数余量"] = round(
                _headroom(product.overdue_count_max, profile.overdue_count_12m), 3
            )
        margins["逾期天数余量"] = round(
            _headroom(max(product.overdue_days_max, 1), profile.overdue_days_max), 3
        )
        margins["负债比余量"] = round(
            _headroom(product.debt_ratio_max, profile.debt_ratio), 3
        )
        if product.income_min > 0:
            margins["收入余量"] = round(
                _headroom(product.income_min * 3, profile.monthly_income), 3
            )
        # 网贷笔数越多，通过率越低（经验规则）
        margins["网贷笔数惩罚"] = round(_clamp(1 - profile.loans_count / 10), 3)

        avg = sum(margins.values()) / len(margins) if margins else 0.5
        # 已经通过硬筛 → 基础通过率给 0.55，再按余量抬升，封顶 0.95
        probability = _clamp(0.55 + 0.40 * avg, 0.0, 0.95)
        return probability, margins

    @staticmethod
    def _estimate_amount(profile: CustomerProfile, product: Product) -> float:
        """预估可贷额度 = min(产品上限, 客户期望, 基数 × 倍数)。

        **只有"以收入为基数"的产品才做倍数测算。**
        抵押类的基数是房产/车辆评估价、经营类的基数是年开票额 —— 这些字段
        不在客户档案里。拿月收入去硬乘 0.7（本意的抵押率）会算出
        "房产抵押贷 2.4 万"这种离谱数字，业务员拿去跟客户谈就是事故。
        宁可给到产品上限与客户期望的较小值，并明确标注"以评估价为准"。
        """
        caps = [product.amount_max]
        if profile.desired_amount > 0:
            caps.append(profile.desired_amount)
        base = product.quota_base or ""
        income_based = (not base) or any(key in base for key in _INCOME_QUOTA_BASES)
        if income_based and product.quota_multiple > 0:
            # 额度测算：以月收入近似替代"公积金基数 / 个税基数"等基数
            caps.append(profile.monthly_income * product.quota_multiple)
        amount = min(caps)
        return float(round(max(0.0, amount), 0))

    @staticmethod
    def _build_reasons(
        profile: CustomerProfile, product: Product, band_label: str
    ) -> list[str]:
        """生成"为什么推荐它"的说明 —— 业务员要能用它说服客户。"""
        reasons: list[str] = []
        hit = set(product.qualifications) & set(profile.qualifications)
        if hit:
            reasons.append(f"资质匹配：{'/'.join(sorted(hit))}满足该产品准入")
        if product.credit_query_6m_max < _UNLIMITED:
            reasons.append(
                f"征信查询 {profile.credit_query_6m} 次 ≤ 上限 {product.credit_query_6m_max} 次（余量充足）"
            )
        if product.debt_ratio_max < 1.0:
            reasons.append(
                f"负债收入比 {profile.debt_ratio:.2f} ≤ 上限 {product.debt_ratio_max:.2f}"
            )
        reasons.append(f"通过率分档 {band_label}（基于准入余量的启发式分档，最终以银行审批为准）")
        if product.rate_min <= 4.0:
            reasons.append(f"利率有优势：{product.rate_display()}")
        return reasons

    @staticmethod
    def _build_risks(profile: CustomerProfile, product: Product) -> list[str]:
        """生成风险提示 —— 让业务员提前管理客户预期，避免到店后翻车。"""
        risks: list[str] = []
        if profile.loans_count >= 4:
            risks.append(f"在用贷款 {profile.loans_count} 笔偏多，建议优先结清 1–2 笔小额网贷")
        if product.debt_ratio_max < 1.0 and profile.debt_ratio > product.debt_ratio_max * 0.85:
            risks.append("负债收入比接近上限，建议先做负债置换以降低月供")
        if profile.overdue_count_12m > 0:
            risks.append(f"近12月有 {profile.overdue_count_12m} 次逾期，需准备逾期说明材料")
        if profile.credit_query_6m >= 6:
            risks.append("近期征信查询偏多，建议 1 个月内不再新增查询")
        base = product.quota_base or ""
        if base and not any(key in base for key in _INCOME_QUOTA_BASES):
            # 额度基数是资产/流水而不是收入：档案里没有这个数，
            # 预估额度只是"上限与期望取小"，必须让业务员知道要现场核价
            risks.append(f"额度以{base}为准，需评估/核验后再向客户确认")
        if not risks:
            risks.append("暂无明显风险项，仍需以银行实际审批为准")
        return risks
