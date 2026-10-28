"""到店前自动准备（项目八"到店前准备"自动化，对应 40 分钟 → 5 分钟）。

业务员到店前最想要的是**一页纸**：这个客户什么情况、能做什么产品、
大概什么费用、见面先聊哪三句。以前这页纸要手工 40 分钟攒，
现在客户确认到店后由数字员工自动生成。

**三个组成部分与来源**：
    档案汇总    资质/征信/需求摘要       —— 本项目从客户档案读取
    方案初稿    产品匹配 + 方案书摘要     —— 复用项目二（注入 provider，不 import）
    费用预估    服务费 + 放款成本试算     —— 本项目 fees.py（确定性公式）

**对项目二是"调用关系"而不是"重新实现"**：项目二负责"怎么配方案"，
本项目负责"在对的时机（到店前）把它准备好"。面试被问边界时这么答。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from agent_platform.config import Settings
from agent_platform.logging_config import get_logger
from flow_ops.domain import (
    FeeEstimate,
    Lead,
    PlanDraft,
    ProfileSummary,
    ReadinessPack,
)
from flow_ops.fees import FeeEstimator

logger = get_logger(__name__)

#: 方案初稿 provider 的签名：customer_id → 结构化方案摘要
#: 由装配层注入（转调项目二 product_assistant），本项目不直接依赖项目二。
PlanProvider = Callable[[str], dict[str, Any]]


class ReadinessBuilder:
    """到店前准备包构造器。"""

    def __init__(
        self,
        settings: Settings,
        *,
        plan_provider: PlanProvider | None = None,
    ) -> None:
        self._settings = settings
        self._plan_provider = plan_provider
        self.fee_estimator = FeeEstimator(settings)

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def build(self, lead: Lead, profile: ProfileSummary, *, now: float | None = None) -> ReadinessPack:
        """为一条线索生成到店前准备包。"""
        started = time.perf_counter()
        ts = now if now is not None else time.time()

        plan_draft = self._build_plan_draft(lead, profile)
        fee_estimate = self._build_fee_estimate(lead, profile)
        talking_points = self._talking_points(lead, profile, plan_draft)
        warnings = self._warnings(lead, profile, plan_draft, fee_estimate)

        pack = ReadinessPack(
            pack_id=f"pk_{uuid.uuid4().hex[:12]}",
            lead_id=lead.lead_id,
            customer_id=lead.customer_id,
            profile=profile,
            highlights=profile.highlights(),
            risks=profile.risks(),
            plan_draft=plan_draft,
            fee_estimate=fee_estimate,
            talking_points=talking_points,
            warnings=warnings,
            prepared_in_ms=(time.perf_counter() - started) * 1000,
            created_at=ts,
        )
        logger.info(
            "到店前准备包：%s lead=%s 方案=%s 费用=%s 耗时 %.0fms",
            pack.pack_id, lead.lead_id,
            plan_draft.product_code if plan_draft else "无",
            fee_estimate.service_fee if fee_estimate else "无",
            pack.prepared_in_ms,
        )
        return pack

    # ------------------------------------------------------------------ #
    # 三个组成部分
    # ------------------------------------------------------------------ #
    def _build_plan_draft(self, lead: Lead, profile: ProfileSummary) -> PlanDraft | None:
        """调项目二拿方案初稿。provider 缺失或异常时降级为 None + warning。"""
        if self._plan_provider is None:
            return None
        try:
            raw = self._plan_provider(lead.customer_id)
        except Exception as exc:  # noqa: BLE001 - 降级不能炸掉整个准备流程
            logger.warning("方案初稿获取失败（降级为人工匹配）：%s: %s", lead.customer_id, exc)
            return PlanDraft(
                customer_id=lead.customer_id,
                product_code="",
                product_name="",
                summary="",
                handoff_reason=f"方案匹配服务暂不可用：{exc}",
            )
        if not raw:
            return None
        return PlanDraft(
            customer_id=lead.customer_id,
            product_code=raw.get("product_code", ""),
            product_name=raw.get("product_name", ""),
            summary=raw.get("summary", ""),
            citations=list(raw.get("citations", [])),
            rejected=list(raw.get("rejected", [])),
            handoff_reason=raw.get("handoff_reason", ""),
        )

    def _build_fee_estimate(self, lead: Lead, profile: ProfileSummary) -> FeeEstimate | None:
        """费用预估。客户没提额度需求时跳过（不编数字）。"""
        amount = profile.need_amount
        if amount <= 0:
            return None
        return self.fee_estimator.estimate(lead.lead_id, loan_amount=amount)

    # ------------------------------------------------------------------ #
    # 谈资与预警（确定性模板，不依赖模型）
    # ------------------------------------------------------------------ #
    def _talking_points(
        self, lead: Lead, profile: ProfileSummary, plan_draft: PlanDraft | None
    ) -> list[str]:
        """给业务员的"见面先聊这三句"。

        为什么用模板而不是让模型生成：这些话术要**稳定、可审查**。
        模型生成的开场白每次不一样，主管没法审查"我们的顾问到店
        第一句话到底在说什么"。
        """
        points: list[str] = []
        if plan_draft and plan_draft.product_name:
            points.append(
                f"客户适配「{plan_draft.product_name}」，到店可直接按该产品讲解准入条件。"
            )
        elif plan_draft and plan_draft.handoff_reason:
            points.append(f"暂未匹配到产品（{plan_draft.handoff_reason}），到店需专家介入。")
        else:
            points.append("未获取到方案初稿，到店后按档案现场匹配。")

        if profile.credit_query_times_90d > 8:
            points.append(
                f"客户近 90 天征信查询 {profile.credit_query_times_90d} 次，"
                "先安抚再解释查询多的影响，避免一上来就否定。"
            )
        if profile.microloan_count >= 3:
            points.append(
                f"客户有 {profile.microloan_count} 笔网贷，可主推债务置换方案，"
                "把「多头借贷」作为切入点。"
            )
        if profile.need_amount > 0:
            points.append(
                f"客户意向额度 {profile.need_amount / 10_000:.1f} 万，"
                "先确认资金用途与还款来源再谈产品。"
            )
        return points

    def _warnings(
        self,
        lead: Lead,
        profile: ProfileSummary,
        plan_draft: PlanDraft | None,
        fee_estimate: FeeEstimate | None,
    ) -> list[str]:
        """准备包的预警项（到店前必须知道的风险）。"""
        warnings: list[str] = []
        if plan_draft is None:
            warnings.append("方案初稿缺失：请顾问到店后现场匹配")
        elif plan_draft.handoff_reason:
            warnings.append(f"方案匹配未通过：{plan_draft.handoff_reason}")
        if fee_estimate is None:
            warnings.append("客户未提供意向额度，费用预估已跳过")
        if profile.credit_overdue_times > 0:
            warnings.append(
                f"客户征信有 {profile.credit_overdue_times} 次逾期，"
                "费用与准入需人工复核后再对外沟通"
            )
        return warnings

    # ------------------------------------------------------------------ #
    # 档案构造（种子数据 / 项目一 / 项目四的统一入口）
    # ------------------------------------------------------------------ #
    @staticmethod
    def profile_from_dict(raw: dict) -> ProfileSummary:
        """把外部档案（seed / 项目一 / 项目四）适配成本项目的 ProfileSummary。

        外部字段缺失时给安全默认值（0 / False / ""），绝不编造资质 ——
        宁可提示"档案不全"，也不能替客户"补"一个公积金记录。
        """
        return ProfileSummary(
            customer_id=raw.get("customer_id", ""),
            name=raw.get("name", ""),
            city=raw.get("city", ""),
            monthly_income=float(raw.get("monthly_income", 0) or 0),
            social_insurance_months=int(raw.get("social_insurance_months", 0) or 0),
            housing_fund_months=int(raw.get("housing_fund_months", 0) or 0),
            has_property=bool(raw.get("has_property", False)),
            has_vehicle=bool(raw.get("has_vehicle", False)),
            has_policy=bool(raw.get("has_policy", False)),
            has_business_license=bool(raw.get("has_business_license", False)),
            credit_overdue_times=int(raw.get("credit_overdue_times", 0) or 0),
            credit_query_times_90d=int(raw.get("credit_query_times_90d", 0) or 0),
            debt_total=float(raw.get("debt_total", 0) or 0),
            microloan_count=int(raw.get("microloan_count", 0) or 0),
            need_amount=float(raw.get("need_amount", 0) or 0),
            need_purpose=raw.get("need_purpose", ""),
        )
