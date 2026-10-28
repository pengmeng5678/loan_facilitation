"""进度自助查询（项目八"进度自助查询"，对应重复咨询下降 60%）。

客户反复打电话问业务员"我的进件到哪一步了"，占掉业务员大量时间。
自助查询把这类重复答疑转移到企微/小程序，**业务员时间还给了跟进**。

**安全口径（面试高频追问：会不会暴露隐私）**：

    ① 身份校验：客户要知道线索 ID + 手机号后 4 位（双重定位，猜不出来）；
    ② 脱敏返回：只给阶段名与时间线，**不给身份证号、完整手机号、征信明细**；
    ③ 时间线只暴露"流程节点"，不暴露"内部动作"——
       客户不需要知道"主管催了两次"这种内部管理信息。

**为什么脱敏在服务端做而不是前端**：前端脱敏等于把敏感数据
先送到了客户端。服务端只发该发的字段，前端想多显示也拿不到。
"""

from __future__ import annotations

import time

from agent_platform.logging_config import get_logger
from flow_ops.domain import (
    NEXT_STEPS,
    STAGE_LABELS,
    Lead,
    LeadStage,
    ProgressSnapshot,
)
from flow_ops.leads import FlowError
from flow_ops.store import FlowStore

logger = get_logger(__name__)

#: 客户可见的节点（内部动作不进自助查询时间线）
_CUSTOMER_VISIBLE_STAGES: frozenset[LeadStage] = frozenset(
    {
        LeadStage.ASSIGNED,
        LeadStage.FIRST_CALLED,
        LeadStage.WECHAT_ADDED,
        LeadStage.NURTURING,
        LeadStage.INVITED,
        LeadStage.ARRIVED,
        LeadStage.PROPOSAL_READY,
        LeadStage.FEE_ESTIMATED,
        LeadStage.SIGNED,
        LeadStage.LOST,
    }
)


class ProgressService:
    """客户侧进度自助查询。"""

    def __init__(self, store: FlowStore) -> None:
        self._store = store

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def query(self, lead_id: str, phone_last4: str, *, now: float | None = None) -> ProgressSnapshot:
        """身份校验 + 脱敏视图。校验失败抛 FlowError(forbidden)。"""
        lead = self._store.get_lead(lead_id)
        if lead is None:
            raise FlowError("lead_not_found", f"线索不存在：{lead_id}")
        self._verify_identity(lead, phone_last4)

        timeline = self._customer_timeline(lead)
        appointment = self._appointment_view(lead)
        return ProgressSnapshot(
            lead_id=lead.lead_id,
            customer_name=self._mask_name(lead.customer_name),
            current_stage=lead.stage.value,
            stage_label=STAGE_LABELS.get(lead.stage, lead.stage.value),
            next_step=NEXT_STEPS.get(lead.stage, ""),
            appointment=appointment,
            timeline=timeline,
            updated_at=lead.updated_at,
        )

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _verify_identity(lead: Lead, phone_last4: str) -> None:
        """手机号后 4 位校验。"""
        expected = lead.phone_masked.replace("*", "")[-4:]
        given = (phone_last4 or "").strip()
        if len(given) != 4 or not given.isdigit() or given != expected:
            # 刻意不说"哪一位错了"—— 防枚举
            raise FlowError("forbidden", "身份校验未通过，请确认线索编号与手机号后 4 位")

    @staticmethod
    def _mask_name(name: str) -> str:
        """姓名脱敏：张* / 欧阳**。留首字足够客户确认"是我的单子"。"""
        if not name:
            return ""
        if len(name) <= 1:
            return name
        return name[0] + "*" * (len(name) - 1)

    def _customer_timeline(self, lead: Lead) -> list[dict]:
        transitions = self._store.list_transitions(lead.lead_id)
        items: list[dict] = []
        for tr in transitions:
            try:
                stage = LeadStage(tr.to_stage)
            except ValueError:
                continue
            if stage not in _CUSTOMER_VISIBLE_STAGES:
                continue
            items.append(
                {
                    "stage": stage.value,
                    "label": STAGE_LABELS.get(stage, stage.value),
                    "ts": tr.ts,
                    "time_text": self._format_ts(tr.ts),
                }
            )
        return items

    def _appointment_view(self, lead: Lead) -> dict | None:
        """邀约信息（只给门店与时间，不给内部备注）。"""
        appt = self._store.find_active_appointment(lead.lead_id)
        if appt is None:
            return None
        return {
            "store": appt.store,
            "scheduled_at": appt.scheduled_at,
            "time_text": self._format_ts(appt.scheduled_at),
            "status": appt.status,
        }

    @staticmethod
    def _format_ts(ts: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
