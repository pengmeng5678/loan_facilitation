"""方案书生成（项目二的"最后一公里"）。

设计要点：
    1. **结构化优先**：先用确定性代码把方案书对象拼出来（额度、利率、资料、流程、
       风险提示都来自产品目录，可核对、可统计、可 A/B），LLM 只在需要时润色话术；
    2. **合规兜底**：所有预估都带"以银行审批为准"，数据里绝不能出现"包过/保证"；
    3. **可复用**：输出的是 Proposal 对象，项目八（流程数字员工）在客户到店前
       直接调用它生成方案初稿 —— 这就是"装配式"的价值。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from agent_platform.llm.base import LLMProvider, Message
from agent_platform.logging_config import get_logger
from product_assistant.domain.models import (
    CustomerProfile,
    MatchResult,
    Product,
    Proposal,
)

logger = get_logger(__name__)


class ProposalBuilder:
    """方案书构建器。"""

    def __init__(self, llm: LLMProvider | None = None) -> None:
        self.llm = llm

    def build(
        self,
        profile: CustomerProfile,
        match: MatchResult,
        product_map: dict[str, Product],
    ) -> Proposal:
        """根据匹配结果生成方案书。"""
        products_payload: list[dict] = []
        materials: list[str] = []
        process: list[str] = []
        citations: list[str] = []

        candidates = []
        if match.primary:
            candidates.append(match.primary)
        candidates.extend(match.alternates)

        amounts: list[float] = []
        rates: list[tuple[float, float]] = []

        for cand in candidates:
            product = product_map.get(cand.product_code)
            if product is None:
                continue
            products_payload.append(
                {
                    "code": product.code,
                    "name": product.name,
                    "bank": product.bank,
                    "amount": f"{cand.estimated_amount:,.0f} 元（预估）",
                    "rate": product.rate_display(),
                    "reason": cand.reasons[0] if cand.reasons else "",
                }
            )
            amounts.append(cand.estimated_amount)
            rates.append((product.rate_min, product.rate_max))
            # 资料与流程取并集（去重保序）
            for m in product.materials:
                if m not in materials:
                    materials.append(m)
            for s in product.process:
                if s not in process:
                    process.append(s)

        amount_range = (
            f"{min(amounts):,.0f} – {max(amounts):,.0f} 元（预估，最终以银行审批为准）"
            if amounts
            else "待评估"
        )
        rate_range = (
            f"{min(r[0] for r in rates):.2f}% – {max(r[1] for r in rates):.2f}%（预估，以银行审批为准）"
            if rates
            else "待评估"
        )

        risk_notes = self._risk_notes(profile, match)

        proposal = Proposal(
            proposal_id=f"PR-{uuid.uuid4().hex[:8].upper()}",
            customer_id=profile.customer_id,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            products=products_payload,
            amount_range=amount_range,
            rate_range=rate_range,
            materials=materials or ["身份证", "征信报告", "银行要求的其他材料"],
            process=process or ["提交资料", "银行初审", "面签", "放款"],
            risk_notes=risk_notes,
            citations=citations,
        )

        # 只有在接了真实模型时才做话术润色（离线模式下保持确定性输出）
        if self.llm is not None and getattr(self.llm, "name", "echo") != "echo":
            proposal.risk_notes = self._polish(proposal, profile)
        return proposal

    @staticmethod
    def _risk_notes(profile: CustomerProfile, match: MatchResult) -> list[str]:
        """汇总风险提示：来自候选产品的风险项 + 通用合规提示。"""
        notes: list[str] = []
        if match.primary:
            notes.extend(match.primary.risks)
        notes.append("本方案为初步匹配结果，最终额度、利率、期限均以银行审批为准。")
        notes.append("办理过程中请勿新增其他贷款申请，避免征信查询次数增加影响审批。")
        if profile.overdue_count_12m > 0:
            notes.append("存在历史逾期记录，需提前准备情况说明，可能影响审批结果。")
        # 去重保序
        seen: set[str] = set()
        deduped = []
        for n in notes:
            if n not in seen:
                seen.add(n)
                deduped.append(n)
        return deduped

    def _polish(self, proposal: Proposal, profile: CustomerProfile) -> list[str]:
        """用 LLM 把风险提示改写成更贴合客户的话术（可选增强）。"""
        try:
            prompt = (
                "你是助贷合规顾问。请把下面的风险提示改写成对客户友好、但绝不承诺放款的表述，"
                "每条一行，不要新增任何未经证实的信息：\n" + "\n".join(proposal.risk_notes)
            )
            resp = self.llm.complete([Message(role="user", content=prompt)])
            lines = [ln.strip("-· ").strip() for ln in resp.content.splitlines() if ln.strip()]
            return lines or proposal.risk_notes
        except Exception as exc:  # noqa: BLE001 - 润色失败必须降级，不能影响主流程
            logger.warning("方案书风险提示润色失败（已降级为模板文案）：%s", exc)
            return proposal.risk_notes
