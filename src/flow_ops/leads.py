"""线索状态机与 SOP 规则（项目八的"流程自动化"核心）。

两个职责：

1. **状态机**：定义线索允许的流转路径。非法流转直接拒绝 ——
   "从 new 直接跳到 signed" 这类绕过 SOP 的操作，必须在代码层被挡住，
   而不是靠 UI 上不给你这个按钮（接口层永远有人直接调）。

2. **SOP 规则表**：定义"进入某阶段后多久内必须完成什么"。
    漏跟进的判定 = 当前时间 - stage_entered_at > sla_minutes
    —— 纯确定性计算，不依赖任何模型判断。这是本项目"用代码固化业务判据"
    设计原则的直接体现：模型可以帮写提醒话术，但"算不算漏"必须可解释、可复现。
"""

from __future__ import annotations

from agent_platform.config import Settings
from agent_platform.logging_config import get_logger
from flow_ops.domain import LeadStage, SopRule

logger = get_logger(__name__)


class FlowError(Exception):
    """流程领域的业务异常。

    code 用于让上层（API / Agent）把错误分类返回给用户：
        illegal_transition  非法流转
        lead_not_found      线索不存在
        duplicate_lead      重复录入（幂等命中是另一回事，那是正常返回）
        approval_required   需要人工审批
        approval_not_found  审批不存在
        lock_busy           锁被占用（并发冲突）
        human_stage         人工节点禁区
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# 一、状态机
# --------------------------------------------------------------------------- #
#: 允许的流转表。key = 当前阶段，value = 可达阶段集合。
#: 设计约束：
#:   ① 不允许"跳阶段"——每一步 SOP 都是留痕点，跳过的步骤无法审计；
#:   ② 任何非终态都可以 LOST（客户流失是随时可能发生的正常业务）；
#:   ③ 终态（SIGNED / LOST）不可再流转 —— 历史不允许被改写。
ALLOWED_TRANSITIONS: dict[LeadStage, frozenset[LeadStage]] = {
    LeadStage.NEW: frozenset({LeadStage.ASSIGNED, LeadStage.LOST}),
    LeadStage.ASSIGNED: frozenset({LeadStage.FIRST_CALLED, LeadStage.LOST}),
    LeadStage.FIRST_CALLED: frozenset({LeadStage.WECHAT_ADDED, LeadStage.LOST}),
    LeadStage.WECHAT_ADDED: frozenset({LeadStage.NURTURING, LeadStage.LOST}),
    LeadStage.NURTURING: frozenset({LeadStage.INVITED, LeadStage.LOST}),
    LeadStage.INVITED: frozenset({LeadStage.ARRIVED, LeadStage.LOST}),
    LeadStage.ARRIVED: frozenset({LeadStage.PROPOSAL_READY, LeadStage.LOST}),
    LeadStage.PROPOSAL_READY: frozenset({LeadStage.FEE_ESTIMATED, LeadStage.LOST}),
    LeadStage.FEE_ESTIMATED: frozenset({LeadStage.SIGNED, LeadStage.LOST}),
    LeadStage.SIGNED: frozenset(),
    LeadStage.LOST: frozenset(),
}

#: 每个阶段"推进到下一阶段"要求完成的动作（供 SOP 检测与 API 提示共用）
STAGE_ACTION_HINT: dict[LeadStage, str] = {
    LeadStage.NEW: "分配业务员",
    LeadStage.ASSIGNED: "完成首呼",
    LeadStage.FIRST_CALLED: "添加客户微信",
    LeadStage.WECHAT_ADDED: "跟进并推送匹配方案",
    LeadStage.NURTURING: "发出到店邀约",
    LeadStage.INVITED: "确认到店时间",
    LeadStage.ARRIVED: "接待客户并汇总档案",
    LeadStage.PROPOSAL_READY: "费用预估",
    LeadStage.FEE_ESTIMATED: "人工确认费用并签约",
    LeadStage.SIGNED: "-",
    LeadStage.LOST: "-",
}


def can_transition(from_stage: LeadStage, to_stage: LeadStage) -> bool:
    """是否允许 from → to 的流转。"""
    return to_stage in ALLOWED_TRANSITIONS.get(from_stage, frozenset())


def validate_transition(from_stage: LeadStage, to_stage: LeadStage) -> None:
    """校验流转合法性，非法则抛 FlowError。"""
    if from_stage == to_stage:
        raise FlowError("illegal_transition", f"阶段未变化：{from_stage.value} → {to_stage.value}")
    if from_stage.is_terminal:
        raise FlowError("illegal_transition", f"终态不可再流转：{from_stage.value}")
    if not can_transition(from_stage, to_stage):
        raise FlowError(
            "illegal_transition",
            f"非法流转：{from_stage.value} → {to_stage.value}，"
            f"允许的目标：{sorted(s.value for s in ALLOWED_TRANSITIONS.get(from_stage, frozenset()))}",
        )


# --------------------------------------------------------------------------- #
# 二、SOP 规则
# --------------------------------------------------------------------------- #
def build_sop_rules(settings: Settings) -> list[SopRule]:
    """从配置构造 SOP 规则表。

    为什么从配置构造而不是写死：SLA（首呼 30 分钟、加微 2 小时……）是运营决策，
    换一个团队/换一个渠道就可能调整。写死在代码里意味着每次调整都要发版。
    """
    return [
        SopRule(
            stage=LeadStage.ASSIGNED,
            action="first_call",
            sla_minutes=settings.flow_first_call_sla_minutes,
            description="线索分配后完成首次外呼",
        ),
        SopRule(
            stage=LeadStage.FIRST_CALLED,
            action="add_wechat",
            sla_minutes=settings.flow_wechat_sla_hours * 60,
            description="首呼后添加客户微信",
        ),
        SopRule(
            stage=LeadStage.WECHAT_ADDED,
            action="follow_up",
            sla_minutes=settings.flow_followup_sla_hours * 60,
            description="加微后持续跟进，推动方案沟通",
        ),
        SopRule(
            stage=LeadStage.NURTURING,
            action="follow_up",
            sla_minutes=settings.flow_followup_sla_hours * 60,
            description="跟进中保持节奏，避免客户沉睡",
        ),
        SopRule(
            stage=LeadStage.INVITED,
            action="confirm_arrival",
            sla_minutes=settings.flow_invite_confirm_sla_hours * 60,
            description="邀约后确认客户到店时间",
        ),
    ]


def sop_rule_for(stage: LeadStage, rules: list[SopRule]) -> SopRule | None:
    """取某阶段对应的 SOP 规则（无规则的阶段不参与漏跟进检测）。"""
    for rule in rules:
        if rule.stage == stage:
            return rule
    return None


def overdue_minutes(
    *,
    stage_entered_at: float,
    sla_minutes: int,
    now: float,
) -> int:
    """计算逾期分钟数。未逾期返回 0（不是负数，避免下游误判）。"""
    elapsed_minutes = (now - stage_entered_at) / 60.0
    over = int(elapsed_minutes - sla_minutes)
    return max(over, 0)
