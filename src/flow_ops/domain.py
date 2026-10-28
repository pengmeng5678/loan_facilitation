"""项目八（助贷业务流程数字员工）的领域模型。

**业务边界先讲清楚**（这是本项目存在的前提，不是补充说明）：

    自动化范围（Agent 做）：咨询应答 → 邀约 → 方案初稿生成 → 费用预估 → 提醒/留痕
    人工节点  （Agent 不做）：接待客户 / 谈判方案 / 确认费用 / 签约

所以模型分成三组：
    ① 流程主线    Lead / StageTransition / Appointment —— 状态机驱动的线索生命周期
    ② 自动化产物  ReadinessPack / FeeEstimate / SopAlert —— 到店前准备与 SOP 兜底
    ③ 人机边界    HumanStage / ApprovalRequest / AuditEntry —— 高风险审批与全程留痕

所有模型都提供 to_dict()，因为：审计要能一条条导出来、API 要能直接返回、
日志要能结构化检索。把"可序列化"做成默认而不是可选项。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


def _new_id(prefix: str) -> str:
    """可读 + 唯一的 ID：前缀 + 时间 base36 + 随机 6 位。

    为什么要可读：审计日志里出现 `ld_9f2a1c` 比 `uuid4()` 的一长串
    更便于业务员与主管口头沟通（"那条 ld_9f2a1c 的处理一下"）。
    """
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# =========================================================================== #
# 一、流程主线：线索状态机
# =========================================================================== #
class LeadStage(StrEnum):
    """线索阶段。**顺序即业务顺序**，流转合法性由状态机校验。

    阶段划分依据（对应简历里的 SOP）：
        线索分配 → 首呼 → 加微 → 跟进 → 邀约 → 到店确认 → 方案准备 → 费用评估 → 签约
    """

    NEW = "new"  # 刚录入，未分配
    ASSIGNED = "assigned"  # 已分配业务员，等待首呼
    FIRST_CALLED = "first_called"  # 首呼完成
    WECHAT_ADDED = "wechat_added"  # 已加微信
    NURTURING = "nurturing"  # 跟进培育中
    INVITED = "invited"  # 已发出到店邀约
    ARRIVED = "arrived"  # 客户已到店（此后进入人工节点）
    PROPOSAL_READY = "proposal_ready"  # 方案初稿已就绪（自动化产物）
    FEE_ESTIMATED = "fee_estimated"  # 费用预估已就绪（自动化产物）
    SIGNED = "signed"  # 已签约（人工成交）
    LOST = "lost"  # 已流失

    @property
    def is_terminal(self) -> bool:
        return self in (LeadStage.SIGNED, LeadStage.LOST)

    @property
    def needs_human(self) -> bool:
        """该阶段是否已进入人工环节（ARRIVED 及之后）。"""
        return self in (
            LeadStage.ARRIVED,
            LeadStage.PROPOSAL_READY,
            LeadStage.FEE_ESTIMATED,
            LeadStage.SIGNED,
        )


class HumanStage(StrEnum):
    """人工节点枚举 —— Agent 的**禁区**。

    这三个节点 Agent 只允许做两件事：标记"待人工"、把材料准备齐。
    任何"替客户做决定/替业务员承诺"的动作都不允许出现在这些阶段。
    """

    RECEPTION = "reception"  # 接待
    NEGOTIATION = "negotiation"  # 谈判
    FEE_CONFIRMATION = "fee_confirmation"  # 费用确认

    @property
    def label(self) -> str:
        return {"reception": "接待", "negotiation": "谈判", "fee_confirmation": "费用确认"}[self.value]


@dataclass
class Lead:
    """一条线索。对应 CRM 里的客户跟进记录。"""

    lead_id: str
    customer_id: str
    customer_name: str
    phone_masked: str
    source: str  # 留资渠道：douyin / infoflow / referral / walkin
    owner: str  # 负责业务员
    stage: LeadStage = LeadStage.NEW
    stage_entered_at: float = 0.0
    created_at: float = 0.0
    updated_at: float = 0.0
    human_stage: HumanStage | None = None  # 当前挂起的人工节点
    lost_reason: str = ""
    note: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        customer_id: str,
        customer_name: str,
        phone_masked: str,
        source: str,
        owner: str,
        now: float | None = None,
        note: str = "",
    ) -> Lead:
        ts = now if now is not None else time.time()
        return cls(
            lead_id=_new_id("ld"),
            customer_id=customer_id,
            customer_name=customer_name,
            phone_masked=phone_masked,
            source=source,
            owner=owner,
            stage=LeadStage.NEW,
            stage_entered_at=ts,
            created_at=ts,
            updated_at=ts,
            note=note,
        )

    def to_dict(self, *, include_payload: bool = True) -> dict[str, Any]:
        return {
            "lead_id": self.lead_id,
            "customer_id": self.customer_id,
            "customer_name": self.customer_name,
            "phone_masked": self.phone_masked,
            "source": self.source,
            "owner": self.owner,
            "stage": self.stage.value,
            "human_stage": self.human_stage.value if self.human_stage else "",
            "lost_reason": self.lost_reason,
            "note": self.note,
            "stage_entered_at": self.stage_entered_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "payload": dict(self.payload) if include_payload else {},
        }


@dataclass
class StageTransition:
    """一次阶段流转。审计与"进度自助查询"的时间线都来自它。"""

    transition_id: str
    lead_id: str
    from_stage: str
    to_stage: str
    actor: str  # 谁推进的：agent / 业务员 ID / system
    reason: str
    ts: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "lead_id": self.lead_id,
            "from_stage": self.from_stage,
            "to_stage": self.to_stage,
            "actor": self.actor,
            "reason": self.reason,
            "ts": self.ts,
        }


@dataclass
class Appointment:
    """到店邀约。幂等键 = lead_id + 时间段（同一客户同一时段重复邀约只保留一条）。"""

    appointment_id: str
    lead_id: str
    store: str
    scheduled_at: float
    status: str = "booked"  # booked / confirmed / cancelled / arrived / no_show
    note: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "appointment_id": self.appointment_id,
            "lead_id": self.lead_id,
            "store": self.store,
            "scheduled_at": self.scheduled_at,
            "status": self.status,
            "note": self.note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# =========================================================================== #
# 二、SOP 规则与预警
# =========================================================================== #
@dataclass(frozen=True)
class SopRule:
    """一条 SOP 规则：进入某阶段后，多久内必须完成某个动作。

    漏跟进的判定全部来自这张表 —— 判据是**确定性代码**，不靠模型"觉得该跟进了"。
    """

    stage: LeadStage
    action: str  # first_call / add_wechat / follow_up / confirm_arrival
    sla_minutes: int
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "action": self.action,
            "sla_minutes": self.sla_minutes,
            "description": self.description,
        }


@dataclass
class SopAlert:
    """SOP 逾期预警。acknowledged 之前会反复出现在主管的待办里。"""

    alert_id: str
    lead_id: str
    customer_name: str
    owner: str
    stage: str
    action: str
    overdue_minutes: int
    created_at: float
    acknowledged: bool = False
    acknowledged_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "lead_id": self.lead_id,
            "customer_name": self.customer_name,
            "owner": self.owner,
            "stage": self.stage,
            "action": self.action,
            "overdue_minutes": self.overdue_minutes,
            "created_at": self.created_at,
            "acknowledged": self.acknowledged,
            "acknowledged_by": self.acknowledged_by,
        }


# =========================================================================== #
# 三、自动化产物：到店前准备与费用预估
# =========================================================================== #
@dataclass
class ProfileSummary:
    """客户档案摘要（到店前准备的输入）。

    字段对应公司真实的资质初审口径：公积金 / 社保 / 房 / 车 / 保单 / 执照。
    """

    customer_id: str
    name: str
    city: str = ""
    monthly_income: float = 0.0
    social_insurance_months: int = 0
    housing_fund_months: int = 0
    has_property: bool = False
    has_vehicle: bool = False
    has_policy: bool = False
    has_business_license: bool = False
    credit_overdue_times: int = 0
    credit_query_times_90d: int = 0
    debt_total: float = 0.0
    microloan_count: int = 0
    need_amount: float = 0.0
    need_purpose: str = ""

    @classmethod
    def from_customer_profile(cls, p: Any) -> "ProfileSummary":
        """把项目二的 CustomerProfile 映射成流程侧档案摘要。

        映射口径单点维护：装配层（真实请求）与种子脚本（演示数据）共用，
        避免两处各写一遍后口径漂移。入参用鸭子类型，不 import 项目二，
        保持流程包对业务包的单向依赖。

        口径差异：项目二只有 credit_query_6m（6 个月查询次数），流程侧要
        90 天 —— 这里用 6 个月值近似（偏保守，宁可多拦），字段名带 90d
        就是为了让这个差异在代码里看得见。
        """
        quals = set(getattr(p, "qualifications", []) or [])
        return cls(
            customer_id=p.customer_id,
            name=getattr(p, "name_masked", ""),
            city=getattr(p, "city", ""),
            monthly_income=float(getattr(p, "monthly_income", 0.0) or 0.0),
            # 项目二档案不区分缴纳月份，置 0 并靠 highlights/资质标签兜底
            social_insurance_months=0,
            housing_fund_months=0,
            has_property="房产" in quals or "房" in quals,
            has_vehicle="车辆" in quals or "车" in quals,
            has_policy="保单" in quals,
            has_business_license="营业执照" in quals or "执照" in quals,
            credit_overdue_times=int(getattr(p, "overdue_count_12m", 0) or 0),
            credit_query_times_90d=int(getattr(p, "credit_query_6m", 0) or 0),
            debt_total=float(getattr(p, "monthly_debt", 0.0) or 0.0),
            microloan_count=int(getattr(p, "loans_count", 0) or 0),
            need_amount=float(getattr(p, "desired_amount", 0.0) or 0.0),
            need_purpose="、".join(getattr(p, "needs", []) or []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "customer_id": self.customer_id,
            "name": self.name,
            "city": self.city,
            "monthly_income": self.monthly_income,
            "social_insurance_months": self.social_insurance_months,
            "housing_fund_months": self.housing_fund_months,
            "has_property": self.has_property,
            "has_vehicle": self.has_vehicle,
            "has_policy": self.has_policy,
            "has_business_license": self.has_business_license,
            "credit_overdue_times": self.credit_overdue_times,
            "credit_query_times_90d": self.credit_query_times_90d,
            "debt_total": self.debt_total,
            "microloan_count": self.microloan_count,
            "need_amount": self.need_amount,
            "need_purpose": self.need_purpose,
        }

    def highlights(self) -> list[str]:
        """档案亮点（给业务员到店前 30 秒扫读用）。"""
        items: list[str] = []
        if self.housing_fund_months >= 12:
            items.append(f"公积金连续 {self.housing_fund_months} 个月")
        if self.social_insurance_months >= 12:
            items.append(f"社保连续 {self.social_insurance_months} 个月")
        if self.has_property:
            items.append("名下有房")
        if self.has_vehicle:
            items.append("名下有车")
        if self.has_policy:
            items.append("有生效保单")
        if self.has_business_license:
            items.append("有营业执照")
        if self.credit_overdue_times == 0:
            items.append("征信无逾期")
        if self.credit_query_times_90d <= 6:
            items.append("近 90 天查询少")
        return items

    def risks(self) -> list[str]:
        """档案风险点（到店前必须知道，否则现场被动）。"""
        items: list[str] = []
        if self.credit_overdue_times > 0:
            items.append(f"征信逾期 {self.credit_overdue_times} 次")
        if self.credit_query_times_90d > 8:
            items.append(f"近 90 天查询 {self.credit_query_times_90d} 次偏多")
        if self.microloan_count >= 3:
            items.append(f"网贷笔数 {self.microloan_count} 笔偏多")
        if self.debt_total > self.monthly_income * 36:
            items.append("总负债超过月收入 36 倍")
        return items


@dataclass
class FeeEstimate:
    """费用预估（试算口径，**不是报价**）。

    两个组成部分：
        服务费 = 放款金额 × 服务费率（分档）—— 公司收入
        放款成本 = 等额本息利息试算            —— 客户付出
    两者都必须带口径说明：预估 ≠ 成交，以银行审批与合同为准。
    """

    lead_id: str
    loan_amount: float
    months: int
    annual_rate: float
    monthly_payment: float
    total_interest: float
    total_repayment: float
    service_fee_rate: float
    service_fee: float
    net_amount: float  # 放款金额 - 服务费（客户到手）
    basis: str = ""  # 试算依据（利率来源、费率档位）
    disclaimer: str = "预估口径，非最终报价；以银行审批结果与签署合同为准。"

    def to_dict(self) -> dict[str, Any]:
        return {
            "lead_id": self.lead_id,
            "loan_amount": round(self.loan_amount, 2),
            "months": self.months,
            "annual_rate": self.annual_rate,
            "monthly_payment": round(self.monthly_payment, 2),
            "total_interest": round(self.total_interest, 2),
            "total_repayment": round(self.total_repayment, 2),
            "service_fee_rate": self.service_fee_rate,
            "service_fee": round(self.service_fee, 2),
            "net_amount": round(self.net_amount, 2),
            "basis": self.basis,
            "disclaimer": self.disclaimer,
        }


@dataclass
class PlanDraft:
    """方案初稿（复用项目二能力生成，本项目只负责"在对的时机把它准备好"）。"""

    customer_id: str
    product_code: str
    product_name: str
    summary: str
    citations: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)  # 未匹配产品与原因（归因用）
    handoff_reason: str = ""  # 无匹配时的转人工原因

    def to_dict(self) -> dict[str, Any]:
        return {
            "customer_id": self.customer_id,
            "product_code": self.product_code,
            "product_name": self.product_name,
            "summary": self.summary,
            "citations": list(self.citations),
            "rejected": list(self.rejected),
            "handoff_reason": self.handoff_reason,
        }


@dataclass
class ReadinessPack:
    """到店前准备包：业务员到店即可谈的"一页纸"。"""

    pack_id: str
    lead_id: str
    customer_id: str
    profile: ProfileSummary
    highlights: list[str]
    risks: list[str]
    plan_draft: PlanDraft | None
    fee_estimate: FeeEstimate | None
    talking_points: list[str]
    warnings: list[str]
    prepared_in_ms: float
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "lead_id": self.lead_id,
            "customer_id": self.customer_id,
            "profile": self.profile.to_dict(),
            "highlights": list(self.highlights),
            "risks": list(self.risks),
            "plan_draft": self.plan_draft.to_dict() if self.plan_draft else None,
            "fee_estimate": self.fee_estimate.to_dict() if self.fee_estimate else None,
            "talking_points": list(self.talking_points),
            "warnings": list(self.warnings),
            "prepared_in_ms": round(self.prepared_in_ms, 1),
            "created_at": self.created_at,
        }


# =========================================================================== #
# 四、人机边界：审批与审计
# =========================================================================== #
class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def is_final(self) -> bool:
        return self in (ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED)


@dataclass
class ApprovalRequest:
    """高风险动作的人工审批请求。

    铁律：**没有 approved 记录就没有执行**。过期只能 expired，绝不自动批准 ——
    助贷场景里"没人批"的正确结果是"这事没发生"，不是"系统替人批了"。
    """

    approval_id: str
    action: str  # send_contract / confirm_fee / ...
    lead_id: str
    payload: dict[str, Any]
    requester: str  # 谁发起的（agent 或业务员）
    reason: str  # 为什么要做这个动作
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: float = 0.0
    decided_at: float = 0.0
    decided_by: str = ""
    decision_comment: str = ""
    expires_at: float = 0.0
    # 执行时间：**批准和执行是两件事**，必须分开记。
    # status 只表达"人批没批"，executed_at 表达"事办没办" ——
    # 只靠 status 的话，界面无法区分"已批准还没发"和"已批准且已发出"，
    # 而这两者在合规上是完全不同的责任。
    executed_at: float = 0.0

    @property
    def is_executed(self) -> bool:
        return self.executed_at > 0

    def to_dict(self, *, include_payload: bool = True) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "action": self.action,
            "lead_id": self.lead_id,
            "payload": dict(self.payload) if include_payload else {},
            "requester": self.requester,
            "reason": self.reason,
            "status": self.status.value,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "decision_comment": self.decision_comment,
            "expires_at": self.expires_at,
            "executed_at": self.executed_at,
            "is_executed": self.is_executed,
        }


@dataclass
class AuditEntry:
    """一条审计记录：谁、何时、对什么、做了什么、结果如何、关联哪次审批。"""

    entry_id: str
    ts: float
    actor: str
    action: str
    target_type: str  # lead / appointment / approval / fee / ...
    target_id: str
    detail: str
    approval_id: str = ""
    result: str = "ok"  # ok / blocked / failed / idempotent_hit

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "ts": self.ts,
            "actor": self.actor,
            "action": self.action,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "detail": self.detail,
            "approval_id": self.approval_id,
            "result": self.result,
        }


@dataclass
class ProgressSnapshot:
    """进度自助查询的返回（**脱敏视图**）。

    安全口径：只给阶段与时间，不给身份证号、不给完整手机号、不给征信明细。
    客户问"我的进件到哪一步了"只需要这句话，不需要更多。
    """

    lead_id: str
    customer_name: str
    current_stage: str
    stage_label: str
    next_step: str
    appointment: dict[str, Any] | None
    timeline: list[dict[str, Any]]
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "lead_id": self.lead_id,
            "customer_name": self.customer_name,
            "current_stage": self.current_stage,
            "stage_label": self.stage_label,
            "next_step": self.next_step,
            "appointment": self.appointment,
            "timeline": list(self.timeline),
            "updated_at": self.updated_at,
        }


# =========================================================================== #
# 五、评测
# =========================================================================== #
@dataclass
class EvalCase:
    """流程行为评测用例。

    断言全部是**确定性行为**（该不该告警、该不该拦截、幂等有没有生效），
    不评"回答得好不好" —— 流程系统的正确性是布尔问题，不是主观问题。
    """

    case_id: str
    title: str
    setup: dict[str, Any] = field(default_factory=dict)  # 构造场景的参数
    expect: dict[str, Any] = field(default_factory=dict)  # 期望结果
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "title": self.title,
            "setup": dict(self.setup),
            "expect": dict(self.expect),
            "tags": list(self.tags),
        }


@dataclass
class EvalCaseResult:
    case_id: str
    title: str
    passed: bool
    score: float
    failures: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "title": self.title,
            "passed": self.passed,
            "score": round(self.score, 3),
            "failures": list(self.failures),
            "detail": dict(self.detail),
        }


@dataclass
class EvalReport:
    total: int
    passed: int
    avg_score: float
    results: list[EvalCaseResult] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "pass_rate": round(self.pass_rate, 3),
            "avg_score": round(self.avg_score, 3),
            "results": [r.to_dict() for r in self.results],
        }


@dataclass
class GateDecision:
    """准入结论（发版/提示词变更前的机器化门禁）。"""

    allowed: bool
    reasons: list[str]
    baseline: dict[str, Any]
    current: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "baseline": dict(self.baseline),
            "current": dict(self.current),
        }


# =========================================================================== #
# 六、阶段文案（自助查询与 API 共用，保证客户看到的话术一致）
# =========================================================================== #
STAGE_LABELS: dict[LeadStage, str] = {
    LeadStage.NEW: "已登记",
    LeadStage.ASSIGNED: "已分配顾问",
    LeadStage.FIRST_CALLED: "已电话沟通",
    LeadStage.WECHAT_ADDED: "已加微信",
    LeadStage.NURTURING: "方案沟通中",
    LeadStage.INVITED: "已预约到店",
    LeadStage.ARRIVED: "已到店",
    LeadStage.PROPOSAL_READY: "方案已备好",
    LeadStage.FEE_ESTIMATED: "费用已核算",
    LeadStage.SIGNED: "已签约",
    LeadStage.LOST: "已结束",
}

NEXT_STEPS: dict[LeadStage, str] = {
    LeadStage.NEW: "顾问会尽快与您电话联系",
    LeadStage.ASSIGNED: "顾问将在一个工作时段内致电您",
    LeadStage.FIRST_CALLED: "顾问会添加您微信，发送资料清单",
    LeadStage.WECHAT_ADDED: "请把公积金/社保截图发我，我们做初审",
    LeadStage.NURTURING: "顾问正在为您匹配银行产品",
    LeadStage.INVITED: "请按预约时间到店，带齐身份证与银行卡",
    LeadStage.ARRIVED: "顾问正在为您准备方案",
    LeadStage.PROPOSAL_READY: "方案初稿已就绪，可预约面谈",
    LeadStage.FEE_ESTIMATED: "费用明细已核算，面谈时确认",
    LeadStage.SIGNED: "已进入放款流程",
    LeadStage.LOST: "如有需要可重新联系顾问",
}
