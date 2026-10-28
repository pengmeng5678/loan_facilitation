"""编排状态定义（LangGraph 的 State）。

设计要点：
    1. 状态是"整条流程的共享内存"——节点读它、写部分字段，LangGraph 负责归并。
    2. 需要"累加"的字段（消息、工具调用、观察结果）用 Annotated + reducer 声明，
       节点只需要返回"本次新增的部分"，不用自己读旧值再拼接（否则并发分支会互相覆盖）。
    3. 字段全部可 JSON 序列化 —— 这是 Checkpointer 能落盘、能断点恢复的前提。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class AgentState(TypedDict, total=False):
    """Agent 单次运行（run）在整个图上的共享状态。

    total=False 表示所有字段都是可选的 —— 图初始化时只给输入字段，
    其余字段由各节点逐步填充，这也更贴近真实编排的写法。
    """

    # ---------------- 输入（由 runtime 注入） ----------------
    run_id: str  # 单次运行唯一标识（可观测 / 审计主键）
    agent_name: str  # 当前业务 Agent 名称
    thread_id: str  # 会话（线程）标识，Checkpointer 按它做状态隔离
    user_input: str  # 用户本轮输入
    context: dict[str, Any]  # 业务上下文，如客户档案、会话历史摘要

    # ---------------- 运行期：对话与推理 ----------------
    # 累加语义：节点只需返回新增消息
    messages: Annotated[list[dict[str, Any]], operator.add]
    # 规划结果（Planner 产出）
    plan: list[str]
    plan_index: int

    # ---------------- 运行期：工具与观察 ----------------
    step: int  # 已执行步数，用于熔断
    route: str  # 意图路由结果（router 节点写入）
    flow: str  # 图内部的路由信号（continue / critique / respond / escalate）
    pending_action: dict[str, Any] | None  # 本轮决定要执行的工具动作（Executor 写入）
    scoped_tools: list[str]  # 本轮作用域内的工具名（作用域裁剪的产物）
    # 累加语义：审计用，记录每次工具调用
    tool_calls: Annotated[list[dict[str, Any]], operator.add]
    # 累加语义：工具返回结果
    observations: Annotated[list[dict[str, Any]], operator.add]
    # 累加语义：本线程内已成功执行过的"非幂等写工具"名。
    # 用途：Critic 打回 revise 时图会重放 execute，写工具若再执行一次就会产生
    # 重复副作用（重复发消息、重复生成方案书版本）。读工具可以重放，
    # 写工具只能"回放上次结果"，这是重做循环的安全前提。
    executed_write_tools: Annotated[list[str], operator.add]

    # ---------------- 运行期：治理 ----------------
    # 累加语义：token 用量，用于预算守卫
    tokens_used: Annotated[int, operator.add]

    # ---------------- 运行期：评审（Critic） ----------------
    critique: dict[str, Any]
    critique_rounds: int
    needs_revision: bool

    # ---------------- 运行期：人工审批（HITL） ----------------
    pending_approval: dict[str, Any] | None
    # 累加语义：审批记录，审计用
    approvals: Annotated[list[dict[str, Any]], operator.add]

    # ---------------- 输出 ----------------
    output: dict[str, Any]  # 业务结果（结构化，供 API 直接返回）
    status: str  # running | completed | interrupted | failed | escalated
    error: str  # 失败原因（status=failed 时有值）


# 初始化状态时使用的空模板：保证图入口拿到完整结构，避免 KeyError
def initial_state(
    *,
    run_id: str,
    agent_name: str,
    thread_id: str,
    user_input: str,
    context: dict[str, Any] | None = None,
) -> AgentState:
    """构造图的初始状态。只放"输入"，其余字段由节点填充。"""
    return AgentState(
        run_id=run_id,
        agent_name=agent_name,
        thread_id=thread_id,
        user_input=user_input,
        context=context or {},
        messages=[],
        plan=[],
        plan_index=0,
        step=0,
        route="",
        flow="",
        pending_action=None,
        scoped_tools=[],
        tool_calls=[],
        observations=[],
        executed_write_tools=[],
        tokens_used=0,
        critique={},
        critique_rounds=0,
        needs_revision=False,
        pending_approval=None,
        approvals=[],
        output={},
        status="running",
        error="",
    )
