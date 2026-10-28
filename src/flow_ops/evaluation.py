"""流程行为评测（项目八的 Golden Set 与准入门禁）。

与项目一/项目六的评测哲学一致，但断言类型不同：
    项目一评"回答得好不好"（有主观分）
    本项目评"流程做得对不对"（**纯布尔**）

一个流程系统的正确性是布尔问题：该告警的没告警 = 漏跟进，
没审批就发合同 = 合规事故。这些没有"部分正确"。

**准入三条拒发理由**（CI 里跑，对应简历"CI 准入"）：
    ① 通过率相对基线退化超过阈值；
    ② 任一基线通过的用例本次失败；
    ③ 出现基线里没有的新失败用例（防止"删掉用例凑通过率"）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from agent_platform.config import Settings
from agent_platform.logging_config import get_logger
from flow_ops.domain import (
    EvalCase,
    EvalCaseResult,
    EvalReport,
    GateDecision,
    HumanStage,
    LeadStage,
)
from flow_ops.leads import FlowError
from flow_ops.workflow import FlowEngine

logger = get_logger(__name__)

#: 引擎工厂：每个用例一个全新的库，保证用例之间零干扰
EngineFactory = Callable[[], FlowEngine]

_CUSTOMER = {
    "customer_id": "C9001",
    "customer_name": "测试客户",
    "phone_masked": "138****5678",
    "source": "douyin",
    "owner": "rep_01",
}


# =========================================================================== #
# 一、内置用例
# =========================================================================== #
def builtin_cases(settings: Settings) -> list[EvalCase]:
    """内置 Golden Set：十一条流程行为用例。

    每条都对应一个**真实发生过或可能发生**的故障模式，
    不是为凑数量编的（面试被问"你们测什么"要能一条条讲出来）。
    """
    sla = settings.flow_first_call_sla_minutes
    return [
        EvalCase(
            case_id="flow_sop_overdue",
            title="首呼超时必须告警（SOP 兜底）",
            setup={"kind": "sop", "stage": "assigned", "elapsed_minutes": sla + 15},
            expect={"alerts_gte": 1, "alert_action": "first_call"},
            tags=("sop",),
        ),
        EvalCase(
            case_id="flow_sop_on_time",
            title="未超时不得告警（防预警疲劳）",
            setup={"kind": "sop", "stage": "assigned", "elapsed_minutes": max(sla - 10, 1)},
            expect={"alerts_eq": 0},
            tags=("sop",),
        ),
        EvalCase(
            case_id="flow_illegal_transition",
            title="跳过 SOP 的流转必须被拒绝",
            setup={"kind": "illegal"},
            expect={"error_code": "illegal_transition"},
            tags=("state_machine",),
        ),
        EvalCase(
            case_id="flow_idempotent_intake",
            title="重复录入只生成一条线索",
            setup={"kind": "idempotent_intake"},
            expect={"leads_eq": 1, "idem_hit": True},
            tags=("reliability",),
        ),
        EvalCase(
            case_id="flow_invite_idempotent",
            title="重复邀约只保留一条预约",
            setup={"kind": "invite_idempotent"},
            expect={"appointments_eq": 1},
            tags=("reliability",),
        ),
        EvalCase(
            case_id="flow_fee_formula",
            title="等额本息月供计算正确（可复现）",
            setup={"kind": "fee_formula", "amount": 120_000, "months": 36, "rate": 0.072},
            expect={"monthly_payment": 3_716.71, "tolerance": 1.0},
            tags=("fee",),
        ),
        EvalCase(
            case_id="flow_fee_tier",
            title="大额单走低费率档（口径一致）",
            setup={"kind": "fee_tier", "amount": 500_000},
            expect={"rate": settings.flow_fee_rate_large_loan},
            tags=("fee",),
        ),
        EvalCase(
            case_id="flow_hitl_blocks",
            title="未审批就执行高风险动作必须被拦",
            setup={"kind": "hitl_block"},
            expect={"error_code": "approval_required"},
            tags=("hitl",),
        ),
        EvalCase(
            case_id="flow_hitl_executes_after_approval",
            title="审批通过后动作可执行",
            setup={"kind": "hitl_approve"},
            expect={"executed": True},
            tags=("hitl",),
        ),
        EvalCase(
            case_id="flow_progress_desensitized",
            title="自助查询身份校验 + 脱敏返回",
            setup={"kind": "progress"},
            expect={"wrong_key_blocked": True, "name_masked": True, "no_full_phone": True},
            tags=("privacy",),
        ),
        EvalCase(
            case_id="flow_human_stage_forbidden",
            title="谈判节点禁止 Agent 自动准备",
            setup={"kind": "human_stage"},
            expect={"error_code": "human_stage"},
            tags=("hitl",),
        ),
    ]


# =========================================================================== #
# 二、评测器
# =========================================================================== #
class FlowEvaluator:
    """流程行为评测器。"""

    def __init__(self, engine_factory: EngineFactory) -> None:
        self._engine_factory = engine_factory

    # ------------------------------------------------------------------ #
    # 单用例
    # ------------------------------------------------------------------ #
    def run_case(self, case: EvalCase) -> EvalCaseResult:
        engine = self._engine_factory()
        failures: list[str] = []
        detail: dict[str, Any] = {}
        try:
            failures, detail = self._execute(engine, case)
        except Exception as exc:  # noqa: BLE001 - 用例级异常算失败，不能炸飞整轮
            failures.append(f"用例执行异常：{exc}")
        passed = not failures
        return EvalCaseResult(
            case_id=case.case_id, title=case.title, passed=passed,
            score=1.0 if passed else 0.0, failures=failures, detail=detail,
        )

    def _execute(self, engine: FlowEngine, case: EvalCase) -> tuple[list[str], dict[str, Any]]:
        kind = case.setup.get("kind", "")
        handler = getattr(self, f"_case_{kind}", None)
        if handler is None:
            return [f"未知用例类型：{kind}"], {}
        return handler(engine, case)

    # ------------------------------------------------------------------ #
    # 各用例实现（一个方法一个故障模式）
    # ------------------------------------------------------------------ #
    def _intake(self, engine: FlowEngine, **kw) -> Any:
        return engine.intake_lead(**_CUSTOMER, note=kw.get("note", ""))

    def _case_sop(self, engine: FlowEngine, case: EvalCase) -> tuple[list[str], dict]:
        lead, _ = self._intake(engine)
        # 直接改 stage_entered_at 模拟"已进入某阶段 N 分钟"
        lead = engine.get_lead(lead.lead_id)
        lead.stage = LeadStage(case.setup["stage"])
        lead.stage_entered_at = (
            time.time() - case.setup["elapsed_minutes"] * 60
        )
        engine.store.update_lead(lead)
        alerts = engine.sweep_sop()
        expect = case.expect
        failures: list[str] = []
        detail = {"alerts": [a.action for a in alerts]}
        if "alerts_gte" in expect and len(alerts) < expect["alerts_gte"]:
            failures.append(f"应产生 ≥{expect['alerts_gte']} 条预警，实际 {len(alerts)}")
        if "alerts_eq" in expect and len(alerts) != expect["alerts_eq"]:
            failures.append(f"应产生 {expect['alerts_eq']} 条预警，实际 {len(alerts)}")
        if "alert_action" in expect and alerts:
            if alerts[0].action != expect["alert_action"]:
                failures.append(f"预警动作应为 {expect['alert_action']}，实际 {alerts[0].action}")
        return failures, detail

    def _case_illegal(self, engine: FlowEngine, case: EvalCase) -> tuple[list[str], dict]:
        lead, _ = self._intake(engine)
        failures: list[str] = []
        try:
            engine.advance(lead.lead_id, LeadStage.SIGNED, actor="test", reason="试图跳过 SOP")
            failures.append("new → signed 竟被放行（SOP 可被绕过）")
        except FlowError as exc:
            if exc.code != case.expect["error_code"]:
                failures.append(f"错误码应为 {case.expect['error_code']}，实际 {exc.code}")
        return failures, {"lead_id": lead.lead_id}

    def _case_idempotent_intake(self, engine: FlowEngine, case: EvalCase) -> tuple[list[str], dict]:
        lead1, hit1 = self._intake(engine)
        lead2, hit2 = self._intake(engine)
        leads = engine.store.list_leads(limit=10)
        failures: list[str] = []
        if len(leads) != case.expect["leads_eq"]:
            failures.append(f"应只有 1 条线索，实际 {len(leads)}")
        if not hit2:
            failures.append("第二次录入应命中幂等缓存")
        if lead1.lead_id != lead2.lead_id:
            failures.append("两次返回的 lead_id 不一致")
        return failures, {"leads": len(leads), "hit2": hit2, "hit1": hit1}

    def _case_invite_idempotent(self, engine: FlowEngine, case: EvalCase) -> tuple[list[str], dict]:
        lead, _ = self._intake(engine)
        engine.advance(lead.lead_id, LeadStage.ASSIGNED, actor="test")
        engine.advance(lead.lead_id, LeadStage.FIRST_CALLED, actor="test")
        engine.advance(lead.lead_id, LeadStage.WECHAT_ADDED, actor="test")
        engine.advance(lead.lead_id, LeadStage.NURTURING, actor="test")
        when = time.time() + 86_400
        appt1, hit1 = engine.invite(lead.lead_id, store="南京旗舰店", scheduled_at=when)
        appt2, hit2 = engine.invite(lead.lead_id, store="南京旗舰店", scheduled_at=when)
        appointments = engine.store.list_appointments(lead.lead_id)
        failures: list[str] = []
        if len(appointments) != case.expect["appointments_eq"]:
            failures.append(f"应只有 1 条邀约，实际 {len(appointments)}")
        if not hit2:
            failures.append("第二次邀约应命中幂等缓存")
        if appt1.appointment_id != appt2.appointment_id:
            failures.append("两次返回的预约 ID 不一致")
        return failures, {"appointments": len(appointments), "hit2": hit2}

    def _case_fee_formula(self, engine: FlowEngine, case: EvalCase) -> tuple[list[str], dict]:
        est = engine.readiness.fee_estimator.estimate(
            "eval", loan_amount=case.setup["amount"],
            months=case.setup["months"], annual_rate=case.setup["rate"],
        )
        failures: list[str] = []
        expect = case.expect
        # 独立复算一遍公式（不信任被测代码的实现，用数学定义校验）
        r = case.setup["rate"] / 12
        n = case.setup["months"]
        p = case.setup["amount"]
        expected = p * r * (1 + r) ** n / ((1 + r) ** n - 1)
        if abs(est.monthly_payment - expected) > expect["tolerance"]:
            failures.append(
                f"月供 {est.monthly_payment} 与公式值 {expected:.2f} 偏差超 {expect['tolerance']}"
            )
        return failures, {
            "monthly_payment": est.monthly_payment,
            "expected": round(expected, 2),
            "total_interest": est.total_interest,
        }

    def _case_fee_tier(self, engine: FlowEngine, case: EvalCase) -> tuple[list[str], dict]:
        est = engine.readiness.fee_estimator.estimate("eval", loan_amount=case.setup["amount"])
        failures: list[str] = []
        if abs(est.service_fee_rate - case.expect["rate"]) > 1e-9:
            failures.append(
                f"大额档费率应为 {case.expect['rate']}，实际 {est.service_fee_rate}"
            )
        return failures, {"rate": est.service_fee_rate, "fee": est.service_fee}

    def _case_hitl_block(self, engine: FlowEngine, case: EvalCase) -> tuple[list[str], dict]:
        lead, _ = self._intake(engine)
        appr = engine.request_high_risk(
            "send_contract", lead_id=lead.lead_id,
            payload={"customer_id": lead.customer_id, "text": "合同"},
            requester="test", reason="评测未审批场景",
        )
        failures: list[str] = []
        try:
            engine.execute_approved(appr.approval_id, actor="test")
            failures.append("未审批竟然执行成功了（HITL 失效）")
        except FlowError as exc:
            if exc.code != case.expect["error_code"]:
                failures.append(f"错误码应为 {case.expect['error_code']}，实际 {exc.code}")
        return failures, {"approval_id": appr.approval_id, "status": appr.status.value}

    def _case_hitl_approve(self, engine: FlowEngine, case: EvalCase) -> tuple[list[str], dict]:
        lead, _ = self._intake(engine)
        appr = engine.request_high_risk(
            "confirm_fee", lead_id=lead.lead_id,
            payload={"customer_id": lead.customer_id, "text": "费用确认"},
            requester="test", reason="评测审批通过场景",
        )
        engine.approvals.decide(appr.approval_id, approver="mgr_01", approved=True, comment="同意")
        result = engine.execute_approved(appr.approval_id, actor="test")
        failures: list[str] = []
        if not result.get("ok"):
            failures.append("审批通过后执行失败")
        return failures, {"result_ok": bool(result.get("ok")), "crm_calls": len(engine.crm.calls) if engine.crm else 0}

    def _case_progress(self, engine: FlowEngine, case: EvalCase) -> tuple[list[str], dict]:
        lead, _ = self._intake(engine)
        failures: list[str] = []
        # ① 错误的后 4 位必须被拒
        try:
            engine.progress_query(lead.lead_id, "0000")
            failures.append("错误的手机号后 4 位竟通过了校验")
        except FlowError as exc:
            if exc.code != "forbidden":
                failures.append(f"错误码应为 forbidden，实际 {exc.code}")
        # ② 正确的查询返回脱敏视图
        snap = engine.progress_query(lead.lead_id, "5678")
        if "*" not in snap["customer_name"]:
            failures.append("姓名未脱敏")
        if "138" in str(snap):
            failures.append("返回里出现了手机号前缀")
        return failures, {"snapshot_keys": sorted(snap.keys())}

    def _case_human_stage(self, engine: FlowEngine, case: EvalCase) -> tuple[list[str], dict]:
        lead, _ = self._intake(engine)
        engine.request_handoff(lead.lead_id, HumanStage.NEGOTIATION, actor="test", note="客户到店面谈")
        failures: list[str] = []
        profile = engine.readiness.profile_from_dict(
            {"customer_id": lead.customer_id, "name": lead.customer_name, "need_amount": 100_000}
        )
        try:
            engine.prepare_arrival(lead.lead_id, profile, actor="test")
            failures.append("谈判节点竟允许 Agent 自动准备（人机边界失效）")
        except FlowError as exc:
            if exc.code != case.expect["error_code"]:
                failures.append(f"错误码应为 {case.expect['error_code']}，实际 {exc.code}")
        return failures, {"human_stage": "negotiation"}

    # ------------------------------------------------------------------ #
    # 全量与准入
    # ------------------------------------------------------------------ #
    def evaluate(self, cases: list[EvalCase]) -> EvalReport:
        results = [self.run_case(c) for c in cases]
        passed = sum(1 for r in results if r.passed)
        avg = sum(r.score for r in results) / len(results) if results else 0.0
        return EvalReport(total=len(results), passed=passed, avg_score=avg, results=results)


def compare(
    baseline: EvalReport,
    current: EvalReport,
    *,
    max_regression: float = 0.03,
) -> GateDecision:
    """准入判定：先取基线、再跑本次（顺序反了基线就是自己，永远通过）。"""
    reasons: list[str] = []
    if current.total == 0:
        reasons.append("本次评测没有用例（疑似配置错误）")
    if baseline.pass_rate and current.pass_rate < baseline.pass_rate - max_regression:
        reasons.append(
            f"通过率退化 {baseline.pass_rate:.1%} → {current.pass_rate:.1%}，"
            f"超过允许幅度 {max_regression:.1%}"
        )
    baseline_failed = {r.case_id for r in baseline.results if not r.passed}
    for r in current.results:
        if not r.passed and r.case_id not in baseline_failed:
            reasons.append(f"新失败用例：{r.case_id}（{r.title}）")
    for r in current.results:
        if not r.passed and r.case_id in baseline_failed:
            reasons.append(f"基线已失败的用例仍未修复：{r.case_id}")
    return GateDecision(
        allowed=not reasons,
        reasons=reasons,
        baseline={"total": baseline.total, "passed": baseline.passed,
                  "pass_rate": round(baseline.pass_rate, 4)},
        current={"total": current.total, "passed": current.passed,
                 "pass_rate": round(current.pass_rate, 4)},
    )
