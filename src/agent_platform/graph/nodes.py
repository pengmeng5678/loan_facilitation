"""编排图的标准节点实现。

图上共 5 类节点，全部是"就近封装业务回调 + 平台治理"的薄壳：
    route     意图路由（可选）
    plan      任务规划（可选）
    execute   执行循环（决策 → HITL 审批 → 工具调用 → 观察结果 → 下一轮）
    critique  产出评审（可选，不通过则回退重规划）
    respond   最终应答
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from langgraph.types import interrupt

from agent_platform.config import Settings
from agent_platform.errors import StepBudgetExceeded, TokenBudgetExceeded
from agent_platform.graph.capabilities import AgentCapabilities, NextAction
from agent_platform.hitl import ApprovalManager
from agent_platform.llm.base import estimate_tokens, stable_hash
from agent_platform.logging_config import get_logger
from agent_platform.observability import Metrics, Tracer
from agent_platform.resilience import BudgetGuard, RetryPolicy
from agent_platform.state.schema import AgentState
from agent_platform.tools.registry import ToolRegistry
from agent_platform.tools.spec import ToolCallRecord

logger = get_logger(__name__)


def _subject_hash(arguments: dict[str, Any]) -> str:
    """参数指纹：审批单只对"这组参数"有效。

    用规范化 JSON（sorted keys）做稳定哈希，跨进程/跨重启一致；
    不能直接用内置 hash()（带随机盐）。
    """
    payload = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    return format(stable_hash(payload, 10 ** 12), "012d")


def estimate_context_tokens(state: AgentState) -> int:
    """估算"模型本轮真正看到的上下文"的 token 数。

    只算 user_input + schema 是不够的：多轮对话里 observations / messages /
    plan / 上一轮 critique 都会随状态累积，真正的上下文早已不是首轮规模。
    预算守卫必须按全上下文记账，否则"超出预算熔断"只是理论值。
    """
    payload = {
        "user_input": state.get("user_input", ""),
        "context": state.get("context", {}),
        "messages": state.get("messages", []),
        "plan": state.get("plan", []),
        "pending_action": state.get("pending_action"),
        "observations": state.get("observations", []),
        "critique": state.get("critique", {}),
        "approvals": state.get("approvals", []),
    }
    return estimate_tokens(
        json.dumps(payload, ensure_ascii=False, default=str)
    )


@dataclass
class GraphDeps:
    """节点运行时依赖（由 runtime 组装后注入）。"""

    tools: ToolRegistry
    approvals: ApprovalManager
    guard: BudgetGuard
    tracer: Tracer
    metrics: Metrics
    settings: Settings


# =========================================================================== #
# 1. 路由节点
# =========================================================================== #
def make_route_node(caps: AgentCapabilities, deps: GraphDeps):
    """意图路由：决定本轮走哪条业务子流。"""

    def node(state: AgentState) -> dict[str, Any]:
        # 即使没有业务路由函数也记录下来，保证决策轨迹的第一跳永远存在
        with deps.tracer.span("route", kind="node") as sp:
            route = caps.route(state) if caps.route is not None else "default"
            sp.attributes["route"] = route
        deps.metrics.incr("agent_route_total", route=route)
        logger.info("意图路由 → %s", route)
        return {"route": route}

    return node


# =========================================================================== #
# 2. 规划节点
# =========================================================================== #
def make_plan_node(caps: AgentCapabilities, deps: GraphDeps):
    """规划：产出步骤列表，并**在此确定本轮工具作用域**。

    把 scoping 放在规划节点，是因为"要做什么"和"需要哪些工具"是同一件事，
    放在一起可以避免每个执行步都重算一次。
    """

    def node(state: AgentState) -> dict[str, Any]:
        scoped = caps.resolve_scoped_tools(state)
        scoped_names = [t.name for t in scoped]

        # 作用域裁剪的收益量化：全量 vs 裁剪后的 prompt token 对比
        comparison = deps.tools.scoping_comparison(scoped)
        deps.metrics.observe("scoping_saved_percent", comparison["saved_percent"])
        logger.info(
            "工具作用域裁剪：%d/%d 个工具，prompt token %d → %d（省 %.1f%%）",
            comparison["scoped_tools"], comparison["total_tools"],
            comparison["full_prompt_tokens"], comparison["scoped_prompt_tokens"],
            comparison["saved_percent"],
        )

        plan: list[str] = []
        if caps.plan is not None:
            with deps.tracer.span("plan", kind="node") as sp:
                plan = caps.plan(state, scoped)
                sp.attributes["plan_steps"] = len(plan)

        # 规划本身也要计入 token 成本（system prompt + 工具 schema + 用户输入）
        plan_tokens = estimate_tokens(deps.tools.schema_block(scoped)) + estimate_tokens(
            state.get("user_input", "")
        )
        return {
            "plan": plan,
            "plan_index": 0,
            "scoped_tools": scoped_names,
            "tokens_used": plan_tokens,
        }

    return node


# =========================================================================== #
# 3. 执行节点（ReAct 循环体）
# =========================================================================== #
def make_execute_node(caps: AgentCapabilities, deps: GraphDeps):
    """执行一步：预算校验 → 决策 → (HITL 审批) → 工具调用 → 记录观察。

    对应简历"失败分级自愈"的落点：
        - 预算超限     → 抛异常，本节点捕获后置 flow=escalate（熔断转人工）
        - 参数错误     → 工具返回 ok=False + error，写进 observations，
                         下一轮 decide 看到它就能自修正参数
        - 可重试错误   → 由 ToolRegistry 内部按 RetryPolicy 指数退避
    """

    retry_policy = RetryPolicy(
        max_retries=deps.settings.tool_max_retries,
        base_seconds=deps.settings.tool_retry_base_seconds,
    )

    def node(state: AgentState) -> dict[str, Any]:
        step = int(state.get("step", 0))
        tokens_used = int(state.get("tokens_used", 0))

        # ---- ① 预算守卫：死循环烧钱的最后一道闸 ----
        try:
            deps.guard.check_steps(step)
            deps.guard.check_tokens(tokens_used)
        except (StepBudgetExceeded, TokenBudgetExceeded) as exc:
            logger.warning("触发预算熔断：%s", exc)
            deps.metrics.incr("agent_budget_breaker_total")
            return {
                "flow": "escalate",
                "status": "escalated",
                "error": str(exc),
            }

        scoped = caps.resolve_scoped_tools(state)
        allowed = {t.name for t in scoped}
        schema_block = deps.tools.schema_block(scoped)
        turn_tokens = estimate_tokens(schema_block) + estimate_context_tokens(state)

        # ---- ② 业务决策：下一步做什么 ----
        with deps.tracer.span("decide", kind="node", step=step) as sp:
            action: NextAction = caps.decide(state, scoped)
            sp.attributes["tool"] = action.tool or "(finish)"

        # 决策为"收手" → 进入评审/应答
        if action.tool is None:
            logger.info("第 %d 步：决策收手，转入评审/应答", step)
            return {"step": step + 1, "tokens_used": turn_tokens, "flow": "done", "pending_action": None}

        # ---- ③ 工具不存在：把错误回灌给模型自修正 ----
        if not caps.tools.has(action.tool):
            logger.warning("第 %d 步：模型选择了未注册工具 %s，回灌自修正", step, action.tool)
            deps.metrics.incr("agent_invalid_tool_total")
            observation = {
                "step": step,
                "tool": action.tool,
                "ok": False,
                "error": f"工具 {action.tool} 不存在。可用工具：{sorted(allowed)}",
            }
            return {
                "step": step + 1,
                "tokens_used": turn_tokens,
                "flow": "continue",
                "pending_action": None,
                "observations": [observation],
            }

        spec = caps.tools.get(action.tool)

        # ---- ④ 写工具重放保护：评审打回时绝不重复产生副作用 ----
        # 图的结构是 critique --revise--> execute，重做会把 execute 再走一遍；
        # 而 decide 是状态纯函数，大概率还会选择同一个写工具。如果直接重放，
        # 就会出现"客户收到两条同样短信 / 方案书出现第二个版本"这类真实事故。
        # 处理方式：写工具只回放上次成功的观察结果，让 decide 正常收敛；
        # 幂等工具（读、查询、带幂等键的写）不在此列，允许真实重放。
        executed = set(state.get("executed_write_tools", []))
        if not spec.idempotent and spec.name in executed:
            previous = next(
                (
                    o
                    for o in reversed(state.get("observations", []))
                    if o.get("tool") == spec.name and o.get("ok")
                ),
                None,
            )
            deps.metrics.incr("agent_write_replay_blocked_total", tool=spec.name)
            logger.warning(
                "第 %d 步：写工具 %s 在本线程已执行过，回放上次结果而不重复执行", step, spec.name
            )
            return {
                "step": step + 1,
                "tokens_used": turn_tokens,
                "flow": "continue",
                "pending_action": None,
                "observations": [
                    {
                        "step": step,
                        "tool": spec.name,
                        "arguments": action.arguments,
                        "ok": True,
                        "data": (previous or {}).get("data"),
                        "error": None,
                        "thought": action.thought,
                        "replayed": True,
                        "skipped": "already_executed_in_thread",
                    }
                ],
            }

        need_approval, reason = deps.approvals.requires_approval(spec)

        # ---- ⑤ HITL：高风险动作挂起等人工审批 ----
        # approval_updates 用来承载"审批留痕"，无论通过还是拒绝都要写回状态，
        # 以便审计时能回答"这个动作是谁批的、什么时候批的"。
        approval_updates: dict[str, Any] = {}
        if need_approval:
            # 审批请求 ID 必须**确定性**生成：LangGraph 恢复时会重放本节点，
            # 用 (thread_id, step, tool, 参数指纹) 做 key 才能保证不产生重复待办。
            # 参数指纹不能省：同一工具换个参数就是另一个动作，审批不能复用。
            subject_hash = _subject_hash(action.arguments)
            approval_key = f"{state.get('thread_id', '')}:{step}:{spec.name}:{subject_hash}"
            request_id = "apr_" + format(stable_hash(approval_key, 10 ** 12), "012d")
            approval_req, created = deps.approvals.create_or_get(
                request_id,
                run_id=str(state.get("run_id", "")),
                thread_id=str(state.get("thread_id", "")),
                spec=spec,
                arguments=action.arguments,
                reason=reason,
                subject_hash=subject_hash,
            )
            if created:
                deps.metrics.incr("agent_approval_required_total", tool=spec.name)
            else:
                # 防御性校验：审批单绑定的必须是"这组参数"
                deps.approvals.assert_subject(approval_req, action.arguments)

            # interrupt 会抛出一个特殊控制流，把图挂起；恢复时本节点从头重放，
            # 此处 interrupt 直接返回审批结论。因此上面的 decide 必须确定性。
            payload = {
                "type": "approval_required",
                "request_id": approval_req.request_id,
                "tool": spec.name,
                "arguments": action.arguments,
                "risk_level": spec.risk_level,
                "reason": reason,
                "step": step,
            }

            if created or approval_req.status == "pending":
                logger.info(
                    "第 %d 步：工具 %s 需要人工审批（%s），挂起等待",
                    step, spec.name, approval_req.request_id,
                )
                decision = interrupt(payload)

                decided = decision if isinstance(decision, dict) else {"approved": bool(decision)}
                approved = bool(decided.get("approved"))
                decided_by = decided.get("decided_by", "unknown")
                comment = decided.get("comment")

                # 把审批结论回写到 ApprovalManager（审批中心 / 审计要用）
                deps.approvals.resolve(
                    approval_req.request_id, approved=approved, decided_by=decided_by, comment=comment
                )
            else:
                # 恢复重放时审批已有结论（approved / rejected / timeout）：
                # **直接采用已有结论，绝不再挂起**。否则每次 resume 都会重新
                # interrupt 同一个动作，形成"批了也执行不了"的死循环 ——
                # 这是 HITL 恢复语义的关键一环：重放必须收敛到同一结论。
                approved = approval_req.status == "approved"
                decided_by = approval_req.decided_by or "unknown"
                comment = approval_req.comment
                logger.info(
                    "第 %d 步：恢复重放，采用已有审批结论 %s → %s",
                    step, approval_req.request_id, approval_req.status,
                )

            approvals_record = {
                "request_id": approval_req.request_id,
                "tool": spec.name,
                "arguments": action.arguments,
                "approved": approved,
                "decided_by": decided_by,
                "comment": comment,
                "risk_level": spec.risk_level,
                "ts": time.time(),
            }
            approval_updates["approvals"] = [approvals_record]
            if created:
                deps.metrics.incr(
                    "agent_approval_decided_total", tool=spec.name, approved=str(approved)
                )

            if not approved:
                logger.warning("第 %d 步：人工拒绝执行 %s", step, spec.name)
                return {
                    "step": step + 1,
                    "tokens_used": turn_tokens,
                    "flow": "done",
                    "pending_action": None,
                    "observations": [
                        {
                            "step": step,
                            "tool": spec.name,
                            "ok": False,
                            "error": "人工审批未通过，该动作已取消。请调整策略或转人工处理。",
                            "approval_rejected": True,
                        }
                    ],
                    **approval_updates,
                }

        # ---- ⑥ 真正执行工具 ----
        with deps.tracer.span("tool", kind="tool", tool=spec.name, step=step) as sp:
            result = caps.tools.invoke(
                spec.name, action.arguments, allowed=allowed, retry_policy=retry_policy
            )
            sp.attributes["ok"] = result.ok
            sp.attributes["latency_ms"] = round(result.latency_ms, 2)
            if not result.ok:
                sp.status = "error"
                sp.error = result.error

        deps.metrics.incr("tool_calls_total", tool=spec.name, ok=str(result.ok))
        deps.metrics.observe("tool_latency_ms", result.latency_ms, tool=spec.name)

        record = ToolCallRecord(
            tool=spec.name,
            arguments=action.arguments,
            ok=result.ok,
            latency_ms=result.latency_ms,
            step=step,
            error=result.error,
            blocked=result.blocked,
        )
        observation = {
            "step": step,
            "tool": spec.name,
            "arguments": action.arguments,
            "ok": result.ok,
            "data": result.data,
            "error": result.error,
            "thought": action.thought,
        }

        return {
            "step": step + 1,
            "tokens_used": turn_tokens,
            "flow": "continue",  # 继续下一轮，由 decide 判断是否收手
            "pending_action": action.__dict__,
            "tool_calls": [record.to_dict()],
            "observations": [observation],
            # 只有"成功执行的非幂等写工具"才登记，失败的允许下一轮修正后重试
            "executed_write_tools": [spec.name] if (result.ok and not spec.idempotent) else [],
            **approval_updates,
        }

    return node


# =========================================================================== #
# 4. 评审节点（Critic）
# =========================================================================== #
def make_critique_node(caps: AgentCapabilities, deps: GraphDeps):
    """对当前产出做事实性 / 合规性校验，不通过则回退重做。

    注意：Critic 只在高价值场景启用（简单任务启用会拖慢延迟），
    这个开关由业务 Agent 通过"是否提供 critique 回调"来决定。
    """

    def node(state: AgentState) -> dict[str, Any]:
        if caps.critique is None:
            return {"flow": "accept"}

        with deps.tracer.span("critique", kind="node") as sp:
            result = caps.critique(state)
            sp.attributes["passed"] = bool(result.get("passed", True))

        rounds = int(state.get("critique_rounds", 0)) + 1
        passed = bool(result.get("passed", True))
        # 超过最大轮数就强制接受，避免"评审-重做"死循环
        force_accept = rounds >= caps.max_critique_rounds
        flow = "accept" if (passed or force_accept) else "revise"

        # 统一评审结论结构，便于前端/审计按同一套字段展示：
        #   passed      —— 是否通过
        #   reasons     —— 不通过的具体原因（兼容业务自定义的 notes）
        #   revise_hint —— 给下一轮执行的可操作提示（可为空）
        verdict: dict[str, Any] = dict(result)
        verdict["passed"] = passed
        verdict["reasons"] = list(result.get("reasons") or result.get("notes") or [])
        verdict.setdefault("revise_hint", None)
        verdict["forced_accept"] = bool(force_accept and not passed)

        deps.metrics.incr("agent_critique_total", passed=str(passed))
        if force_accept and not passed:
            deps.metrics.incr("agent_critique_forced_accept_total", agent=caps.name)
            logger.warning(
                "评审第 %d 轮仍未通过，达上限 %d，强制接受（原因：%s）",
                rounds, caps.max_critique_rounds, "；".join(verdict["reasons"]) or "未说明",
            )
        else:
            logger.info("评审第 %d 轮：passed=%s → %s", rounds, passed, flow)
        return {
            "critique": verdict,
            "critique_rounds": rounds,
            "needs_revision": not passed,
            "flow": flow,
        }

    return node


# =========================================================================== #
# 5. 应答节点
# =========================================================================== #
def make_respond_node(caps: AgentCapabilities, deps: GraphDeps):
    """生成最终业务输出，并落定 run 的终态。"""

    def node(state: AgentState) -> dict[str, Any]:
        with deps.tracer.span("respond", kind="node"):
            output = caps.respond(state)

        # 终态判定：被熔断/审批拒绝的 run 不能被"应答成功"覆盖掉状态
        current = state.get("status", "running")
        status = current if current in ("escalated", "failed") else "completed"

        deps.metrics.incr("agent_runs_total", agent=caps.name, status=status)
        return {"output": output, "status": status}

    return node
