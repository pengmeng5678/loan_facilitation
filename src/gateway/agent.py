"""把项目六（网关与评测平台）装配成平台上的运维/研发 Agent。

三条路由：
    route   路由预览（"这次为什么走大模型/为什么变贵"）
    eval    评测与 CI 准入（"这次变更能不能发"）
    report  成本与观测报告 / 会话轨迹回放（"钱花哪了、哪一步坏了"）

这个 Agent 的意义在于：**基础设施本身也能被自然语言操作**。
运维不用记 6 个接口路径，直接问"上周成本怎么样、帮我跑一遍评测"就行。
"""

from __future__ import annotations

import re
from typing import Any

from agent_platform.graph.capabilities import AgentCapabilities, NextAction
from agent_platform.logging_config import get_logger
from agent_platform.state.schema import AgentState
from agent_platform.tools.registry import ToolRegistry
from agent_platform.tools.spec import ToolSpec
from gateway.service import GatewayService

logger = get_logger(__name__)

AGENT_NAME = "gateway"

_THREAD_PATTERN = re.compile(r"\b(?:th|gw|grun)_[0-9a-fA-F]{4,}\b")

_EVAL_KEYS = ("评测", "回归", "准入", "能不能发", "gate", "评测集", "通过率", "基线")
_REPORT_KEYS = ("成本", "花了多少", "账单", "回放", "轨迹", "观测", "指标", "费用", "token 消耗")
_ROUTE_KEYS = ("路由", "走哪个模型", "走大模型", "哪个档", "用哪个模型", "为什么这么贵", "档位", "选型")


def build_gateway_agent(service: GatewayService, tools: ToolRegistry) -> AgentCapabilities:
    """构造网关运维 Agent 的能力声明。"""

    # ------------------------------------------------------------------ #
    # ① 意图路由
    # ------------------------------------------------------------------ #
    def route(state: AgentState) -> str:
        text = state.get("user_input", "")
        if any(k in text for k in _EVAL_KEYS):
            return "eval"
        if any(k in text for k in _REPORT_KEYS):
            return "report"
        if any(k in text for k in _ROUTE_KEYS):
            return "route"
        return "report"  # 默认看报告（最常见诉求）

    # ------------------------------------------------------------------ #
    # ② 作用域裁剪
    # ------------------------------------------------------------------ #
    def tool_tags_for(state: AgentState) -> list[str]:
        # 同样**不放** "gateway" 命名空间标签：所有 gateway.* 工具都带它，
        # 放进去等于每个路由都召回全部 6 个工具，作用域裁剪就白做了。
        mapping = {
            "route": ["route"],               # gateway.route_preview
            "eval": ["eval"],                 # run_eval + gate_eval
            "report": ["cost", "observe"],     # cost_report / stats / replay
        }
        return mapping.get(state.get("route", "report"), ["cost", "observe"])

    # ------------------------------------------------------------------ #
    # ③ 规划
    # ------------------------------------------------------------------ #
    def plan(state: AgentState, scoped: list[ToolSpec]) -> list[str]:
        route_name = state.get("route", "report")
        if route_name == "eval":
            return ["确定评测场景", "跑 Golden Set", "与基线比对给出准入结论"]
        if route_name == "route":
            return ["解析请求文本与任务标签", "计算复杂度", "返回档位与理由"]
        return ["采集网关运行态", "汇总成本与命中率", "必要时回放特定会话轨迹"]

    # ------------------------------------------------------------------ #
    # ④ 执行决策（确定性）
    # ------------------------------------------------------------------ #
    def decide(state: AgentState, scoped: list[ToolSpec]) -> NextAction:
        route_name = state.get("route", "report")
        text = state.get("user_input", "")
        observations = state.get("observations", [])

        def obs_of(tool: str) -> list[dict[str, Any]]:
            return [o for o in observations if o.get("tool") == tool]

        if route_name == "eval":
            # 提到"准入/能不能发"→ 走 gate（含基线比对）；否则只跑评测
            wants_gate = any(k in text for k in ("准入", "能不能发", "gate", "发布", "基线"))
            tool = "gateway.gate_eval" if wants_gate else "gateway.run_eval"
            if not obs_of(tool):
                scene = "all"
                for s in service.list_scenes()["scenes"]:
                    if s in text:
                        scene = s
                        break
                return NextAction(tool=tool, arguments={"scene": scene}, thought=f"执行 {tool}")
            return NextAction(tool=None)

        if route_name == "route":
            if not obs_of("gateway.route_preview"):
                return NextAction(
                    tool="gateway.route_preview",
                    arguments={"text": text, "task": str((state.get("context") or {}).get("task", ""))},
                    thought="预览路由档位",
                )
            return NextAction(tool=None)

        # report：优先按会话回放，其次成本报告，最后统计快照
        found = _THREAD_PATTERN.search(text)
        thread_id = str((state.get("context") or {}).get("thread_id") or (found.group(0) if found else ""))
        if thread_id and not obs_of("gateway.replay"):
            return NextAction(tool="gateway.replay", arguments={"thread_id": thread_id}, thought="回放会话轨迹")
        if any(k in text for k in ("成本", "花了多少", "账单", "费用")) and not obs_of("gateway.cost_report"):
            return NextAction(tool="gateway.cost_report", arguments={}, thought="查询成本报告")
        if not obs_of("gateway.stats") and not obs_of("gateway.cost_report"):
            return NextAction(tool="gateway.stats", arguments={}, thought="采集网关运行态")
        return NextAction(tool=None)

    # ------------------------------------------------------------------ #
    # ⑤ 评审
    # ------------------------------------------------------------------ #
    def critique(state: AgentState) -> dict[str, Any]:
        route_name = state.get("route", "report")
        observations = state.get("observations", [])

        def ok(tool: str) -> bool:
            return any(o.get("tool") == tool and o.get("ok") for o in observations)

        if route_name == "eval":
            # 评测必须真的跑完（跑挂了的评测不能当成"通过"）
            done = ok("gateway.run_eval") or ok("gateway.gate_eval")
            return {"passed": done, "checks": {"eval_done": done}, "notes": [] if done else ["评测未完成"]}
        if route_name == "route":
            done = ok("gateway.route_preview")
            return {"passed": done, "checks": {"route_preview": done}, "notes": []}
        done = ok("gateway.stats") or ok("gateway.cost_report") or ok("gateway.replay")
        return {"passed": done, "checks": {"report_done": done}, "notes": []}

    # ------------------------------------------------------------------ #
    # ⑥ 应答
    # ------------------------------------------------------------------ #
    def respond(state: AgentState) -> dict[str, Any]:
        route_name = state.get("route", "report")
        observations = state.get("observations", [])

        def data_of(tool: str) -> dict[str, Any]:
            for o in reversed(observations):
                if o.get("tool") == tool:
                    return o.get("data") or {}
            return {}

        if route_name == "eval":
            gate = data_of("gateway.gate_eval")
            if gate:
                return {
                    "route": "eval",
                    "mode": "gate",
                    "scene": gate.get("scene"),
                    "current": gate.get("current"),
                    "baseline": gate.get("baseline"),
                    "gate": gate.get("gate"),
                }
            report = data_of("gateway.run_eval")
            return {
                "route": "eval",
                "mode": "run",
                "scene": report.get("scene"),
                "score": report.get("score"),
                "passed": report.get("passed"),
                "failed": report.get("failed"),
                "report_text": report.get("report_text", ""),
            }

        if route_name == "route":
            return {"route": "route", "decision": data_of("gateway.route_preview")}

        replay = data_of("gateway.replay")
        if replay:
            return {"route": "report", "mode": "replay", **replay}
        cost = data_of("gateway.cost_report")
        if cost:
            return {"route": "report", "mode": "cost", "cost": cost}
        return {"route": "report", "mode": "stats", "stats": data_of("gateway.stats")}

    return AgentCapabilities(
        name=AGENT_NAME,
        description=(
            "大模型推理网关与可观测评测平台的运维 Agent："
            "路由预览 / 成本归因 / 会话轨迹回放 / Golden Set 评测与 CI 准入"
        ),
        tools=tools,
        decide=decide,
        respond=respond,
        route=route,
        tool_tags_for=tool_tags_for,
        plan=plan,
        critique=critique,
        max_critique_rounds=2,
        version="1.0.0",
        tags=("gateway", "observability", "evaluation", "cost"),
    )
