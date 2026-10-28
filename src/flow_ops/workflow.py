"""FlowEngine —— 项目八的"数字员工"本体。

它是简历里那句话的代码化：
    **"把'提醒人做'升级为'替人做准备'，同时用 HITL 把成交留在人手里。"**

引擎对外暴露的动作分三类，边界清清楚楚：

    ┌─ 准备类（自动执行）intake / advance / invite / sweep / prepare / estimate
    ├─ 建议类（自动执行）readiness pack / fee estimate（都带"预估≠报价"口径）
    └─ 成交类（必须人批）send_contract / confirm_fee → ApprovalGate  → assert_approved

**每个写操作的固定套路**（生产级可靠性的来源，不是可选项）：

    ① 生成幂等键（业务语义，防重复提交）
    ② 拿分布式锁（防并发双写）
    ③ 执行业务动作 + 状态机校验
    ④ 写审计（含 result：ok / blocked / idempotent_hit）
    ⑤ 释放锁

定时任务（SOP 扫描）与 HTTP 请求走同一套逻辑 —— 所以"客户连点三次"
和"定时器跑三遍"的结果完全一致。
"""

from __future__ import annotations

import time

from agent_platform.config import Settings
from agent_platform.logging_config import get_logger
from flow_ops.approval import ApprovalGate
from flow_ops.audit import AuditLogger
from flow_ops.crm import CrmClient
from flow_ops.domain import (
    Appointment,
    ApprovalRequest,
    FeeEstimate,
    HumanStage,
    Lead,
    LeadStage,
    ProfileSummary,
    ReadinessPack,
    SopAlert,
)
from flow_ops.idempotency import IdempotencyGuard, LockGuard, make_idempotency_key
from flow_ops.leads import (
    FlowError,
    build_sop_rules,
    overdue_minutes,
    sop_rule_for,
    validate_transition,
)
from flow_ops.locking import build_lock_backend
from flow_ops.progress import ProgressService
from flow_ops.readiness import ReadinessBuilder
from flow_ops.store import FlowStore

logger = get_logger(__name__)

#: 阶段顺序（用于"已经到更后的阶段了"判断与自助查询进度百分比）
_STAGE_ORDER: tuple[LeadStage, ...] = (
    LeadStage.NEW,
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
)


def stage_rank(stage: LeadStage) -> int:
    return _STAGE_ORDER.index(stage)


class FlowEngine:
    """流程数字员工引擎。"""

    def __init__(
        self,
        settings: Settings,
        store: FlowStore,
        *,
        crm: CrmClient | None = None,
        plan_provider=None,
        lock_backend=None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.crm = crm
        self.idem = IdempotencyGuard(store, ttl_hours=settings.flow_idempotency_ttl_hours)
        # 锁后端：默认按配置建（FLOW_LOCK_BACKEND=sqlite|redis）。
        # 测试可显式传入假后端；redis 模式下启动即探测连通性，连不上直接炸 ——
        # 多 worker 部署里"锁静默失效"是数据级事故，不能拖到第一个写请求。
        self.locks = LockGuard(
            lock_backend if lock_backend is not None else build_lock_backend(settings, store),
            ttl_seconds=settings.flow_lock_ttl_seconds,
        )
        self.audit = AuditLogger(store)
        self.approvals = ApprovalGate(store, settings)
        self.readiness = ReadinessBuilder(settings, plan_provider=plan_provider)
        self.progress = ProgressService(store)
        self.sop_rules = build_sop_rules(settings)

    # ================================================================== #
    # 一、线索录入与推进
    # ================================================================== #
    def intake_lead(
        self,
        *,
        customer_id: str,
        customer_name: str,
        phone_masked: str,
        source: str,
        owner: str,
        actor: str = "system",
        note: str = "",
        idem_key: str = "",
    ) -> tuple[Lead, bool]:
        """录入线索并自动分配。返回 (线索, 是否幂等命中)。

        幂等键缺省时按业务语义自动生成：customer_id + source + owner ——
        同一个客户同一个渠道重复提交（典型：广告表单连点两次）只录一条。
        """
        key = idem_key or make_idempotency_key("intake", customer_id, source, owner)

        def _do() -> dict:
            lead = Lead.create(
                customer_id=customer_id,
                customer_name=customer_name,
                phone_masked=phone_masked,
                source=source,
                owner=owner,
                note=note,
            )
            self.store.insert_lead(lead)
            self.audit.log(
                actor=actor, action="lead.intake", target_type="lead", target_id=lead.lead_id,
                detail=f"录入线索 客户={customer_id} 渠道={source} 分配={owner}",
            )
            # 通知 CRM 分配（失败不影响线索已落库）
            if self.crm is not None:
                try:
                    self.crm.assign_owner(
                        lead.lead_id, owner=owner,
                        idem_key=make_idempotency_key("crm_assign", lead.lead_id, owner),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("CRM 分配通知失败：%s", exc)
            return lead.to_dict()

        result, hit = self.idem.run(key, _do, action_name="lead.intake")
        if hit:
            self.audit.log(
                actor=actor, action="lead.intake", target_type="lead",
                target_id=result.get("lead_id", ""), detail="重复录入被幂等拦截",
                result="idempotent_hit",
            )
            # 幂等命中时**重新读库**返回当前状态，而不是首次快照：
            # 客户重复提交留资表单时，他真正想知道的是"我的线索现在到哪了"，
            # 而不是"你第一次提交时它是新的"。返回快照会让调用方拿着
            # 过期状态去推进流程，撞状态机校验（那是保护，但报错对人不友好）。
            current = self.store.get_lead(result["lead_id"])
            if current is not None:
                result = current.to_dict()
        lead = Lead(
            lead_id=result["lead_id"], customer_id=result["customer_id"],
            customer_name=result["customer_name"], phone_masked=result["phone_masked"],
            source=result["source"], owner=result["owner"],
            stage=LeadStage(result["stage"]), human_stage=None,
            lost_reason=result.get("lost_reason", ""), note=result.get("note", ""),
            stage_entered_at=result.get("stage_entered_at", 0.0),
            created_at=result.get("created_at", 0.0), updated_at=result.get("updated_at", 0.0),
        )
        return lead, hit

    def advance(
        self,
        lead_id: str,
        to_stage: LeadStage | str,
        *,
        actor: str = "system",
        reason: str = "",
    ) -> Lead:
        """推进阶段。状态机校验 + 锁 + 幂等 + 审计。

        两个可靠性要点：

        ① **必须在锁内重新读库**：锁外读到的 lead 可能已经被另一请求推进，
           用它做 validate_transition 是典型 TOCTOU —— 校验通过了，
           但真正写入时状态已经不是校验时的状态。
        ② **幂等键 = lead_id + 目标阶段 + 原因**：网络重试把同一个"推进"
           再发一次时，第二次不该撞 `illegal_transition`（那会把正常重试
           报成业务错误），而应直接返回当前状态。

        幂等命中后**重新读库**返回当前状态，不是首次快照：调用方拿到过期
        快照会继续按旧阶段推进，再次撞状态机。
        """
        to = LeadStage(to_stage) if isinstance(to_stage, str) else to_stage
        self._require_lead(lead_id)  # 先给出清晰的"线索不存在"错误
        key = make_idempotency_key("advance", lead_id, to.value, reason)

        def _do() -> dict:
            with self.locks.hold(f"advance:{lead_id}", owner=f"adv-{actor}"):
                lead = self._reload(lead_id)
                validate_transition(lead.stage, to)
                old = lead.stage
                lead.stage = to
                lead.stage_entered_at = time.time()
                lead.updated_at = time.time()
                # 进入人工环节时自动挂起对应人工节点（Agent 从此禁止执行）
                if to.needs_human and lead.human_stage is None:
                    lead.human_stage = HumanStage.RECEPTION
                if to is LeadStage.LOST:
                    lead.lost_reason = reason or "未填写"
                self.store.update_lead(lead)
                self.store.insert_transition(
                    _transition(lead_id, old, to, actor=actor, reason=reason)
                )
                self.audit.log(
                    actor=actor, action="lead.advance", target_type="lead", target_id=lead_id,
                    detail=f"{old.value} → {to.value}：{reason}",
                )
                return lead.to_dict()

        result, hit = self.idem.run(key, _do, action_name="lead.advance")
        if hit:
            self.audit.log(
                actor=actor, action="lead.advance", target_type="lead", target_id=lead_id,
                detail=f"重复推进被幂等拦截（目标阶段 {to.value}）",
                result="idempotent_hit",
            )
            return self._reload(lead_id)
        return _lead_from(result)

    # ================================================================== #
    # 二、邀约与到店
    # ================================================================== #
    def invite(
        self,
        lead_id: str,
        *,
        store: str,
        scheduled_at: float,
        actor: str = "system",
        note: str = "",
        idem_key: str = "",
    ) -> tuple[Appointment, bool]:
        """创建到店邀约并把线索推到 INVITED。

        幂等键缺省 = lead_id + store + 时间段：同一客户同一时段
        重复邀约（消息重投 / 前端重试）只保留一条。
        """
        key = idem_key or make_idempotency_key("invite", lead_id, store, int(scheduled_at))
        self._require_lead(lead_id)

        def _do() -> dict:
            with self.locks.hold(f"invite:{lead_id}", owner=f"inv-{actor}"):
                appt = Appointment(
                    appointment_id=f"ap_{int(time.time() * 1000):x}_{lead_id[-4:]}",
                    lead_id=lead_id, store=store, scheduled_at=scheduled_at,
                    status="booked", note=note,
                    created_at=time.time(), updated_at=time.time(),
                )
                self.store.insert_appointment(appt)
                if self.crm is not None:
                    try:
                        self.crm.create_appointment(
                            lead_id, store=store, scheduled_at=scheduled_at,
                            idem_key=make_idempotency_key("crm_appt", lead_id, store, int(scheduled_at)),
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("CRM 邀约同步失败：%s", exc)
                # 状态机推进：NURTURING → INVITED（幂等语义下允许重复邀约刷新时间）
                current = self._reload(lead_id)
                if stage_rank(current.stage) < stage_rank(LeadStage.INVITED):
                    old = current.stage
                    current.stage = LeadStage.INVITED
                    current.stage_entered_at = time.time()
                    current.updated_at = time.time()
                    self.store.update_lead(current)
                    self.store.insert_transition(
                        _transition(lead_id, old, LeadStage.INVITED, actor=actor, reason="发出到店邀约")
                    )
                self.audit.log(
                    actor=actor, action="lead.invite", target_type="lead", target_id=lead_id,
                    detail=f"邀约到店 门店={store} 时间={time.strftime('%Y-%m-%d %H:%M', time.localtime(scheduled_at))}",
                )
                return appt.to_dict()

        result, hit = self.idem.run(key, _do, action_name="lead.invite")
        if hit:
            self.audit.log(
                actor=actor, action="lead.invite", target_type="lead", target_id=lead_id,
                detail="重复邀约被幂等拦截", result="idempotent_hit",
            )
            # 同样读当前状态：重复邀约时预约可能已被确认/已到店
            current = self.store.find_active_appointment(lead_id)
            if current is not None:
                result = current.to_dict()
        return _appointment_from(result), hit

    def confirm_arrival(
        self, lead_id: str, *, actor: str = "system", idem_key: str = ""
    ) -> tuple[Lead, bool]:
        """到店确认。幂等（客户/前台重复确认只生效一次）。"""
        key = idem_key or make_idempotency_key("confirm_arrival", lead_id)
        self._require_lead(lead_id)

        def _do() -> dict:
            with self.locks.hold(f"arrive:{lead_id}", owner=f"arr-{actor}"):
                lead = self._reload(lead_id)
                if stage_rank(lead.stage) < stage_rank(LeadStage.ARRIVED):
                    old = lead.stage
                    lead.stage = LeadStage.ARRIVED
                    lead.stage_entered_at = time.time()
                    lead.updated_at = time.time()
                    lead.human_stage = HumanStage.RECEPTION  # 到店即进入人工接待
                    self.store.update_lead(lead)
                    self.store.insert_transition(
                        _transition(lead_id, old, LeadStage.ARRIVED, actor=actor, reason="客户到店确认")
                    )
                appt = self.store.find_active_appointment(lead_id)
                if appt is not None:
                    appt.status = "arrived"
                    appt.updated_at = time.time()
                    self.store.update_appointment(appt)
                self.audit.log(
                    actor=actor, action="lead.confirm_arrival", target_type="lead",
                    target_id=lead_id, detail="客户到店，进入人工接待节点",
                )
                return lead.to_dict()

        result, hit = self.idem.run(key, _do, action_name="lead.confirm_arrival")
        if hit:
            self.audit.log(
                actor=actor, action="lead.confirm_arrival", target_type="lead", target_id=lead_id,
                detail="重复到店确认被幂等拦截", result="idempotent_hit",
            )
            current = self.store.get_lead(lead_id)
            if current is not None:
                result = current.to_dict()
        return _lead_from(result), hit

    # ================================================================== #
    # 三、SOP 扫描（系统兜底：把"靠自觉"变成"系统预警"）
    # ================================================================== #
    def sweep_sop(self, *, actor: str = "system", now: float | None = None) -> list[SopAlert]:
        """扫描所有活跃线索，逾期即预警主管。

        这是定时任务的主入口（简历里的"SOP 漏做即预警"）。
        幂等语义：同一线索同一动作已有未处理预警时**不重复创建** ——
        否则主管每 5 分钟收到同一条轰炸，很快就会学会忽略预警。
        """
        ts = now if now is not None else time.time()
        alerts: list[SopAlert] = []
        leads = self.store.list_leads(limit=500)
        for item in leads:
            if item.stage.is_terminal:
                continue
            rule = sop_rule_for(item.stage, self.sop_rules)
            if rule is None:
                continue
            over = overdue_minutes(
                stage_entered_at=item.stage_entered_at, sla_minutes=rule.sla_minutes, now=ts
            )
            if over <= 0:
                continue
            # 宽限期：最近 2×SLA 内已有预警（不论认领与否）就跳过 ——
            # 未认领的靠"还在列表里"持续提醒，认领的给处理时间，
            # 超出一倍 SLA 还没解决才升级为再次预警。
            if self.store.find_recent_alert(
                item.lead_id, rule.action, since_ts=ts - rule.sla_minutes * 120
            ) is not None:
                continue
            alert = SopAlert(
                alert_id=f"al_{int(time.time() * 1000):x}_{item.lead_id[-4:]}",
                lead_id=item.lead_id, customer_name=item.customer_name, owner=item.owner,
                stage=item.stage.value, action=rule.action, overdue_minutes=over,
                created_at=ts,
            )
            self.store.insert_alert(alert)
            self.audit.log(
                actor=actor, action="sop.alert", target_type="lead", target_id=item.lead_id,
                detail=f"SOP 逾期：{rule.action} 超时 {over} 分钟（ SLA {rule.sla_minutes} ），已通知主管",
            )
            if self.crm is not None:
                try:
                    self.crm.notify_supervisor(
                        owner=item.owner,
                        message=f"客户 {item.customer_name} 的 {rule.action} 已超时 {over} 分钟",
                        idem_key=make_idempotency_key("crm_alert", item.lead_id, rule.action, over),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("主管通知失败：%s", exc)
            alerts.append(alert)
        if alerts:
            logger.info("SOP 扫描：%d 条预警", len(alerts))
        return alerts

    def acknowledge_alert(self, alert_id: str, *, by: str) -> bool:
        with self.locks.hold(f"alert:{alert_id}", owner=f"ack-{by}"):
            ok = self.store.acknowledge_alert(alert_id, by=by, now=time.time())
            if ok:
                self.audit.log(
                    actor=by, action="sop.ack", target_type="alert", target_id=alert_id,
                    detail="预警已认领",
                )
            return ok

    # ================================================================== #
    # 四、到店前准备与费用预估（"替人做准备"）
    # ================================================================== #
    def prepare_arrival(
        self, lead_id: str, profile: ProfileSummary, *, actor: str = "agent"
    ) -> ReadinessPack:
        """生成到店前准备包（档案 + 方案初稿 + 费用预估）。

        人工节点禁区：客户已经在谈接待/谈判时，Agent 不得再自动改状态 ——
        准备包可以生成（是材料），但阶段推进只允许人做。

        **整段在锁内执行**：人工节点判断与阶段推进必须基于同一次读库结果。
        锁外判断"当前不在人工节点"、锁内写入的写法，正好会被
        "读到 ARRIVED → 业务员同时转 NEGOTIATION → 这里仍把阶段推到
        PROPOSAL_READY" 的时序击穿。
        """
        with self.locks.hold(f"prepare:{lead_id}", owner=f"prep-{actor}"):
            lead = self._reload(lead_id)
            if lead.human_stage in (HumanStage.NEGOTIATION, HumanStage.FEE_CONFIRMATION):
                raise FlowError(
                    "human_stage",
                    f"客户处于{lead.human_stage.label}节点，到店前准备须由业务员确认后重新生成",
                )
            pack = self.readiness.build(lead, profile)
            self.audit.log(
                actor=actor, action="readiness.build", target_type="lead", target_id=lead_id,
                detail=f"准备包 {pack.pack_id}：方案={pack.plan_draft.product_code if pack.plan_draft else '无'} "
                       f"费用={pack.fee_estimate.service_fee if pack.fee_estimate else '无'} "
                       f"耗时 {pack.prepared_in_ms:.0f}ms",
            )
            # 已到店且还没标记方案就绪 → 自动推进（这是"准备"语义，不是成交动作）
            if lead.stage is LeadStage.ARRIVED:
                lead.stage = LeadStage.PROPOSAL_READY
                lead.stage_entered_at = time.time()
                lead.updated_at = time.time()
                self.store.update_lead(lead)
                self.store.insert_transition(
                    _transition(lead_id, LeadStage.ARRIVED, LeadStage.PROPOSAL_READY,
                                actor=actor, reason="到店前准备包已生成")
                )
            return pack

    def estimate_fee(
        self,
        lead_id: str,
        *,
        loan_amount: float,
        months: int | None = None,
        annual_rate: float | None = None,
        actor: str = "agent",
    ) -> FeeEstimate:
        """费用预估（确定性公式，可复现）。预估 ≠ 报价，禁止对外直接当报价发。

        阶段推进放在锁内并重新读库：否则"锁外看到 PROPOSAL_READY、
        锁内才发现已被别人推到 FEE_ESTIMATED / SIGNED"时会写出倒退的转换记录。
        """
        with self.locks.hold(f"fee:{lead_id}", owner=f"fee-{actor}"):
            lead = self._reload(lead_id)
            estimate = self.readiness.fee_estimator.estimate(
                lead_id, loan_amount=loan_amount, months=months, annual_rate=annual_rate
            )
            self.audit.log(
                actor=actor, action="fee.estimate", target_type="lead", target_id=lead_id,
                detail=f"费用预估：{estimate.basis} 服务费 {estimate.service_fee} 元",
            )
            if lead.stage is LeadStage.PROPOSAL_READY:
                lead.stage = LeadStage.FEE_ESTIMATED
                lead.stage_entered_at = time.time()
                lead.updated_at = time.time()
                self.store.update_lead(lead)
                self.store.insert_transition(
                    _transition(lead_id, LeadStage.PROPOSAL_READY, LeadStage.FEE_ESTIMATED,
                                actor=actor, reason="费用预估完成")
                )
            return estimate

    # ================================================================== #
    # 五、人机边界：人工节点与高风险审批
    # ================================================================== #
    def request_handoff(
        self, lead_id: str, human_stage: HumanStage | str, *, actor: str = "agent", note: str = ""
    ) -> Lead:
        """标记线索进入人工节点（接待 / 谈判 / 费用确认）。

        这是 Agent **唯一**被允许触碰人工节点的方式：只打标记、写备注，
        绝不代替人做节点内动作。
        """
        stage = HumanStage(human_stage) if isinstance(human_stage, str) else human_stage
        with self.locks.hold(f"handoff:{lead_id}", owner=f"handoff-{actor}"):
            lead = self._reload(lead_id)
            lead.human_stage = stage
            lead.note = note or lead.note
            lead.updated_at = time.time()
            self.store.update_lead(lead)
            self.audit.log(
                actor=actor, action="lead.handoff", target_type="lead", target_id=lead_id,
                detail=f"转人工节点：{stage.label} {note}",
            )
            return lead

    def request_high_risk(
        self,
        action: str,
        *,
        lead_id: str,
        payload: dict | None = None,
        requester: str = "agent",
        reason: str = "",
        ttl_hours: int = 24,
    ) -> ApprovalRequest:
        """发起高风险动作审批（发合同 / 确认费用）。创建后状态为 pending。"""
        if not self.approvals.is_high_risk(action):
            raise FlowError("approval_required", f"{action} 不在高风险清单内，无需审批")
        self._require_lead(lead_id)
        appr = self.approvals.request(
            action, lead_id=lead_id, payload=payload, requester=requester,
            reason=reason, ttl_hours=ttl_hours,
        )
        self.audit.log(
            actor=requester, action=f"approval.request.{action}", target_type="lead",
            target_id=lead_id, detail=f"申请高风险动作：{self.approvals.describe(action)}（{reason}）",
            approval_id=appr.approval_id,
        )
        return appr

    def execute_approved(self, approval_id: str, *, actor: str = "agent") -> dict:
        """审批通过后执行高风险动作。

        执行顺序是硬约束：**先 assert_approved，再动 CRM**。
        反过来的话"审批中"和"已执行"之间会出现窗口期，
        那正是"没批就发了合同"的经典事故路径。
        """
        appr = self.approvals.assert_approved(approval_id)
        if self.crm is None:
            raise FlowError("approval_required", "CRM 客户端未配置，拒绝执行高风险动作")
        payload = appr.payload
        if appr.action == "send_contract":
            result = self.crm.send_wechat_message(
                payload.get("customer_id", ""),
                text=payload.get("text", ""),
                idem_key=make_idempotency_key("contract", approval_id),
            )
        elif appr.action == "confirm_fee":
            result = self.crm.send_wechat_message(
                payload.get("customer_id", ""),
                text=payload.get("text", ""),
                idem_key=make_idempotency_key("fee_confirm", approval_id),
            )
        else:  # 未知高风险动作：默认拒绝执行而不是猜测
            raise FlowError("approval_required", f"未知高风险动作：{appr.action}")
        # 动作真的发出去了才落"已执行"标记（在 CRM 调用之后，
        # 不在之前 —— 顺序反了就会出现"标了已执行但其实没发"）
        self.approvals.mark_executed(approval_id)
        self.audit.log(
            actor=actor, action=f"approval.execute.{appr.action}", target_type="lead",
            target_id=appr.lead_id, detail="高风险动作已执行（审批通过）",
            approval_id=approval_id,
        )
        return result

    # ================================================================== #
    # 六、查询类（无副作用）
    # ================================================================== #
    def progress_query(self, lead_id: str, phone_last4: str) -> dict:
        """客户自助查进度（脱敏）。"""
        snap = self.progress.query(lead_id, phone_last4)
        return snap.to_dict()

    def audit_trail(self, lead_id: str, limit: int = 100) -> list[dict]:
        return self.audit.trail(target_type="lead", target_id=lead_id, limit=limit)

    def list_leads(self, *, stage: str = "", owner: str = "", limit: int = 100) -> list[dict]:
        return [item.to_dict() for item in self.store.list_leads(stage=stage, owner=owner, limit=limit)]

    def get_lead(self, lead_id: str) -> Lead:
        return self._require_lead(lead_id)

    def alerts(self, *, include_acknowledged: bool = False, limit: int = 100) -> list[dict]:
        return [a.to_dict() for a in self.store.list_alerts(
            include_acknowledged=include_acknowledged, limit=limit
        )]

    def pending_approvals(self, limit: int = 50) -> list[dict]:
        return self.approvals.pending(limit=limit)

    # ================================================================== #
    # 内部
    # ================================================================== #
    def _require_lead(self, lead_id: str) -> Lead:
        lead = self.store.get_lead(lead_id)
        if lead is None:
            raise FlowError("lead_not_found", f"线索不存在：{lead_id}")
        return lead

    def _reload(self, lead_id: str) -> Lead:
        return self._require_lead(lead_id)


# =========================================================================== #
# 小工具（审计与重建）
# =========================================================================== #
def _transition(lead_id: str, old: LeadStage, new: LeadStage, *, actor: str, reason: str):
    from flow_ops.domain import StageTransition

    return StageTransition(
        transition_id=f"tr_{int(time.time() * 1000):x}_{lead_id[-4:]}",
        lead_id=lead_id, from_stage=old.value, to_stage=new.value,
        actor=actor, reason=reason, ts=time.time(),
    )


def _lead_from(data: dict) -> Lead:
    return Lead(
        lead_id=data["lead_id"], customer_id=data["customer_id"],
        customer_name=data["customer_name"], phone_masked=data["phone_masked"],
        source=data["source"], owner=data["owner"], stage=LeadStage(data["stage"]),
        human_stage=HumanStage(data["human_stage"]) if data.get("human_stage") else None,
        lost_reason=data.get("lost_reason", ""), note=data.get("note", ""),
        stage_entered_at=data.get("stage_entered_at", 0.0),
        created_at=data.get("created_at", 0.0), updated_at=data.get("updated_at", 0.0),
        payload=data.get("payload", {}),
    )


def _appointment_from(data: dict) -> Appointment:
    return Appointment(
        appointment_id=data["appointment_id"], lead_id=data["lead_id"], store=data["store"],
        scheduled_at=data["scheduled_at"], status=data["status"], note=data.get("note", ""),
        created_at=data.get("created_at", 0.0), updated_at=data.get("updated_at", 0.0),
    )
