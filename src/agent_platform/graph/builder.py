"""编排图装配器 —— 把"能力声明"编译成可运行的 LangGraph 状态机。

图的形态（节点按需装配，不需要的自动跳过）：

    START → [route] → [plan] → execute ⇄ (循环)
                                  │
                                  ├─ done ──▶ [critique] ──revise──▶ execute
                                  │                 └──── accept ──┐
                                  └─ escalate ──────────────────▶ respond → END

这张图就是平台对外的"产品"：业务 Agent 只要提供能力函数，
就自动获得 状态持久化 / 断点恢复 / HITL 审批 / 失败自愈 / 预算熔断 / 全链路观测。
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from agent_platform.graph.capabilities import AgentCapabilities
from agent_platform.graph.nodes import (
    GraphDeps,
    make_critique_node,
    make_execute_node,
    make_plan_node,
    make_respond_node,
    make_route_node,
)
from agent_platform.logging_config import get_logger
from agent_platform.state.schema import AgentState

logger = get_logger(__name__)


def build_agent_graph(
    caps: AgentCapabilities,
    deps: GraphDeps,
    checkpointer: Any | None = None,
) -> Any:
    """装配并编译一个业务 Agent 的编排图。

    参数：
        caps:         业务能力声明
        deps:         平台运行时依赖
        checkpointer: 状态持久化后端（None 则无持久化，仅用于极简测试）
    返回：
        LangGraph 编译后的图对象（可直接 .invoke / .get_state）
    """
    has_plan = caps.plan is not None
    has_critique = caps.critique is not None

    graph = StateGraph(AgentState)

    # ---------------- 注册节点 ----------------
    graph.add_node("route", make_route_node(caps, deps))
    if has_plan:
        graph.add_node("plan", make_plan_node(caps, deps))
    graph.add_node("execute", make_execute_node(caps, deps))
    if has_critique:
        graph.add_node("critique", make_critique_node(caps, deps))
    graph.add_node("respond", make_respond_node(caps, deps))

    # ---------------- 入口与主干边 ----------------
    graph.add_edge(START, "route")
    if has_plan:
        graph.add_edge("route", "plan")
        graph.add_edge("plan", "execute")
    else:
        graph.add_edge("route", "execute")

    # ---------------- 执行循环的条件路由 ----------------
    def _after_execute(state: AgentState) -> str:
        """执行节点之后走哪里。

        continue → 回到 execute（还有下一步）
        escalate → 直接 respond（预算熔断，转人工结论）
        done     → 有 Critic 就先去评审，否则直接应答
        """
        flow = state.get("flow", "continue")
        if flow == "continue":
            return "execute"
        if flow == "escalate":
            return "respond"
        return "critique" if has_critique else "respond"

    execute_map: dict[str, str] = {"execute": "execute", "respond": "respond"}
    if has_critique:
        execute_map["critique"] = "critique"
    graph.add_conditional_edges("execute", _after_execute, execute_map)

    # ---------------- 评审后的条件路由 ----------------
    if has_critique:

        def _after_critique(state: AgentState) -> str:
            """评审不通过 → 回退重做；通过 → 出结果。"""
            return "execute" if state.get("flow") == "revise" else "respond"

        graph.add_conditional_edges(
            "critique", _after_critique, {"execute": "execute", "respond": "respond"}
        )

    graph.add_edge("respond", END)

    compiled = graph.compile(checkpointer=checkpointer)
    logger.info(
        "装配 Agent 图：%s（planner=%s, critic=%s, tools=%d）",
        caps.name, has_plan, has_critique, len(caps.tools.all()),
    )
    return compiled


def recursion_limit_for(
    max_steps: int, has_critique: bool, *, critique_rounds: int | None = None
) -> int:
    """计算 LangGraph 的 recursion_limit。

    为什么要显式算：LangGraph 默认 25，若 max_steps 配得较大（如 20），
    会在到熔断之前先撞上图自身的递归上限，抛出 GraphRecursionError，
    导致"熔断转人工"这条业务路径永远走不到 —— 这是很隐蔽的坑。

    估算：路由/规划各 1 + 每步执行 1 + 每轮评审 2 + 应答 1 + 余量。
    critique_rounds 默认按"评审上限 + 1"取（critique 节点在强制接受前
    会多走一轮判定，见 nodes.py 的 force_accept 逻辑），传 0 视为无评审。
    """
    per_step = 1
    extra = 1 + (1 if has_critique else 0)
    rounds = (critique_rounds if critique_rounds is not None else 3) if has_critique else 0
    return max_steps * per_step + extra + rounds * 2 + 5
