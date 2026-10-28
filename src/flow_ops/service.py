"""项目八（助贷业务流程数字员工）服务门面。

对外暴露的六组能力（对应简历 Task 的四个可验收任务 + 查询）：

    流程自动化  intake_lead / advance / sweep_sop / acknowledge_alert
    到店前准备  prepare_arrival（档案 + 方案初稿 + 费用预估一页纸）
    费用评估    estimate_fee（确定性试算，预估 ≠ 报价）
    人机边界    request_handoff / request_high_risk / decide / execute_approved
    自助查询    progress_query（身份校验 + 脱敏）
    留痕观测    audit_trail / alerts / pending_approvals / metrics

**与项目五的关系**：本服务把自己注册成一个 Agent（`flow_ops`），
编排、中断恢复、HITL 检查点全部复用平台能力，不重写。
高风险工具同时享受**双重保护**：平台层的高风险工具自动挂起 +
    本服务的 ApprovalGate（定时任务路径也走得到）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_platform.config import Settings, get_settings
from agent_platform.llm.base import LLMProvider
from agent_platform.logging_config import get_logger
from agent_platform.tools.registry import ToolRegistry
from agent_platform.tools.spec import ToolSpec
from flow_ops.crm import RecordingCrmClient
from flow_ops.domain import HumanStage, LeadStage, ProfileSummary
from flow_ops.evaluation import FlowEvaluator, builtin_cases
from flow_ops.metrics import FlowMetrics
from flow_ops.notifier import (
    Notifier,
    approval_requested_fields,
    approvals_expired_fields,
    build_notifier,
    sop_alerts_fields,
)
from flow_ops.store import FlowStore
from flow_ops.workflow import FlowEngine

logger = get_logger(__name__)


class FlowOpsService:
    """助贷业务流程数字员工服务。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        llm: LLMProvider | None = None,
        plan_provider: Callable[[str], dict[str, Any]] | None = None,
        profile_provider: Callable[[str], ProfileSummary | None] | None = None,
        crm=None,
        notifier: Notifier | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        # llm 参数保留签名兼容：本项目的业务判据全部是确定性代码，
        # 模型只用于"写提醒话术"这类语言层工作（当前版本刻意未接入，
        # 避免把"要不要提醒"这种判错也能被模型影响）。
        self.llm = llm
        self._plan_provider = plan_provider
        self._profile_provider = profile_provider

        # 通知渠道：不显式传入就按配置自建（配了 webhook 地址才是 Webhook，
        # 否则 LogNotifier 只写日志）。测试可注入假通知器断言"发了什么"。
        self.notifier = notifier if notifier is not None else build_notifier(self.settings)

        self.store = FlowStore(self.settings.flow_sqlite_abs_path)
        self.crm = crm if crm is not None else RecordingCrmClient()
        self.engine = FlowEngine(
            self.settings, self.store, crm=self.crm, plan_provider=plan_provider
        )
        self.metrics = FlowMetrics(self.store)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def bootstrap(self) -> dict[str, Any]:
        """启动初始化：清理过期审批（不自动批准，只标记）。"""
        expired = self.engine.approvals.expire_overdue()
        if expired:
            logger.info("启动时清理过期审批：%s", expired)
            self._safe_notify(
                "approvals_expired",
                title=f"审批超时清理 ×{len(expired)}",
                fields=approvals_expired_fields(expired),
            )
        snap = self.metrics.snapshot()
        logger.info(
            "流程数字员工就绪：线索 %d 条（活跃 %d），未处理预警 %d，待审批 %d",
            snap["leads"]["total"], snap["leads"]["active"],
            snap["sop_alerts_open"], snap["hitl"]["pending"],
        )
        return snap

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> FlowOpsService:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _safe_notify(self, event: str, *, title: str, fields: dict[str, Any]) -> None:
        """发通知；任何失败只记日志。

        通知是"找人"的手段，不是流程的一部分 —— webhook 超时、IM 机器人
        限流、网络抖动，都不该让 SOP 扫描半路死掉。业务结果照常返回，
        运维从日志里能看到哪条通知没送到。
        """
        try:
            self.notifier.notify(event, title=title, fields=fields)
        except Exception:  # noqa: BLE001
            logger.exception("通知发送失败（不影响业务流程）：%s", event)

    # ------------------------------------------------------------------ #
    # 档案解析（项目一 / 项目四 / 种子的统一入口）
    # ------------------------------------------------------------------ #
    def profile_of(self, customer_id: str) -> ProfileSummary:
        """取客户档案摘要。provider 缺失时返回空档案（带 warning，不编造）。"""
        if self._profile_provider is None:
            return ProfileSummary(customer_id=customer_id, name="")
        profile = self._profile_provider(customer_id)
        if profile is None:
            return ProfileSummary(customer_id=customer_id, name="")
        return profile

    # ================================================================== #
    # 一、流程自动化
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
    ) -> dict[str, Any]:
        lead, hit = self.engine.intake_lead(
            customer_id=customer_id, customer_name=customer_name,
            phone_masked=phone_masked, source=source, owner=owner,
            actor=actor, note=note, idem_key=idem_key,
        )
        return {**lead.to_dict(), "idempotent_hit": hit}

    def advance(self, lead_id: str, to_stage: str, *, actor: str = "system", reason: str = "") -> dict:
        lead = self.engine.advance(lead_id, LeadStage(to_stage), actor=actor, reason=reason)
        return lead.to_dict()

    def check_sop(self, *, actor: str = "system") -> dict[str, Any]:
        """SOP 扫描（定时任务入口，也可手动触发）。"""
        alerts = self.engine.sweep_sop(actor=actor)
        if alerts:
            # 一轮一个 digest 而不是逐条发：预警量大时逐条会撞 IM 机器人
            # 频率限制，而且对主管是同一件事的 N 条轰炸（见 notifier 文件头）
            self._safe_notify(
                "sop_alerts",
                title=f"SOP 逾期预警 ×{len(alerts)}",
                fields=sop_alerts_fields([a.to_dict() for a in alerts]),
            )
        return {
            "alerts": [a.to_dict() for a in alerts],
            "count": len(alerts),
            "open_total": len(self.engine.alerts(limit=500)),
        }

    def acknowledge_alert(self, alert_id: str, *, by: str) -> dict:
        return {"acknowledged": self.engine.acknowledge_alert(alert_id, by=by)}

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
    ) -> dict[str, Any]:
        appt, hit = self.engine.invite(
            lead_id, store=store, scheduled_at=scheduled_at,
            actor=actor, note=note, idem_key=idem_key,
        )
        return {**appt.to_dict(), "idempotent_hit": hit}

    def confirm_arrival(self, lead_id: str, *, actor: str = "system", idem_key: str = "") -> dict:
        lead, hit = self.engine.confirm_arrival(lead_id, actor=actor, idem_key=idem_key)
        return {**lead.to_dict(), "idempotent_hit": hit}

    # ================================================================== #
    # 三、到店前准备与费用预估
    # ================================================================== #
    def prepare_arrival(self, lead_id: str, *, actor: str = "agent") -> dict[str, Any]:
        lead = self.engine.get_lead(lead_id)
        profile = self.profile_of(lead.customer_id)
        pack = self.engine.prepare_arrival(lead_id, profile, actor=actor)
        self.metrics.record_prepare(pack.prepared_in_ms)
        return pack.to_dict()

    def estimate_fee(
        self,
        lead_id: str,
        *,
        loan_amount: float,
        months: int | None = None,
        annual_rate: float | None = None,
        actor: str = "agent",
    ) -> dict[str, Any]:
        est = self.engine.estimate_fee(
            lead_id, loan_amount=loan_amount, months=months,
            annual_rate=annual_rate, actor=actor,
        )
        return est.to_dict()

    # ================================================================== #
    # 四、人机边界
    # ================================================================== #
    def request_handoff(
        self, lead_id: str, human_stage: str, *, actor: str = "agent", note: str = ""
    ) -> dict:
        lead = self.engine.request_handoff(lead_id, HumanStage(human_stage), actor=actor, note=note)
        return lead.to_dict()

    def request_high_risk(
        self,
        action: str,
        *,
        lead_id: str,
        payload: dict | None = None,
        requester: str = "agent",
        reason: str = "",
    ) -> dict:
        appr = self.engine.request_high_risk(
            action, lead_id=lead_id, payload=payload, requester=requester, reason=reason
        )
        # 高风险单建出来第一秒就该通知到人 —— 审批时效直接决定
        # "发合同"这类动作能不能当天完成（超时就只能重来）
        self._safe_notify(
            "approval_requested",
            title=f"高风险动作待审批：{appr.action}",
            fields=approval_requested_fields(appr.to_dict()),
        )
        return appr.to_dict()

    def decide_approval(
        self, approval_id: str, *, approver: str, approved: bool, comment: str = ""
    ) -> dict:
        appr = self.engine.approvals.decide(
            approval_id, approver=approver, approved=approved, comment=comment
        )
        return appr.to_dict()

    def execute_approved(self, approval_id: str, *, actor: str = "agent") -> dict:
        result = self.engine.execute_approved(approval_id, actor=actor)
        return {"ok": bool(result.get("ok")), "result": result}

    def pending_approvals(self, limit: int = 50) -> list[dict]:
        return self.engine.pending_approvals(limit=limit)

    # ================================================================== #
    # 五、查询与观测
    # ================================================================== #
    def progress_query(self, lead_id: str, phone_last4: str) -> dict:
        return self.engine.progress_query(lead_id, phone_last4)

    def leads(self, *, stage: str = "", owner: str = "", limit: int = 100) -> list[dict]:
        return self.engine.list_leads(stage=stage, owner=owner, limit=limit)

    def lead_detail(self, lead_id: str) -> dict | None:
        lead = self.engine.store.get_lead(lead_id)
        if lead is None:
            return None
        return {
            **lead.to_dict(),
            "transitions": [t.to_dict() for t in self.engine.store.list_transitions(lead_id)],
            "appointments": [a.to_dict() for a in self.engine.store.list_appointments(lead_id)],
        }

    def audit_trail(self, lead_id: str = "", action: str = "", limit: int = 100) -> list[dict]:
        return self.engine.audit.trail(
            target_type="lead" if lead_id else "",
            target_id=lead_id, action=action, limit=limit,
        )

    def alerts(self, *, include_acknowledged: bool = False, limit: int = 100) -> list[dict]:
        return self.engine.alerts(include_acknowledged=include_acknowledged, limit=limit)

    def metrics_snapshot(self) -> dict[str, Any]:
        return self.metrics.snapshot()

    # ================================================================== #
    # 六、评测
    # ================================================================== #
    def evaluate(self) -> dict[str, Any]:
        """跑内置 Golden Set。每个用例用独立库，互不污染。"""
        import tempfile

        def _factory() -> FlowEngine:
            tmp = Path(tempfile.mkdtemp(prefix="flow_eval_")) / "eval.sqlite"
            store = FlowStore(tmp)
            return FlowEngine(self.settings, store, crm=RecordingCrmClient(),
                              plan_provider=self._plan_provider)

        evaluator = FlowEvaluator(_factory)
        cases = builtin_cases(self.settings)
        report = evaluator.evaluate(cases)
        return report.to_dict()

    def gate(self) -> dict[str, Any]:
        """准入：先取基线，再跑本次（顺序反了基线就是自己，永远通过）。"""
        baseline_path = self.settings.flow_abs_dir / "eval_baseline.json"
        current = self.evaluate()
        baseline = self._load_baseline(baseline_path)
        if baseline is None:
            # 首跑没有基线：记录当前为基线并放行（并明确告知这是首跑）
            self._save_baseline(baseline_path, current)
            return {
                "allowed": True,
                "reasons": ["首次运行，已把当前结果记录为基线"],
                "baseline": None,
                "current": current,
            }
        from flow_ops.domain import EvalReport

        base_report = EvalReport(
            total=baseline["total"], passed=baseline["passed"],
            avg_score=baseline.get("avg_score", 0.0),
        )
        from flow_ops.evaluation import compare as _compare

        decision = _compare(
            base_report,
            _report_from(current),
            max_regression=self.settings.flow_eval_max_regression,
        )
        result = decision.to_dict()
        result["current"] = current
        result["baseline"] = baseline
        if decision.allowed:
            self._save_baseline(baseline_path, current)
        return result

    # ------------------------------------------------------------------ #
    # 工具注册（挂到平台五）
    # ------------------------------------------------------------------ #
    def register_tools(self, registry: ToolRegistry) -> list[str]:
        """把流程能力注册成平台工具。高风险动作标 high → 平台自动 HITL。"""
        registered: list[str] = []

        def _reg(spec: ToolSpec) -> None:
            registry.register(spec)
            registered.append(spec.name)

        _reg(ToolSpec(
            name="flow.intake_lead",
            description="录入新线索并自动分配业务员（幂等：同客户同渠道重复提交只录一条）",
            parameters={
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string", "description": "客户编号，如 C1002"},
                    "customer_name": {"type": "string"},
                    "phone_masked": {"type": "string", "description": "脱敏手机号，如 138****5678"},
                    "source": {"type": "string", "description": "留资渠道：douyin/infoflow/referral"},
                    "owner": {"type": "string", "description": "负责业务员 ID"},
                    "note": {"type": "string"},
                },
                "required": ["customer_id", "customer_name", "phone_masked", "source", "owner"],
            },
            handler=self.intake_lead,
            tags=("flow", "lead", "write"),
            risk_level="low",
            idempotent=True,
        ))
        _reg(ToolSpec(
            name="flow.check_sop",
            description="SOP 扫描：找出超时未跟进/未加微/未确认到店的线索并预警主管",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=self.check_sop,
            tags=("flow", "sop"),
            risk_level="low",
        ))
        _reg(ToolSpec(
            name="flow.invite",
            description="创建到店邀约（幂等：同客户同时段重复邀约只保留一条）",
            parameters={
                "type": "object",
                "properties": {
                    "lead_id": {"type": "string"},
                    "store": {"type": "string", "description": "门店，如 南京旗舰店"},
                    "scheduled_at": {"type": "number", "description": "预约时间（Unix 秒）"},
                    "note": {"type": "string"},
                },
                "required": ["lead_id", "store", "scheduled_at"],
            },
            handler=self.invite,
            tags=("flow", "invite", "write"),
            risk_level="medium",
            idempotent=True,
        ))
        _reg(ToolSpec(
            name="flow.prepare_arrival",
            description="到店前自动准备：汇总客户档案 + 预生成方案初稿（复用产品助手）+ 费用预估",
            parameters={
                "type": "object",
                "properties": {"lead_id": {"type": "string"}},
                "required": ["lead_id"],
            },
            handler=self.prepare_arrival,
            tags=("flow", "readiness"),
            risk_level="low",
        ))
        _reg(ToolSpec(
            name="flow.estimate_fee",
            description="费用预估：服务费（分档费率）+ 放款成本等额本息试算。预估≠报价",
            parameters={
                "type": "object",
                "properties": {
                    "lead_id": {"type": "string"},
                    "loan_amount": {"type": "number", "description": "意向放款金额（元）"},
                    "months": {"type": "integer"},
                    "annual_rate": {"type": "number"},
                },
                "required": ["lead_id", "loan_amount"],
            },
            handler=self.estimate_fee,
            tags=("flow", "fee"),
            risk_level="low",
        ))
        _reg(ToolSpec(
            name="flow.query_progress",
            description="客户自助查进件进度（需手机号后 4 位校验，返回脱敏视图）",
            parameters={
                "type": "object",
                "properties": {
                    "lead_id": {"type": "string"},
                    "phone_last4": {"type": "string", "description": "手机号后 4 位"},
                },
                "required": ["lead_id", "phone_last4"],
            },
            handler=self.progress_query,
            tags=("flow", "progress"),
            risk_level="low",
        ))
        _reg(ToolSpec(
            name="flow.request_handoff",
            description="把线索转人工节点（接待/谈判/费用确认）。Agent 只打标记，不代替人决策",
            parameters={
                "type": "object",
                "properties": {
                    "lead_id": {"type": "string"},
                    "human_stage": {"type": "string",
                                    "description": "reception/negotiation/fee_confirmation"},
                    "note": {"type": "string"},
                },
                "required": ["lead_id", "human_stage"],
            },
            handler=self.request_handoff,
            tags=("flow", "hitl", "escalation"),
            risk_level="medium",
        ))
        _reg(ToolSpec(
            name="flow.request_contract",
            description="申请发送合同给客户（高风险：必须人工审批后才执行）",
            parameters={
                "type": "object",
                "properties": {
                    "lead_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["lead_id", "reason"],
            },
            handler=lambda **kw: self.request_high_risk(
                "send_contract", lead_id=kw["lead_id"], reason=kw.get("reason", ""),
                payload={"lead_id": kw["lead_id"]},
            ),
            tags=("flow", "hitl", "contract"),
            risk_level="high",
            idempotent=False,
        ))
        _reg(ToolSpec(
            name="flow.confirm_fee",
            description="申请向客户确认最终费用（高风险：必须人工审批后才执行）",
            parameters={
                "type": "object",
                "properties": {
                    "lead_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["lead_id", "reason"],
            },
            handler=lambda **kw: self.request_high_risk(
                "confirm_fee", lead_id=kw["lead_id"], reason=kw.get("reason", ""),
                payload={"lead_id": kw["lead_id"]},
            ),
            # 刻意不带 "fee" 标签：否则普通"费用预估"路由会把这个
            # 高风险工具一起召回出来（标签是 OR 语义）
            tags=("flow", "hitl", "fee_confirm"),
            risk_level="high",
            idempotent=False,
        ))
        _reg(ToolSpec(
            name="flow.approve",
            description="审批高风险动作（approver 必填；拒绝同样留痕）",
            parameters={
                "type": "object",
                "properties": {
                    "approval_id": {"type": "string"},
                    "approver": {"type": "string"},
                    "approved": {"type": "boolean"},
                    "comment": {"type": "string"},
                },
                "required": ["approval_id", "approver", "approved"],
            },
            handler=self.decide_approval,
            tags=("flow", "hitl"),
            risk_level="medium",
        ))
        _reg(ToolSpec(
            name="flow.execute_approved",
            description="执行已审批通过的高风险动作（未审批一律拒绝）",
            parameters={
                "type": "object",
                "properties": {"approval_id": {"type": "string"}},
                "required": ["approval_id"],
            },
            handler=self.execute_approved,
            tags=("flow", "hitl"),
            risk_level="high",
            idempotent=False,
        ))
        _reg(ToolSpec(
            name="flow.audit_trail",
            description="查询线索的全程审计留痕（谁、何时、做了什么、关联哪次审批）",
            parameters={
                "type": "object",
                "properties": {"lead_id": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["lead_id"],
            },
            handler=self.audit_trail,
            tags=("flow", "audit"),
            risk_level="low",
        ))
        _reg(ToolSpec(
            name="flow.metrics",
            description="流程指标：漏跟进率、阶段分布、幂等拦截数、HITL 待审",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=self.metrics_snapshot,
            tags=("flow", "metrics"),
            risk_level="low",
        ))
        _reg(ToolSpec(
            name="flow.eval",
            description="跑流程行为 Golden Set（SOP/幂等/费用口径/HITL/脱敏）",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=self.evaluate,
            tags=("flow", "eval"),
            risk_level="low",
        ))
        return registered

    # ------------------------------------------------------------------ #
    # 统计
    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.flow_enabled,
            # 锁后端名（SqliteLockBackend / RedisLockBackend）：
            # 多 worker 部署的验收项之一就是"这里必须是 Redis"
            "lock_backend": self.engine.locks.backend_name,
            "store": self.store.stats(),
            "metrics": self.metrics.snapshot(),
            "high_risk_actions": self.engine.approvals.high_risk_actions(),
        }

    # ------------------------------------------------------------------ #
    # 基线文件
    # ------------------------------------------------------------------ #
    @staticmethod
    def _load_baseline(path: Path) -> dict | None:
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _save_baseline(path: Path, report: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"total": report["total"], "passed": report["passed"],
                 "pass_rate": report.get("pass_rate", 0.0),
                 "avg_score": report.get("avg_score", 0.0)},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )


def _report_from(data: dict):
    from flow_ops.domain import EvalCaseResult, EvalReport

    return EvalReport(
        total=data["total"], passed=data["passed"], avg_score=data.get("avg_score", 0.0),
        results=[
            EvalCaseResult(case_id=r["case_id"], title=r["title"], passed=r["passed"],
                           score=r["score"], failures=r.get("failures", []))
            for r in data.get("results", [])
        ],
    )
