"""把项目八（助贷业务流程数字员工）装配成平台上的业务 Agent。

八条路由（按业务动作划分，不按技术模块）：
    intake     录入线索 / 分配
    sop        SOP 检查与预警（"有没有漏跟进的"）
    invite     邀约到店
    readiness  到店前准备（一页纸）
    fee        费用预估
    progress   进度查询（客户自助）
    approve    高风险审批
    audit      审计留痕

**作用域裁剪的两条红线**（对应简历"最小权限"）：

1. **成交类工具只在审批路由出现**。`flow.request_contract` /
   `flow.confirm_fee` / `flow.execute_approved` 只在用户明确说
   "发合同 / 确认费用 / 审批" 时才进作用域 —— 其余路由连看都看不到，
   Agent 想越权也拿不到工具。

2. **逃生舱永远可用**。`flow.request_handoff` 带 `escalation` 标签，
   任何路由下都能调用 —— Agent 走到能力边界时至少能转人工，
   而不是硬编一个答案。这与平台 capabilities.py 的
   ALWAYS_AVAILABLE_TAGS 机制配合。
"""

from __future__ import annotations

import re
from typing import Any

from agent_platform.graph.capabilities import AgentCapabilities, NextAction
from agent_platform.logging_config import get_logger
from agent_platform.state.schema import AgentState
from agent_platform.tools.registry import ToolRegistry
from agent_platform.tools.spec import ToolSpec
from flow_ops.service import FlowOpsService

logger = get_logger(__name__)

AGENT_NAME = "flow_ops"

# 线索编号（ld_xxxxxxxxxxxx）与客户编号（C1002）解析
_LEAD_PATTERN = re.compile(r"\bld_[0-9a-zA-Z]{6,}\b")
_CUSTOMER_PATTERN = re.compile(r"\bC\d{3,6}\b")
_APPROVAL_PATTERN = re.compile(r"\bap_[0-9a-zA-Z]{6,}\b")

# ---- 意图关键词（顺序即优先级：越专的排越前）----
_AUDIT_KEYS = ("审计", "留痕", "谁做的", "操作记录", "追溯", "日志")
_APPROVE_KEYS = ("审批", "批准", "同意", "驳回", "拒绝", "pending", "待审")
_CONTRACT_KEYS = ("发合同", "发送合同", "合同", "签约文件")
_FEE_KEYS = ("确认费用", "费用确认", "最终费用", "报价确认")
_PROGRESS_KEYS = ("进度", "到哪一步", "到哪了", "查一下我的", "自助", "客户问")
_METRICS_KEYS = ("指标", "漏跟进", "统计", "看板", "多少条线索")
_READINESS_KEYS = ("到店前", "准备", "一页纸", "档案汇总", "方案初稿", "预生成")
_INVITE_KEYS = ("邀约", "预约", "约到店", "到店时间", "约一下")
_SOP_KEYS = ("sop", "漏跟进", "超时", "未跟进", "预警", "有没有漏", "扫描")
_INTAKE_KEYS = ("录入", "新线索", "分配", "留资", "新客户", "登记")

# 门店识别（口语 → 门店名，种子数据同款）
#: 审批路由涉及的全部工具（用于"已做过就收手"的判定）
_APPROVE_TOOLS: frozenset[str] = frozenset(
    {
        "flow.request_contract",
        "flow.confirm_fee",
        "flow.approve",
        "flow.execute_approved",
        "flow.request_handoff",
    }
)

_STORE_KEYS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("南京", "总店", "旗舰"), "南京旗舰店"),
    (("杭州",), "杭州分店"),
    (("上海",), "上海分店"),
    (("苏州",), "苏州分店"),
)


def _find_store(text: str) -> str:
    for keys, store in _STORE_KEYS:
        if any(k in text for k in keys):
            return store
    return "南京旗舰店"


def tool_tags_for(state: AgentState) -> list[str]:
    """按路由返回工具标签（作用域裁剪的召回依据）。"""
    route = state.get("route", "intake")
    # **只返回细标签，绝不返回 "flow" 这个公共标签**：
    # 平台的 select() 是"任一标签命中即召回"（OR 语义），
    # 而 14 个工具全都带 "flow" —— 返回它等于放开全量，
    # 高风险工具会在普通路由里可见，最小权限就失效了。
    mapping: dict[str, list[str]] = {
        "intake": ["lead"],
        "sop": ["sop"],
        "invite": ["invite"],
        "readiness": ["readiness"],
        "fee": ["fee"],
        "progress": ["progress"],
        "approve": ["hitl"],
        "audit": ["audit", "metrics"],
        "metrics": ["metrics"],
    }
    return mapping.get(route, ["lead"])


def route(state: AgentState) -> str:
    """意图路由：读用户输入，返回路由名。

    路由顺序与关键词表一致：审批/合同/费用确认优先于一切
    （这些是高风险路由，误判到别处最多是多问一句，误判成
    低风险路由就是合规风险）；SOP 先于指标 —— "漏跟进"
    是流程语义（该去扫 SOP），不是看板语义。
    """
    text = state.get("user_input", "") or ""
    if any(k in text for k in _AUDIT_KEYS):
        return "audit"
    if any(k in text for k in _APPROVE_KEYS) or any(k in text for k in _CONTRACT_KEYS) \
            or any(k in text for k in _FEE_KEYS):
        return "approve"
    if any(k in text for k in _PROGRESS_KEYS):
        return "progress"
    if any(k in text for k in _SOP_KEYS):
        return "sop"
    if any(k in text for k in _METRICS_KEYS):
        return "metrics"
    if any(k in text for k in _READINESS_KEYS):
        return "readiness"
    if any(k in text for k in _INVITE_KEYS):
        return "invite"
    return "intake"


def decide(state: AgentState, scoped: list[ToolSpec]) -> NextAction:
    """执行决策：按路由选工具。

    **状态纯函数**（同样输入 → 同样输出）：平台在 HITL 恢复时会
    重放本节点，决策不确定会导致审批与执行不一致。

    **收手条件**：目标工具已出现在 observations 里就返回 None ——
    不检查就会在平台执行循环里反复调同一个工具，直到触发
    "最大步数熔断"（那是异常路径，不是正常完成）。
    """
    route_name = state.get("route", "intake")
    text = state.get("user_input", "") or ""
    called = {
        o.get("tool") for o in (state.get("observations", []) or []) if o.get("tool")
    }

    def _done(thought: str = "本路由动作已完成") -> NextAction:
        return NextAction(tool=None, thought=thought)

    lead_id = _first_match(_LEAD_PATTERN, text)
    customer_id = _first_match(_CUSTOMER_PATTERN, text)
    approval_id = _first_match(_APPROVAL_PATTERN, text)

    if route_name == "sop":
        if "flow.check_sop" in called:
            return _done("SOP 已扫描")
        return NextAction(tool="flow.check_sop", arguments={}, thought="扫描 SOP 逾期")

    if route_name == "metrics":
        if "flow.metrics" in called:
            return _done("指标已取")
        return NextAction(tool="flow.metrics", arguments={}, thought="看流程指标")

    if route_name == "audit":
        if "flow.audit_trail" in called:
            return _done("审计已查")
        return NextAction(
            tool="flow.audit_trail",
            arguments={"lead_id": lead_id},
            thought="查审计流水" if lead_id else "查全量审计流水",
        )

    if route_name == "progress":
        if "flow.query_progress" in called:
            return _done("进度已查")
        if lead_id:
            m = re.search(r"后\s*4\s*位[是为]?\s*(\d{4})", text)
            last4 = m.group(1) if m else ""
            return NextAction(
                tool="flow.query_progress",
                arguments={"lead_id": lead_id, "phone_last4": last4},
                thought="客户自助查进度",
            )
        return _done("没有线索编号，无法查进度")

    if route_name == "readiness":
        if "flow.prepare_arrival" in called:
            return _done("准备包已生成")
        if lead_id:
            return NextAction(tool="flow.prepare_arrival",
                              arguments={"lead_id": lead_id}, thought="生成到店前一页纸")
        return _done("缺线索编号")

    if route_name == "invite":
        if "flow.invite" in called:
            return _done("邀约已创建")
        if lead_id:
            return NextAction(
                tool="flow.invite",
                arguments={"lead_id": lead_id, "store": _find_store(text),
                           "scheduled_at": 0.0},
                thought="创建邀约（时间由调用方补）",
            )
        return _done("缺线索编号")

    if route_name == "fee":
        if "flow.estimate_fee" in called:
            return _done("费用已预估")
        if lead_id:
            amount = _first_amount(text)
            if amount <= 0:
                return _done("未识别到意向额度")
            return NextAction(
                tool="flow.estimate_fee",
                arguments={"lead_id": lead_id, "loan_amount": amount},
                thought="费用预估（确定性公式）",
            )
        return _done("缺线索编号")

    if route_name == "approve":
        # 审批路由：一次只做一个动作。任何审批类工具已执行过就收手 ——
        # 漏掉这条会在平台执行循环里反复申请同一个高风险动作
        # （恢复重放时表现为"批了却又冒出新的待办"）。
        if called & _APPROVE_TOOLS:
            return _done("审批路由动作已完成")
        if approval_id and "执行" in text:
            return NextAction(tool="flow.execute_approved",
                              arguments={"approval_id": approval_id}, thought="执行已审批动作")
        if approval_id:
            approved = not any(k in text for k in ("拒绝", "驳回", "不同意"))
            return NextAction(
                tool="flow.approve",
                arguments={"approval_id": approval_id, "approver": "mgr_01",
                           "approved": approved},
                thought="人工审批决策",
            )
        if lead_id and any(k in text for k in _CONTRACT_KEYS):
            return NextAction(tool="flow.request_contract",
                              arguments={"lead_id": lead_id, "reason": "客户要求发送合同"},
                              thought="申请发合同（走 HITL）")
        if lead_id and any(k in text for k in _FEE_KEYS):
            return NextAction(tool="flow.confirm_fee",
                              arguments={"lead_id": lead_id, "reason": "客户确认费用"},
                              thought="申请确认费用（走 HITL）")
        if lead_id:
            return NextAction(tool="flow.request_handoff",
                              arguments={"lead_id": lead_id, "human_stage": "reception",
                                         "note": "Agent 边界外，转人工"},
                              thought="边界外动作转人工")
        return _done("缺编号")

    # intake：有客户编号就录入
    if customer_id:
        if "flow.intake_lead" in called:
            return _done("线索已录入")
        return NextAction(
            tool="flow.intake_lead",
            arguments={
                "customer_id": customer_id,
                "customer_name": "待补充",
                "phone_masked": "待补充",
                "source": "douyin",
                "owner": "rep_01",
            },
            thought="录入线索（姓名/手机号需人工补全，Agent 不编造）",
        )
    return _done("未识别到客户编号")


def plan(state: AgentState, scoped: list[ToolSpec]) -> list[str]:
    """规划器：把任务拆成步骤（给观测与回放看）。"""
    route_name = state.get("route", "intake")
    steps: dict[str, list[str]] = {
        "intake": ["解析客户编号", "录入线索并分配", "写审计"],
        "sop": ["扫描活跃线索", "计算 SOP 逾期", "生成主管预警"],
        "invite": ["定位线索", "创建邀约（幂等）", "推进状态机"],
        "readiness": ["汇总客户档案", "调产品助手生成方案初稿", "费用预估"],
        "fee": ["读取意向额度", "等额本息试算", "分档服务费"],
        "progress": ["身份校验", "返回脱敏进度"],
        "approve": ["识别审批意图", "走 HITL 门", "留痕"],
        "audit": ["按线索检索审计", "输出时间线"],
        "metrics": ["聚合流程指标"],
    }
    return steps.get(route_name, ["解析意图"])


def respond(state: AgentState) -> dict[str, Any]:
    """应答器：把最后一步工具结果整理成结构化输出。"""
    observations = state.get("observations", []) or []
    approvals = state.get("approvals") or []
    route_name = state.get("route", "intake")

    def data_of(tool: str) -> dict[str, Any]:
        for o in reversed(observations):
            if o.get("tool") == tool:
                return o.get("data") or {}
        return {}

    # 取本路由实际调用过的工具结果（按路由映射，避免取到别路由的）
    route_tool = {
        "intake": "flow.intake_lead",
        "sop": "flow.check_sop",
        "invite": "flow.invite",
        "readiness": "flow.prepare_arrival",
        "fee": "flow.estimate_fee",
        "progress": "flow.query_progress",
        "approve": "flow.request_contract",
        "audit": "flow.audit_trail",
        "metrics": "flow.metrics",
    }.get(route_name, "")
    output = data_of(route_tool)
    used_tool = next(
        (o["tool"] for o in reversed(observations) if o.get("tool", "").startswith("flow.")),
        "",
    )
    return {
        "route": route_name,
        "agent": AGENT_NAME,
        "tool": used_tool,
        "output": output,
        "approvals": approvals,
        "note": "流程类动作以工具返回为准；高风险动作必须人工审批后执行",
    }


def _first_match(pattern: re.Pattern[str], text: str) -> str:
    m = pattern.search(text)
    return m.group(0) if m else ""


def _first_amount(text: str) -> float:
    """从口语里取额度：'30万' → 300000，'5000' → 5000。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(万|w|W)?", text)
    if not m:
        return 0.0
    value = float(m.group(1))
    if m.group(2):
        value *= 10_000
    return value


def build_flow_ops_agent(service: FlowOpsService, tools: ToolRegistry) -> AgentCapabilities:
    """装配 flow_ops Agent。"""
    return AgentCapabilities(
        name=AGENT_NAME,
        description="助贷业务流程数字员工：线索 SOP 自动化、到店前准备、费用预估、HITL 审批与全程留痕",
        tools=tools,
        route=route,
        tool_tags_for=tool_tags_for,
        plan=plan,
        decide=decide,
        respond=respond,
        version="1.0.0",
        tags=("flow", "automation", "hitl"),
    )
