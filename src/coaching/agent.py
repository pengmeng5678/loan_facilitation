"""把项目三装配成平台上的业务 Agent。

三条路由：
    practice  陪练（开启会话 / 对话推进 / 结束评分）
    qc        全量质检；命中 high 级问题时**告警主管**（走平台 HITL 审批）
    mining    高转化话术挖掘

注意 qc 路由的告警动作：platform.send_external_message 是 high 风险 + 非幂等工具，
平台会自动插入人工审批检查点 —— 这正是"AI 提供证据，人做决策"的落地：
AI 可以判定违规，但**对外发告警这件事必须有人点头**。
"""

from __future__ import annotations

import re
from typing import Any

from agent_platform.graph.capabilities import AgentCapabilities, NextAction
from agent_platform.logging_config import get_logger
from agent_platform.state.schema import AgentState
from agent_platform.tools.registry import ToolRegistry
from agent_platform.tools.spec import ToolSpec
from coaching.service import CoachingService

logger = get_logger(__name__)

AGENT_NAME = "coaching"

_PERSONA_PATTERN = re.compile(r"\bP-[A-Z]+\b")
_SESSION_PATTERN = re.compile(r"\bcs_[0-9a-f]+\b")

_QC_KEYS = ("质检", "合规检查", "检查对话", "会话检查", "抽查", "违规", "风险检查")
_MINING_KEYS = ("挖掘", "话术沉淀", "提炼话术", "高转化", "沉淀话术")
_FINISH_KEYS = ("结束", "停止", "评分", "打分", "复盘")


def _ctx_of(state: AgentState) -> dict[str, Any]:
    return state.get("context") or {}


def build_coaching_agent(service: CoachingService, tools: ToolRegistry) -> AgentCapabilities:
    """构造陪练考核 Agent 的能力声明。"""

    # ------------------------------------------------------------------ #
    # ① 意图路由
    # ------------------------------------------------------------------ #
    def route(state: AgentState) -> str:
        text = state.get("user_input", "")
        if any(k in text for k in _QC_KEYS):
            return "qc"
        if any(k in text for k in _MINING_KEYS):
            return "mining"
        return "practice"

    # ------------------------------------------------------------------ #
    # ② 作用域
    # ------------------------------------------------------------------ #
    def tool_tags_for(state: AgentState) -> list[str]:
        mapping = {
            "practice": ["practice"],
            # qc 路由额外允许 message（告警通道），其余路由发不出对外消息
            "qc": ["qc", "message"],
            "mining": ["mining"],
        }
        return mapping.get(state.get("route", "practice"), ["practice"])

    # ------------------------------------------------------------------ #
    # ③ 规划
    # ------------------------------------------------------------------ #
    def plan(state: AgentState, scoped: list[ToolSpec]) -> list[str]:
        route_name = state.get("route", "practice")
        if route_name == "qc":
            return [
                "加载会话存档语料",
                "规则引擎粗筛（违规承诺/敏感信息/SOP/时效）",
                "命中高风险则告警主管（需人工审批）",
                "输出带证据的质检报告",
            ]
        if route_name == "mining":
            return ["扫描已成交对话", "识别高价值话术阶段", "按客户正向反应打分", "回流知识库与题库"]
        return ["开启陪练会话（生成虚拟客户）", "多轮对抗对话", "结束并生成四维评分卡"]

    # ------------------------------------------------------------------ #
    # ④ 执行决策（确定性）
    # ------------------------------------------------------------------ #
    def decide(state: AgentState, scoped: list[ToolSpec]) -> NextAction:
        route_name = state.get("route", "practice")
        text = state.get("user_input", "")
        ctx = _ctx_of(state)
        observations = state.get("observations", [])

        def obs_of(tool: str) -> list[dict[str, Any]]:
            return [o for o in observations if o.get("tool") == tool]

        # ---------------- 质检 ----------------
        if route_name == "qc":
            calls = obs_of("coaching.qc")
            if not calls:
                args: dict[str, Any] = {}
                conv_id = str(ctx.get("conversation_id") or "").strip()
                if conv_id:
                    args["conversation_id"] = conv_id
                return NextAction(tool="coaching.qc", arguments=args, thought="执行全量质检")

            report = calls[-1].get("data") or {}
            # 命中高风险 → 告警主管（high 风险工具，平台会自动挂起等审批）
            if report.get("needs_alert") and not obs_of("platform.send_external_message"):
                target = str(ctx.get("supervisor") or "supervisor_duty")
                findings = report.get("findings") or []
                top = findings[0] if findings else {}
                return NextAction(
                    tool="platform.send_external_message",
                    arguments={
                        "target": target,
                        "content": (
                            f"【合规告警】对话 {report.get('conversation_id')} 检出高风险问题："
                            f"{top.get('detail', '')}。原文：{top.get('excerpt', '')[:60]}"
                        ),
                    },
                    thought="存在 high 级问题，告警主管",
                )
            return NextAction(tool=None, thought="质检完成，输出报告")

        # ---------------- 话术挖掘 ----------------
        if route_name == "mining":
            if not obs_of("coaching.mine_phrases"):
                return NextAction(tool="coaching.mine_phrases", arguments={}, thought="扫描成交对话挖掘话术")
            return NextAction(tool=None)

        # ---------------- 陪练 ----------------
        session_id = str(ctx.get("session_id") or "").strip()
        if not session_id:
            found = _SESSION_PATTERN.search(text)
            session_id = found.group(0) if found else ""

        # 结束并评分
        if any(k in text for k in _FINISH_KEYS) and session_id:
            if not obs_of("coaching.finish_session"):
                return NextAction(
                    tool="coaching.finish_session",
                    arguments={"session_id": session_id},
                    thought="结束陪练并评分",
                )
            return NextAction(tool=None)

        # 推进对话
        if session_id:
            if not obs_of("coaching.reply"):
                return NextAction(
                    tool="coaching.reply",
                    arguments={"session_id": session_id, "text": text},
                    thought="学员发言，模拟客户回应",
                )
            return NextAction(tool=None)

        # 开启新会话
        if not obs_of("coaching.start_session"):
            persona_id = str(ctx.get("persona_id") or "").strip()
            if not persona_id:
                found = _PERSONA_PATTERN.search(text)
                persona_id = found.group(0) if found else ""
            if not persona_id:
                # 未指定画像时取第一个可用画像（真实系统应由前端选择）
                persona_id = sorted(service.personas)[0] if service.personas else ""
            if not persona_id:
                return NextAction(
                    tool="platform.handoff_to_human",
                    arguments={"reason": "无可用客户画像，无法开启陪练", "urgency": "normal"},
                    thought="无画像，转人工",
                )
            return NextAction(
                tool="coaching.start_session",
                arguments={"persona_id": persona_id, "trainee": str(ctx.get("trainee") or "trainee")},
                thought="开启陪练会话",
            )
        return NextAction(tool=None)

    # ------------------------------------------------------------------ #
    # ⑤ 评审
    # ------------------------------------------------------------------ #
    def critique(state: AgentState) -> dict[str, Any]:
        route_name = state.get("route", "practice")
        observations = state.get("observations", [])

        def ok(tool: str) -> bool:
            return any(o.get("tool") == tool and o.get("ok") for o in observations)

        checks: dict[str, Any] = {}
        if route_name == "qc":
            checks["qc_done"] = ok("coaching.qc")
            report = next(
                (o.get("data") or {} for o in observations if o.get("tool") == "coaching.qc"), {}
            )
            if report.get("needs_alert"):
                # 高风险必须已发起告警（是否批准由人决定，但动作不能少）
                checks["alert_attempted"] = any(
                    o.get("tool") == "platform.send_external_message" for o in observations
                )
                passed = checks["qc_done"] and checks["alert_attempted"]
            else:
                passed = checks["qc_done"]
            return {
                "passed": bool(passed),
                "checks": checks,
                "notes": [] if passed else ["高风险问题未发起告警，不符合质检流程要求"],
            }

        if route_name == "mining":
            passed = ok("coaching.mine_phrases")
            return {"passed": passed, "checks": {"mining_done": passed}, "notes": []}

        # 陪练：至少完成一次交互
        passed = ok("coaching.start_session") or ok("coaching.finish_session") or ok("coaching.reply")
        return {"passed": passed, "checks": {"practice_progressed": passed}, "notes": []}

    # ------------------------------------------------------------------ #
    # ⑥ 应答
    # ------------------------------------------------------------------ #
    def respond(state: AgentState) -> dict[str, Any]:
        route_name = state.get("route", "practice")
        observations = state.get("observations", [])

        def data_of(tool: str) -> dict[str, Any]:
            for o in reversed(observations):
                if o.get("tool") == tool:
                    return o.get("data") or {}
            return {}

        if route_name == "qc":
            report = data_of("coaching.qc")
            alert = data_of("platform.send_external_message")
            approvals = state.get("approvals") or []
            return {
                "route": "qc",
                "report": report,
                "report_text": report.get("report_text", ""),
                "needs_alert": report.get("needs_alert", False),
                "alert_sent": bool(alert.get("sent")),
                "alert_approvals": approvals,
            }

        if route_name == "mining":
            result = data_of("coaching.mine_phrases")
            return {"route": "mining", "result": result}

        start = data_of("coaching.start_session")
        reply = data_of("coaching.reply")
        card = data_of("coaching.finish_session")
        if card:
            return {
                "route": "practice",
                "stage": "finished",
                "scorecard": card,
                "report_text": card.get("report_text", ""),
            }
        if reply:
            return {
                "route": "practice",
                "stage": "in_progress",
                "session_id": reply.get("session_id"),
                "customer_reply": reply.get("customer_reply"),
                "resistant": reply.get("resistant"),
            }
        return {
            "route": "practice",
            "stage": "started",
            "session_id": start.get("session_id"),
            "persona": start.get("persona"),
            "opening": start.get("opening"),
        }

    return AgentCapabilities(
        name=AGENT_NAME,
        description="业务员话术陪练与 AI 考核：模拟客户陪练 / 四维评分 / 全量质检 / 话术挖掘",
        tools=tools,
        decide=decide,
        respond=respond,
        route=route,
        tool_tags_for=tool_tags_for,
        plan=plan,
        critique=critique,
        max_critique_rounds=2,
        version="1.0.0",
        tags=("coaching", "practice", "qc", "mining"),
    )
