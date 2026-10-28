"""项目八（助贷业务流程数字员工）测试。

覆盖策略：**每个验收任务一条测试主线**，与简历 Result 表的四个指标对应：

    一、状态机与 SOP      → 流程自动化（漏跟进检测的判据是确定性代码）
    二、幂等与锁          → 可靠性（重复操作零事故）
    三、审批门            → 人机边界（没批就执行 = 合规事故）
    四、到店前准备与费用   → 到店前 5 分钟（方案初稿来自项目二，不是重新实现）
    五、自助查询          → 隐私口径（脱敏 + 身份校验）
    六、评测与准入        → 回归防护（CI 里跑）
    七、Agent 与 HTTP     → 上线路径（装配、路由、HITL 恢复、接口）

所有用例全离线：SQLite 落 tmp_path，CRM 用 RecordingCrmClient。
"""

from __future__ import annotations

import json
import re
import threading
import time

import pytest

from flow_ops.domain import HumanStage, LeadStage
from flow_ops.leads import (
    FlowError,
    build_sop_rules,
    can_transition,
    overdue_minutes,
    validate_transition,
)
from flow_ops.store import FlowStore
from flow_ops.workflow import FlowEngine, stage_rank


# =========================================================================== #
# 夹具
# =========================================================================== #
@pytest.fixture
def engine(tmp_path):
    """带离线 CRM 的流程引擎（每个用例一个干净库）。"""
    from flow_ops.crm import RecordingCrmClient

    store = FlowStore(tmp_path / "flow.sqlite")
    crm = RecordingCrmClient()
    eng = FlowEngine(
        engine_settings(), store, crm=crm, plan_provider=lambda cid: {}
    )
    yield eng
    store.close()


def engine_settings():
    from agent_platform.config import Settings

    return Settings(
        app_env="local", log_level="ERROR", llm_provider="echo",
        embedding_provider="local", embedding_dim=256,
        checkpointer_backend="memory", vector_backend="local",
    )


def _intake(engine: FlowEngine, *, customer_id: str = "C1002", owner: str = "rep_01"):
    """录入线索，返回 (Lead, 是否幂等命中) —— 与 engine.intake_lead 同构。"""
    return engine.intake_lead(
        customer_id=customer_id, customer_name="测试客户",
        phone_masked="138****5678", source="douyin", owner=owner,
    )


def _advance_to(engine: FlowEngine, lead_id: str, stage: LeadStage):
    """沿合法路径推进到目标阶段（测试里少写样板代码）。"""
    path = [
        LeadStage.ASSIGNED, LeadStage.FIRST_CALLED, LeadStage.WECHAT_ADDED,
        LeadStage.NURTURING, LeadStage.INVITED, LeadStage.ARRIVED,
        LeadStage.PROPOSAL_READY, LeadStage.FEE_ESTIMATED,
    ]
    target_rank = stage_rank(stage)
    for s in path:
        if stage_rank(s) <= target_rank:
            engine.advance(lead_id, s, reason="test-advance")


# =========================================================================== #
# 一、状态机与 SOP
# =========================================================================== #
class TestStateMachine:
    def test_happy_path_is_allowed(self):
        assert can_transition(LeadStage.NEW, LeadStage.ASSIGNED)
        assert can_transition(LeadStage.NURTURING, LeadStage.INVITED)
        assert can_transition(LeadStage.FEE_ESTIMATED, LeadStage.SIGNED)

    def test_skip_stage_is_rejected(self):
        """跳阶段 = 绕过 SOP 留痕点，必须拒绝。"""
        with pytest.raises(FlowError) as exc:
            validate_transition(LeadStage.NEW, LeadStage.SIGNED)
        assert exc.value.code == "illegal_transition"

    def test_backward_is_rejected(self):
        with pytest.raises(FlowError):
            validate_transition(LeadStage.INVITED, LeadStage.ASSIGNED)

    def test_terminal_stage_is_frozen(self):
        for terminal in (LeadStage.SIGNED, LeadStage.LOST):
            with pytest.raises(FlowError):
                validate_transition(terminal, LeadStage.ASSIGNED)

    def test_any_active_stage_can_be_lost(self):
        """客户流失随时可能发生 —— 所有非终态都必须允许 LOST。"""
        for stage in LeadStage:
            if stage.is_terminal:
                continue
            assert can_transition(stage, LeadStage.LOST), stage.value

    def test_stage_order_is_monotonic(self):
        ranks = [stage_rank(s) for s in (
            LeadStage.NEW, LeadStage.ASSIGNED, LeadStage.FIRST_CALLED,
            LeadStage.WECHAT_ADDED, LeadStage.NURTURING, LeadStage.INVITED,
            LeadStage.ARRIVED, LeadStage.PROPOSAL_READY, LeadStage.FEE_ESTIMATED,
        )]
        assert ranks == sorted(ranks)


class TestSop:
    def test_rules_come_from_settings(self):
        settings = engine_settings()
        rules = build_sop_rules(settings)
        by_action = {r.action: r for r in rules}
        assert by_action["first_call"].sla_minutes == settings.flow_first_call_sla_minutes
        assert by_action["add_wechat"].sla_minutes == settings.flow_wechat_sla_hours * 60

    def test_overdue_minutes_floors_at_zero(self):
        """未逾期返回 0 而不是负数 —— 下游不能用负数判"还剩多久"。"""
        assert overdue_minutes(stage_entered_at=time.time(), sla_minutes=30, now=time.time()) == 0

    def test_overdue_counts_past_sla(self):
        entered = time.time() - 45 * 60
        assert overdue_minutes(stage_entered_at=entered, sla_minutes=30, now=time.time()) >= 14

    def test_sweep_alerts_on_overdue(self, engine):
        lead, _ = _intake(engine)
        engine.advance(lead.lead_id, LeadStage.ASSIGNED)
        # 把进入时间改到 40 分钟前（SLA 30 分钟）→ 必须告警
        stored = engine.store.get_lead(lead.lead_id)
        stored.stage_entered_at = time.time() - 40 * 60
        engine.store.update_lead(stored)
        alerts = engine.sweep_sop()
        assert len(alerts) == 1
        assert alerts[0].action == "first_call"
        assert alerts[0].overdue_minutes >= 9

    def test_sweep_is_quiet_when_on_time(self, engine):
        lead, _ = _intake(engine)
        engine.advance(lead.lead_id, LeadStage.ASSIGNED)
        assert engine.sweep_sop() == []

    def test_sweep_does_not_spam_supervisor(self, engine):
        """同一件事只报一次：主管被轰炸几次之后就会学会忽略预警。"""
        lead, _ = _intake(engine)
        engine.advance(lead.lead_id, LeadStage.ASSIGNED)
        stored = engine.store.get_lead(lead.lead_id)
        stored.stage_entered_at = time.time() - 40 * 60
        engine.store.update_lead(stored)
        first = engine.sweep_sop()
        second = engine.sweep_sop()
        assert len(first) == 1
        assert second == []
        # 认领之后宽限期内仍然不重复报（认领 = 有人在处理了，
        # 再立刻报一次只会训练主管忽略预警）
        engine.acknowledge_alert(first[0].alert_id, by="mgr_01")
        third = engine.sweep_sop()
        assert third == []

    def test_sweep_skips_terminal_leads(self, engine):
        lead, _ = _intake(engine)
        engine.advance(lead.lead_id, LeadStage.LOST, reason="客户放弃")
        assert engine.sweep_sop() == []


# =========================================================================== #
# 二、幂等与锁
# =========================================================================== #
class TestIdempotency:
    def test_duplicate_intake_creates_one_lead(self, engine):
        lead1, hit1 = _intake(engine)
        lead2, hit2 = _intake(engine)
        assert hit1 is False and hit2 is True
        assert lead1.lead_id == lead2.lead_id
        assert len(engine.store.list_leads(limit=10)) == 1

    def test_idempotent_hit_returns_current_state(self, engine):
        """重复录入返回的是**当前**状态，不是首次快照。

        客户重复提交留资表单时想知道"我的线索到哪了"，
        拿到过期快照会拿着 new 去推进，撞状态机（报错对人不友好）。
        """
        lead, _ = _intake(engine)
        _advance_to(engine, lead.lead_id, LeadStage.NURTURING)
        again, hit = _intake(engine)
        assert hit is True
        assert again.stage is LeadStage.NURTURING

    def test_idempotent_hit_is_audited(self, engine):
        _intake(engine)
        _intake(engine)
        trail = engine.audit.trail(action="lead.intake", limit=10)
        results = [e["result"] for e in trail]
        assert "idempotent_hit" in results
        assert "ok" in results

    def test_duplicate_invite_keeps_one_appointment(self, engine):
        lead, _ = _intake(engine)
        _advance_to(engine, lead.lead_id, LeadStage.NURTURING)
        when = time.time() + 86_400
        appt1, hit1 = engine.invite(lead.lead_id, store="南京旗舰店", scheduled_at=when)
        appt2, hit2 = engine.invite(lead.lead_id, store="南京旗舰店", scheduled_at=when)
        assert hit1 is False and hit2 is True
        assert appt1.appointment_id == appt2.appointment_id
        assert len(engine.store.list_appointments(lead.lead_id)) == 1

    def test_invite_advances_state_machine(self, engine):
        lead, _ = _intake(engine)
        _advance_to(engine, lead.lead_id, LeadStage.NURTURING)
        engine.invite(lead.lead_id, store="南京旗舰店", scheduled_at=time.time() + 86_400)
        assert engine.store.get_lead(lead.lead_id).stage is LeadStage.INVITED

    def test_duplicate_arrival_confirmation(self, engine):
        lead, _ = _intake(engine)
        _advance_to(engine, lead.lead_id, LeadStage.INVITED)
        engine.invite(lead.lead_id, store="南京旗舰店", scheduled_at=time.time() + 86_400)
        first, hit1 = engine.confirm_arrival(lead.lead_id)
        second, hit2 = engine.confirm_arrival(lead.lead_id)
        assert hit1 is False and hit2 is True
        assert first.stage is LeadStage.ARRIVED
        # 到店即挂起人工接待节点
        assert engine.store.get_lead(lead.lead_id).human_stage is HumanStage.RECEPTION

    def test_explicit_idem_key_wins(self, engine):
        """调用方显式传幂等键（HTTP 的 Idempotency-Key 头）优先于业务键。"""
        lead1, _ = engine.intake_lead(
            customer_id="C1002", customer_name="甲", phone_masked="138****1111",
            source="douyin", owner="rep_01", idem_key="client-key-1",
        )
        lead2, hit = engine.intake_lead(
            customer_id="C1002", customer_name="乙", phone_masked="138****2222",
            source="infoflow", owner="rep_02", idem_key="client-key-1",
        )
        assert hit is True
        assert lead1.lead_id == lead2.lead_id

    def test_lock_blocks_second_owner(self, engine):
        with engine.locks.hold("k1", owner="a"):
            with pytest.raises(FlowError) as exc:
                with engine.locks.hold("k1", owner="b"):
                    pass
        assert exc.value.code == "lock_busy"

    def test_lock_released_after_context(self, engine):
        with engine.locks.hold("k2", owner="a"):
            pass
        with engine.locks.hold("k2", owner="b"):  # 已释放，可以再拿
            pass

    def test_lock_owner_cannot_release_others(self, engine):
        """A 不能释放 B 的锁 —— 否则并发保护失效。"""
        with engine.locks.hold("k3", owner="a"):
            assert engine.store.release_lock("k3", owner="b") is False
            assert engine.store.release_lock("k3", owner="a") is True

    def test_advance_retry_is_idempotent(self, engine):
        """同一个推进请求重试两次：只写一条 transition，第二次返回当前状态。

        没有幂等保护时，第二次会撞 `illegal_transition`（assigned → assigned），
        把网络重试报成业务错误。
        """
        lead, _ = _intake(engine)
        first = engine.advance(lead.lead_id, LeadStage.ASSIGNED, reason="分配")
        again = engine.advance(lead.lead_id, LeadStage.ASSIGNED, reason="分配")
        assert first.stage is LeadStage.ASSIGNED
        assert again.stage is LeadStage.ASSIGNED
        assert len(engine.store.list_transitions(lead.lead_id)) == 1
        assert engine.store.count_idempotent_hits() >= 1

    def test_concurrent_advance_writes_one_transition(self, engine):
        """8 个请求同时推进同一线索：最终只有一条 transition。

        抢不到幂等占位的请求会收到 idem_in_progress（明确告知"未做任何修改"），
        这是可重试的语义，不是数据错乱。
        """
        lead, _ = _intake(engine)
        barrier = threading.Barrier(8)
        errors: list[str] = []

        def worker() -> None:
            barrier.wait()
            try:
                engine.advance(lead.lead_id, LeadStage.ASSIGNED, reason="并发分配")
            except FlowError as exc:
                errors.append(exc.code)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert set(errors) <= {"idem_in_progress", "lock_busy"}
        assert engine.store.get_lead(lead.lead_id).stage is LeadStage.ASSIGNED
        assert len(engine.store.list_transitions(lead.lead_id)) == 1

    def test_write_paths_use_lock(self, engine):
        """三个写路径都必须真正走锁：锁被占用时不允许静默写入。"""
        lead, _ = _intake(engine)
        _advance_to(engine, lead.lead_id, LeadStage.ARRIVED)
        profile = engine_profile(lead.customer_id)

        for key, call in (
            (f"prepare:{lead.lead_id}", lambda: engine.prepare_arrival(lead.lead_id, profile)),
            (f"handoff:{lead.lead_id}",
             lambda: engine.request_handoff(lead.lead_id, HumanStage.NEGOTIATION)),
        ):
            with engine.locks.hold(key, owner="someone-else"):
                with pytest.raises(FlowError) as exc:
                    call()
            assert exc.value.code == "lock_busy"


# =========================================================================== #
# 三、HITL 审批门
# =========================================================================== #
class TestApprovalGate:
    def test_high_risk_actions_come_from_settings(self, engine):
        high = engine.approvals.high_risk_actions()
        assert "send_contract" in high
        assert "confirm_fee" in high

    def test_execute_without_approval_is_blocked(self, engine):
        lead, _ = _intake(engine)
        appr = engine.request_high_risk(
            "send_contract", lead_id=lead.lead_id, payload={"customer_id": "C1002"}
        )
        with pytest.raises(FlowError) as exc:
            engine.execute_approved(appr.approval_id)
        assert exc.value.code == "approval_required"

    def test_reject_blocks_execution(self, engine):
        lead, _ = _intake(engine)
        appr = engine.request_high_risk("send_contract", lead_id=lead.lead_id)
        engine.approvals.decide(appr.approval_id, approver="mgr", approved=False, comment="材料不齐")
        with pytest.raises(FlowError):
            engine.execute_approved(appr.approval_id)

    def test_approval_allows_execution_once(self, engine):
        lead, _ = _intake(engine)
        appr = engine.request_high_risk(
            "confirm_fee", lead_id=lead.lead_id,
            payload={"customer_id": "C1002", "text": "费用确认"},
        )
        engine.approvals.decide(appr.approval_id, approver="mgr", approved=True)
        result = engine.execute_approved(appr.approval_id)
        assert result["ok"] is True
        assert engine.crm.calls_of("send_wechat_message")

    def test_approved_is_not_executed(self, engine):
        """批准 ≠ 已执行：状态是 approved，但 executed_at 仍然是 0。

        这两件事在合规上是不同责任，界面也要能分开显示
        （审批中心「已批准待执行」就是靠这个字段）。
        """
        lead, _ = _intake(engine)
        appr = engine.request_high_risk(
            "send_contract", lead_id=lead.lead_id,
            payload={"customer_id": "C1002", "text": "合同"},
        )
        engine.approvals.decide(appr.approval_id, approver="mgr", approved=True)
        approved = engine.approvals.get(appr.approval_id)
        assert approved is not None
        assert approved.status.value == "approved"
        assert approved.executed_at == 0.0
        assert approved.is_executed is False
        assert approved.to_dict()["is_executed"] is False

        engine.execute_approved(appr.approval_id)
        executed = engine.approvals.get(appr.approval_id)
        assert executed is not None
        assert executed.executed_at > 0
        # 状态本身不变：执行不改变"人批过"这个事实
        assert executed.status.value == "approved"
        assert executed.to_dict()["is_executed"] is True

    def test_mark_executed_keeps_first_timestamp(self, engine):
        """重复执行只保留首次执行时间（幂等，不覆盖证据）。"""
        lead, _ = _intake(engine)
        appr = engine.request_high_risk("send_contract", lead_id=lead.lead_id)
        engine.approvals.decide(appr.approval_id, approver="mgr", approved=True)
        first = engine.approvals.mark_executed(appr.approval_id, now=1000.0)
        again = engine.approvals.mark_executed(appr.approval_id, now=2000.0)
        assert first.executed_at == 1000.0
        assert again.executed_at == 1000.0

    def test_decided_approval_cannot_be_redecided(self, engine):
        lead, _ = _intake(engine)
        appr = engine.request_high_risk("send_contract", lead_id=lead.lead_id)
        engine.approvals.decide(appr.approval_id, approver="mgr", approved=True)
        with pytest.raises(FlowError):
            engine.approvals.decide(appr.approval_id, approver="mgr2", approved=False)

    def test_expired_approval_never_auto_approves(self, engine):
        """过期 ≠ 批准。"没人批"的正确结果是"这事没发生"。"""
        lead, _ = _intake(engine)
        appr = engine.request_high_risk("send_contract", lead_id=lead.lead_id, ttl_hours=0)
        # ttl=0 → expires_at = now，稍后即过期
        expired = engine.approvals.expire_overdue(now=time.time() + 1)
        assert appr.approval_id in expired
        with pytest.raises(FlowError) as exc:
            engine.execute_approved(appr.approval_id)
        assert exc.value.code == "approval_required"

    def test_non_high_risk_action_cannot_request(self, engine):
        lead, _ = _intake(engine)
        with pytest.raises(FlowError):
            engine.request_high_risk("send_sms", lead_id=lead.lead_id)

    def test_request_high_risk_is_audited(self, engine):
        lead, _ = _intake(engine)
        appr = engine.request_high_risk("send_contract", lead_id=lead.lead_id)
        trail = engine.audit.trail(action="approval.request.send_contract", limit=5)
        assert trail and trail[0]["approval_id"] == appr.approval_id

    def test_human_stage_negotiation_forbids_agent_prepare(self, engine, flow_ops_service):
        """谈判节点：Agent 连"重新生成准备包"都不行 —— 材料以业务员确认为准。"""
        lead, _ = _intake(engine)
        engine.request_handoff(lead.lead_id, HumanStage.NEGOTIATION, note="面谈中")
        profile = flow_ops_service.profile_of(lead.customer_id)
        with pytest.raises(FlowError) as exc:
            engine.prepare_arrival(lead.lead_id, profile)
        assert exc.value.code == "human_stage"

    def test_handoff_marks_human_stage(self, engine):
        lead, _ = _intake(engine)
        engine.request_handoff(lead.lead_id, HumanStage.FEE_CONFIRMATION, note="等客户确认")
        stored = engine.store.get_lead(lead.lead_id)
        assert stored.human_stage is HumanStage.FEE_CONFIRMATION


# =========================================================================== #
# 四、到店前准备与费用
# =========================================================================== #
class TestFees:
    def test_annuity_formula_matches_math(self, engine):
        est = engine.readiness.fee_estimator.estimate(
            "ld_x", loan_amount=120_000, months=36, annual_rate=0.072
        )
        # 独立复算（不信任被测实现，用数学定义校验）
        r, n, p = 0.072 / 12, 36, 120_000
        expected = p * r * (1 + r) ** n / ((1 + r) ** n - 1)
        assert abs(est.monthly_payment - expected) <= 1.0

    def test_large_loan_uses_lower_rate(self, engine):
        settings = engine.settings
        small = engine.readiness.fee_estimator.estimate("x", loan_amount=100_000)
        large = engine.readiness.fee_estimator.estimate("x", loan_amount=500_000)
        assert small.service_fee_rate == settings.flow_service_fee_rate
        assert large.service_fee_rate == settings.flow_fee_rate_large_loan
        assert large.service_fee_rate < small.service_fee_rate

    def test_zero_rate_degrades_to_principal_over_months(self, engine):
        est = engine.readiness.fee_estimator.estimate(
            "x", loan_amount=120_000, months=12, annual_rate=0.0
        )
        assert est.monthly_payment == pytest.approx(10_000, abs=0.01)
        assert est.total_interest == 0.0

    def test_total_equals_monthly_times_months(self, engine):
        """总额恒等式：total_repayment == monthly_payment × months。

        Decimal 改造后的口径：月供先取整到分，总额由取整后的月供累加
        （客户每期实还的就是取整后的月供）—— 每期与合计严格对账。
        """
        est = engine.readiness.fee_estimator.estimate(
            "x", loan_amount=250_000, months=24, annual_rate=0.068
        )
        assert est.total_repayment == pytest.approx(est.monthly_payment * 24, abs=0.001)

    def test_amounts_are_cent_quantized(self, engine):
        """所有金额逐分取整：任何字段都不该出现第三位小数（半分钱事故）。"""
        est = engine.readiness.fee_estimator.estimate(
            "x", loan_amount=123_456.78, months=13, annual_rate=0.0755
        )
        for value in (
            est.monthly_payment, est.total_repayment,
            est.total_interest, est.service_fee, est.net_amount,
        ):
            assert round(value, 2) == value

    def test_net_amount_excludes_service_fee(self, engine):
        est = engine.readiness.fee_estimator.estimate("x", loan_amount=200_000)
        assert est.net_amount == pytest.approx(est.loan_amount - est.service_fee, abs=0.01)

    def test_disclaimer_is_part_of_the_model(self, engine):
        """预估 ≠ 报价 —— 免责声明是字段，不是可选 UI 文案。"""
        est = engine.readiness.fee_estimator.estimate("x", loan_amount=100_000)
        assert "以银行审批" in est.disclaimer

    def test_invalid_amount_rejected(self, engine):
        with pytest.raises(ValueError):
            engine.readiness.fee_estimator.estimate("x", loan_amount=0)

    def test_estimate_advances_state_machine(self, engine):
        lead, _ = _intake(engine)
        _advance_to(engine, lead.lead_id, LeadStage.ARRIVED)
        engine.prepare_arrival(lead.lead_id, engine_profile(lead.customer_id))
        assert engine.store.get_lead(lead.lead_id).stage is LeadStage.PROPOSAL_READY
        engine.estimate_fee(lead.lead_id, loan_amount=100_000)
        assert engine.store.get_lead(lead.lead_id).stage is LeadStage.FEE_ESTIMATED


class TestReadiness:
    def test_pack_contains_profile_and_plan(self, engine, flow_ops_service):
        lead, _ = _intake(engine)
        _advance_to(engine, lead.lead_id, LeadStage.ARRIVED)
        profile = engine_profile(lead.customer_id)
        pack = engine.prepare_arrival(lead.lead_id, profile)
        assert pack.lead_id == lead.lead_id
        assert pack.highlights is not None
        # 准备包必须包含费用预估（客户有意向额度时）
        assert pack.fee_estimate is not None
        assert pack.warnings is not None

    def test_plan_draft_comes_from_project_two(self, engine):
        """方案初稿来自注入的 provider（生产里是项目二），本项目不自己匹配。"""
        seen = []

        def provider(cid):
            seen.append(cid)
            return {"product_code": "BANK-A-01", "product_name": "测试产品",
                    "summary": "摘要", "citations": [], "rejected": []}

        engine.readiness._plan_provider = provider
        lead, _ = _intake(engine)
        _advance_to(engine, lead.lead_id, LeadStage.ARRIVED)
        pack = engine.prepare_arrival(lead.lead_id, engine_profile(lead.customer_id))
        assert seen == [lead.customer_id]
        assert pack.plan_draft.product_name == "测试产品"

    def test_plan_provider_failure_degrades_to_handoff(self, engine):
        """项目二挂了不能炸掉准备流程：降级为"转人工"，并写进 warnings。"""
        def broken(cid):
            raise RuntimeError("项目二暂不可用")

        engine.readiness._plan_provider = broken
        lead, _ = _intake(engine)
        _advance_to(engine, lead.lead_id, LeadStage.ARRIVED)
        pack = engine.prepare_arrival(lead.lead_id, engine_profile(lead.customer_id))
        assert pack.plan_draft.handoff_reason
        assert any("方案" in w for w in pack.warnings)

    def test_profile_from_dict_never_invents_qualifications(self, engine):
        """档案缺失时给安全默认值，绝不替客户"补"一个公积金记录。"""
        profile = engine.readiness.profile_from_dict({"customer_id": "C1", "name": "张三"})
        assert profile.housing_fund_months == 0
        assert profile.has_property is False
        assert profile.need_amount == 0.0

    def test_highlights_and_risks(self, engine):
        profile = engine.readiness.profile_from_dict({
            "customer_id": "C1", "name": "张三", "housing_fund_months": 24,
            "credit_overdue_times": 2, "microloan_count": 5,
        })
        assert any("公积金" in h for h in profile.highlights())
        assert any("逾期" in r for r in profile.risks())


def engine_profile(customer_id: str):
    from flow_ops.readiness import ReadinessBuilder

    return ReadinessBuilder.profile_from_dict({
        "customer_id": customer_id, "name": "测试客户",
        "housing_fund_months": 18, "credit_overdue_times": 1,
        "need_amount": 200_000,
    })


# =========================================================================== #
# 五、自助查询（隐私口径）
# =========================================================================== #
class TestProgress:
    def test_wrong_last4_is_rejected_without_hint(self, engine):
        lead, _ = _intake(engine)
        with pytest.raises(FlowError) as exc:
            engine.progress_query(lead.lead_id, "0000")
        assert exc.value.code == "forbidden"
        # 刻意不说"哪一位错了"—— 防枚举
        assert "后 4 位" in exc.value.message or "校验" in exc.value.message

    def test_correct_query_returns_masked_view(self, engine):
        lead, _ = _intake(engine)
        _advance_to(engine, lead.lead_id, LeadStage.NURTURING)
        snap = engine.progress_query(lead.lead_id, "5678")
        assert snap["current_stage"] == "nurturing"
        assert snap["customer_name"] == "测***"       # 姓名脱敏（4 字名留首字）
        # 断言"没有完整手机号"，不是"没有 138 子串"—— 时间戳
        # （如 1792257697.2013893）里偶然含 "138" 会让老断言随机失败
        assert re.search(r"1[3-9]\d{9}", json.dumps(snap, ensure_ascii=False)) is None
        assert snap["next_step"]

    def test_timeline_hides_internal_stages(self, engine):
        """NEW（还没分配）不进客户时间线 —— 客户不需要知道内部待办。"""
        lead, _ = _intake(engine)
        engine.advance(lead.lead_id, LeadStage.ASSIGNED)
        snap = engine.progress_query(lead.lead_id, "5678")
        stages = [t["stage"] for t in snap["timeline"]]
        assert "assigned" in stages
        assert "new" not in stages

    def test_unknown_lead_is_404(self, engine):
        with pytest.raises(FlowError) as exc:
            engine.progress_query("ld_missing", "5678")
        assert exc.value.code == "lead_not_found"


# =========================================================================== #
# 六、审计
# =========================================================================== #
class TestAudit:
    def test_full_trail_is_queryable(self, engine):
        lead, _ = _intake(engine)
        engine.advance(lead.lead_id, LeadStage.ASSIGNED, reason="分配")
        trail = engine.audit_trail(lead.lead_id)
        actions = [e["action"] for e in trail]
        assert "lead.intake" in actions
        assert "lead.advance" in actions

    def test_blocked_actions_are_counted(self, engine):
        lead, _ = _intake(engine)
        try:
            engine.advance(lead.lead_id, LeadStage.SIGNED)
        except FlowError:
            pass
        # 非法流转不进 audit（它没发生），但 SOP 拦截与幂等拦截会进
        assert engine.audit.trail(action="lead.intake")

    def test_audit_entry_has_actor_and_target(self, engine):
        lead, _ = _intake(engine, owner="rep_09")
        entry = engine.audit.trail(action="lead.intake")[0]
        assert entry["actor"] == "system"
        assert entry["target_id"] == lead.lead_id
        assert entry["target_type"] == "lead"


# =========================================================================== #
# 七、CRM 工具层
# =========================================================================== #
class TestCrmLayer:
    def test_intake_notifies_crm(self, engine):
        lead, _ = _intake(engine)
        calls = engine.crm.calls_of("assign_owner")
        assert calls and calls[0]["owner"] == "rep_01"

    def test_invite_syncs_to_crm(self, engine):
        lead, _ = _intake(engine)
        _advance_to(engine, lead.lead_id, LeadStage.NURTURING)
        engine.invite(lead.lead_id, store="南京旗舰店", scheduled_at=time.time() + 86_400)
        assert engine.crm.calls_of("create_appointment")

    def test_crm_never_logs_message_content(self, engine):
        """对外发送只记长度不记内容 —— 沟通原文不进日志。"""
        engine.crm.send_wechat_message("C1002", text="您的合同已发送，请查收", idem_key="k")
        call = engine.crm.calls_of("send_wechat_message")[0]
        assert "text_length" in call
        assert "text" not in call


# =========================================================================== #
# 八、评测与准入
# =========================================================================== #
class TestEvaluation:
    def test_builtin_cases_cover_all_acceptance_tasks(self):
        from flow_ops.evaluation import builtin_cases

        cases = builtin_cases(engine_settings())
        tags = {t for c in cases for t in c.tags}
        assert {"sop", "state_machine", "reliability", "fee", "hitl", "privacy"} <= tags

    def test_all_builtin_cases_pass(self, flow_ops_service):
        report = flow_ops_service.evaluate()
        assert report["total"] >= 10
        assert report["passed"] == report["total"], [
            (r["case_id"], r["failures"]) for r in report["results"] if not r["passed"]
        ]

    def test_gate_first_run_records_baseline(self, tmp_path):
        from flow_ops.service import FlowOpsService

        settings = Settings_local(tmp_path)
        svc = FlowOpsService(settings=settings)
        svc.bootstrap()
        try:
            decision = svc.gate()
            assert decision["allowed"] is True
            assert (tmp_path / "flow" / "eval_baseline.json").is_file()
        finally:
            svc.close()

    def test_gate_blocks_new_failure(self, tmp_path):
        """出现基线里没有的新失败用例 → 拒发（防止删用例凑通过率）。"""
        from flow_ops.domain import EvalReport
        from flow_ops.evaluation import compare

        baseline = EvalReport(total=10, passed=10, avg_score=1.0)
        from flow_ops.domain import EvalCaseResult

        current = EvalReport(
            total=10, passed=10, avg_score=1.0,
            results=[EvalCaseResult(case_id="new_case", title="新用例", passed=False, score=0.0)],
        )
        decision = compare(baseline, current)
        assert decision.allowed is False
        assert any("新失败" in r for r in decision.reasons)

    def test_gate_blocks_regression(self):
        from flow_ops.domain import EvalReport
        from flow_ops.evaluation import compare

        baseline = EvalReport(total=10, passed=10, avg_score=1.0)
        current = EvalReport(total=10, passed=7, avg_score=0.7)
        decision = compare(baseline, current, max_regression=0.03)
        assert decision.allowed is False
        assert any("退化" in r for r in decision.reasons)


def Settings_local(tmp_path):
    from agent_platform.config import Settings

    s = Settings(
        app_env="local", log_level="ERROR", llm_provider="echo",
        embedding_provider="local", embedding_dim=256,
        checkpointer_backend="memory", vector_backend="local",
        policy_data_dir=str(tmp_path / "policies"),
        profile_data_dir=str(tmp_path / "profiles"),
        flow_data_dir=str(tmp_path / "flow"),
        flow_sqlite_path=str(tmp_path / "flow" / "flow.sqlite"),
    )
    s.ensure_dirs()
    return s


# =========================================================================== #
# 九、工具注册
# =========================================================================== #
class TestToolRegistration:
    def test_fourteen_tools_registered(self, flow_ops_service):
        from agent_platform.tools.registry import ToolRegistry

        registry = ToolRegistry()
        names = flow_ops_service.register_tools(registry)
        assert len(names) == 14
        assert all(n.startswith("flow.") for n in names)

    def test_high_risk_tools_are_marked(self, flow_ops_service):
        from agent_platform.tools.registry import ToolRegistry

        registry = ToolRegistry()
        flow_ops_service.register_tools(registry)
        for name in ("flow.request_contract", "flow.confirm_fee", "flow.execute_approved"):
            spec = registry.get(name)
            assert spec.risk_level == "high"
            assert spec.idempotent is False  # 非幂等 + 高风险 = 平台必挂 HITL

    def test_handoff_tool_carries_escalation_tag(self, flow_ops_service):
        """逃生舱：任何路由下都能转人工，绝不因裁剪而消失。"""
        from agent_platform.tools.registry import ToolRegistry

        registry = ToolRegistry()
        flow_ops_service.register_tools(registry)
        assert "escalation" in registry.get("flow.request_handoff").tags


# =========================================================================== #
# 十、Agent 路由与作用域
# =========================================================================== #
class TestAgentRouting:
    def _agent(self, flow_ops_service):
        from agent_platform.tools.registry import ToolRegistry
        from flow_ops.agent import build_flow_ops_agent, route, tool_tags_for

        registry = ToolRegistry()
        flow_ops_service.register_tools(registry)
        return build_flow_ops_agent(flow_ops_service, registry), route, tool_tags_for

    def test_route_classification(self, flow_ops_service):
        _, route, _ = self._agent(flow_ops_service)
        assert route({"user_input": "有没有漏跟进的"}) == "sop"
        assert route({"user_input": "帮我录一条新线索"}) == "intake"
        assert route({"user_input": "预约到南京门店"}) == "invite"
        assert route({"user_input": "到店前准备一下"}) == "readiness"
        assert route({"user_input": "客户问进度到哪了"}) == "progress"
        assert route({"user_input": "审批一下"}) == "approve"
        assert route({"user_input": "查一下审计留痕"}) == "audit"

    def test_sop_beats_metrics(self, flow_ops_service):
        """"漏跟进"是流程语义（该扫 SOP），不是看板语义。"""
        _, route, _ = self._agent(flow_ops_service)
        assert route({"user_input": "漏跟进率怎么样"}) == "sop"

    def test_scoped_tools_exclude_high_risk_on_normal_routes(self, flow_ops_service):
        """非审批路由看不到高风险工具 —— Agent 想越权也拿不到。"""
        agent, _, _ = self._agent(flow_ops_service)
        state = {"route": "sop", "user_input": ""}
        scoped = {t.name for t in agent.resolve_scoped_tools(state)}
        assert "flow.check_sop" in scoped
        assert "flow.request_contract" not in scoped
        assert "flow.confirm_fee" not in scoped

    def test_approve_route_sees_high_risk_tools(self, flow_ops_service):
        agent, _, _ = self._agent(flow_ops_service)
        state = {"route": "approve", "user_input": ""}
        scoped = {t.name for t in agent.resolve_scoped_tools(state)}
        assert "flow.request_contract" in scoped
        assert "flow.approve" in scoped

    def test_escalation_always_available(self, flow_ops_service):
        """逃生舱在任何路由下都可见（含被裁剪的路由）。"""
        agent, _, _ = self._agent(flow_ops_service)
        for route_name in ("sop", "readiness", "progress"):
            state = {"route": route_name, "user_input": ""}
            scoped = {t.name for t in agent.resolve_scoped_tools(state)}
            assert "flow.request_handoff" in scoped

    def test_decide_is_pure_function(self, flow_ops_service):
        """同样输入 → 同样输出（HITL 恢复会重放 decide）。"""
        agent, _, _ = self._agent(flow_ops_service)
        state = {"route": "sop", "user_input": "", "observations": []}
        a1 = agent.decide(state, agent.tools.all())
        a2 = agent.decide(dict(state), agent.tools.all())
        assert a1.tool == a2.tool and a1.arguments == a2.arguments

    def test_decide_finishes_after_action_done(self, flow_ops_service):
        """动作已执行过就收手 —— 否则会在执行循环里反复调同一工具直到熔断。"""
        agent, _, _ = self._agent(flow_ops_service)
        state = {
            "route": "approve", "user_input": "给 ld_x 发合同",
            "observations": [{"tool": "flow.request_contract", "ok": True}],
        }
        assert agent.decide(state, agent.tools.all()).tool is None

    def test_decide_intake_requires_customer_id(self, flow_ops_service):
        agent, _, _ = self._agent(flow_ops_service)
        state = {"route": "intake", "user_input": "录一条线索", "observations": []}
        assert agent.decide(state, agent.tools.all()).tool is None

    def test_decide_never_fabricates_customer_info(self, flow_ops_service):
        """Agent 不编造姓名/手机号 —— 缺的信息留给人工补。"""
        agent, _, _ = self._agent(flow_ops_service)
        state = {"route": "intake", "user_input": "C1002", "observations": []}
        action = agent.decide(state, agent.tools.all())
        assert action.tool == "flow.intake_lead"
        assert action.arguments["customer_name"] == "待补充"
        assert action.arguments["phone_masked"] == "待补充"


# =========================================================================== #
# 十一、平台编排端到端（含 HITL 恢复）
# =========================================================================== #
class TestPlatformIntegration:
    def _runtime(self, settings, flow_ops_service):
        from agent_platform.runtime import AgentRuntime
        from flow_ops.agent import build_flow_ops_agent

        runtime = AgentRuntime(settings=settings)
        flow_ops_service.register_tools(runtime.tools)
        runtime.register(build_flow_ops_agent(flow_ops_service, runtime.tools))
        return runtime

    def test_sop_route_completes(self, settings, flow_ops_service):
        runtime = self._runtime(settings, flow_ops_service)
        result = runtime.invoke("flow_ops", "帮我看看有没有漏跟进的线索")
        assert result.status == "completed"
        assert result.output["route"] == "sop"
        assert result.output["tool"] == "flow.check_sop"

    def test_invite_route_completes(self, settings, flow_ops_service):
        runtime = self._runtime(settings, flow_ops_service)
        lead = flow_ops_service.intake_lead(
            customer_id="C1002", customer_name="王强", phone_masked="138****5678",
            source="douyin", owner="rep_01",
        )
        for st in ("assigned", "first_called", "wechat_added", "nurturing"):
            flow_ops_service.advance(lead["lead_id"], st, reason="test")
        result = runtime.invoke(
            "flow_ops", f"把 {lead['lead_id']} 预约到南京门店"
        )
        assert result.status == "completed"
        assert result.output["route"] == "invite"
        assert result.output["output"]["store"] == "南京旗舰店"

    def test_contract_route_hits_approval_then_resumes(self, settings, flow_ops_service):
        """高风险工具 → 挂起 → 审批通过 → 恢复执行 → 完成。

        这条同时锁定平台 HITL 的恢复语义：恢复后必须采用已有结论，
        绝不再挂起一次（否则"批了也执行不了"）。
        """
        runtime = self._runtime(settings, flow_ops_service)
        lead = flow_ops_service.intake_lead(
            customer_id="C1002", customer_name="王强", phone_masked="138****5678",
            source="douyin", owner="rep_01",
        )
        first = runtime.invoke("flow_ops", f"给 {lead['lead_id']} 发合同")
        assert first.status == "interrupted"
        assert first.pending_approval["tool"] == "flow.request_contract"

        resumed = runtime.resume(
            first.thread_id, {"approved": True, "decided_by": "张主管", "comment": "已核对"}
        )
        assert resumed.status == "completed"
        assert resumed.approvals and resumed.approvals[0]["approved"] is True
        assert resumed.output["route"] == "approve"

    def test_rejected_contract_does_not_execute(self, settings, flow_ops_service):
        runtime = self._runtime(settings, flow_ops_service)
        lead = flow_ops_service.intake_lead(
            customer_id="C1002", customer_name="王强", phone_masked="138****5678",
            source="douyin", owner="rep_01",
        )
        first = runtime.invoke("flow_ops", f"给 {lead['lead_id']} 发合同")
        resumed = runtime.resume(
            first.thread_id, {"approved": False, "decided_by": "张主管", "comment": "材料不齐"}
        )
        assert resumed.status == "completed"
        assert resumed.approvals[0]["approved"] is False
        # 工具没有被真正执行
        assert not any(t["ok"] for t in resumed.tool_calls)


# =========================================================================== #
# 十二、HTTP 接口
# =========================================================================== #
@pytest.fixture
def client(app_context_fixture):
    from fastapi.testclient import TestClient

    from agent_platform.api.app import app

    return TestClient(app)


def _intake_via_api(client) -> str:
    resp = client.post("/flow/leads", json={
        "customer_id": "C1002", "customer_name": "王强",
        "phone_masked": "138****5678", "source": "douyin", "owner": "rep_01",
    })
    assert resp.status_code == 200
    return resp.json()["lead_id"]


class TestHttpApi:
    def test_intake_and_list(self, client):
        lead_id = _intake_via_api(client)
        listing = client.get("/flow/leads").json()
        assert listing["count"] >= 1
        assert any(item["lead_id"] == lead_id for item in listing["leads"])

    def test_intake_idempotency_key_header(self, client):
        body = {"customer_id": "C9001", "customer_name": "甲",
                "phone_masked": "138****9999", "source": "douyin", "owner": "rep_01"}
        r1 = client.post("/flow/leads", json=body, headers={"Idempotency-Key": "http-key-1"})
        r2 = client.post("/flow/leads", json=body, headers={"Idempotency-Key": "http-key-1"})
        assert r1.json()["lead_id"] == r2.json()["lead_id"]
        assert r2.json()["idempotent_hit"] is True

    def test_advance_rejects_illegal_transition(self, client):
        lead_id = _intake_via_api(client)
        resp = client.post(f"/flow/leads/{lead_id}/advance", json={"to_stage": "signed"})
        assert resp.status_code == 409
        assert "非法流转" in resp.json()["detail"]

    def test_advance_rejects_unknown_stage(self, client):
        lead_id = _intake_via_api(client)
        resp = client.post(f"/flow/leads/{lead_id}/advance", json={"to_stage": "nope"})
        assert resp.status_code == 400

    def test_invite_and_arrive(self, client):
        lead_id = _intake_via_api(client)
        for st in ("assigned", "first_called", "wechat_added", "nurturing"):
            client.post(f"/flow/leads/{lead_id}/advance", json={"to_stage": st})
        when = time.time() + 86_400
        resp = client.post(f"/flow/leads/{lead_id}/invite",
                           json={"store": "南京旗舰店", "scheduled_at": when})
        assert resp.status_code == 200
        assert resp.json()["status"] == "booked"
        arrived = client.post(f"/flow/leads/{lead_id}/arrived").json()
        assert arrived["stage"] == "arrived"
        assert arrived["human_stage"] == "reception"

    def test_readiness_and_fee(self, client):
        lead_id = _intake_via_api(client)
        for st in ("assigned", "first_called", "wechat_added", "nurturing", "invited"):
            client.post(f"/flow/leads/{lead_id}/advance", json={"to_stage": st})
        client.post(f"/flow/leads/{lead_id}/arrived")
        pack = client.post(f"/flow/leads/{lead_id}/readiness").json()
        assert pack["pack_id"].startswith("pk_")
        assert "profile" in pack and "highlights" in pack
        fee = client.post(f"/flow/leads/{lead_id}/fee",
                          json={"loan_amount": 300_000}).json()
        assert fee["service_fee"] > 0
        assert "以银行审批" in fee["disclaimer"]

    def test_hitl_full_cycle_over_http(self, client):
        lead_id = _intake_via_api(client)
        req = client.post(f"/flow/leads/{lead_id}/request-contract",
                          json={"reason": "客户要求"}).json()
        assert req["status"] == "pending"

        # 未审批就执行 → 409
        blocked = client.post(f"/flow/approvals/{req['approval_id']}/execute")
        assert blocked.status_code == 409

        # 审批通过后执行 → 200
        client.post(f"/flow/approvals/{req['approval_id']}/decide",
                    json={"approver": "mgr", "approved": True, "comment": "同意"})
        ok = client.post(f"/flow/approvals/{req['approval_id']}/execute")
        assert ok.status_code == 200
        assert ok.json()["ok"] is True

    def test_rejected_approval_blocks_execution(self, client):
        lead_id = _intake_via_api(client)
        req = client.post(f"/flow/leads/{lead_id}/request-contract",
                          json={"reason": "测试"}).json()
        client.post(f"/flow/approvals/{req['approval_id']}/decide",
                    json={"approver": "mgr", "approved": False, "comment": "不批"})
        blocked = client.post(f"/flow/approvals/{req['approval_id']}/execute")
        assert blocked.status_code == 409

    def test_progress_requires_correct_last4(self, client):
        lead_id = _intake_via_api(client)
        wrong = client.post("/flow/progress",
                            json={"lead_id": lead_id, "phone_last4": "0000"})
        assert wrong.status_code == 403
        right = client.post("/flow/progress",
                            json={"lead_id": lead_id, "phone_last4": "5678"})
        assert right.status_code == 200
        assert right.json()["customer_name"] == "王*"

    def test_sop_sweep_and_alerts(self, client):
        lead_id = _intake_via_api(client)
        client.post(f"/flow/leads/{lead_id}/advance", json={"to_stage": "assigned"})
        swept = client.post("/flow/sop/sweep").json()
        assert swept["count"] == 0  # 刚推进，未逾期
        listing = client.get("/flow/sop/alerts").json()
        assert listing["count"] == 0

    def test_audit_and_metrics(self, client):
        lead_id = _intake_via_api(client)
        audit = client.get("/flow/audit", params={"lead_id": lead_id}).json()
        assert audit["count"] >= 1
        assert audit["entries"][0]["action"] in ("lead.intake", "lead.advance")
        metrics = client.get("/flow/metrics").json()
        assert "missed_followup_rate" in metrics
        assert "leads" in metrics

    def test_eval_and_gate(self, client):
        report = client.post("/flow/eval").json()
        assert report["passed"] == report["total"]
        gate = client.post("/flow/eval/gate").json()
        assert gate["allowed"] is True

    def test_unknown_lead_returns_404(self, client):
        resp = client.get("/flow/leads/ld_missing")
        assert resp.status_code == 404

    def test_stats(self, client):
        stats = client.get("/flow/stats").json()
        assert "store" in stats and "metrics" in stats
        assert "send_contract" in stats["high_risk_actions"]
