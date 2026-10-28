"""把项目二装配成平台上的一个业务 Agent。

这是"装配式开发"的样板：
    本文件没有一行编排代码 —— 没有状态机、没有中断恢复、没有重试熔断、
    没有可观测。只提供 6 个纯函数（route / tool_tags_for / plan / decide /
    critique / respond），平台就把它们编译成一个生产级 Agent。

对比：如果没有平台，这些能力每个项目都要重写一遍（对应"新 Agent 接入 60 人日"）。
"""

from __future__ import annotations

import re
from typing import Any

from agent_platform.graph.capabilities import AgentCapabilities, NextAction
from agent_platform.logging_config import get_logger
from agent_platform.state.schema import AgentState
from agent_platform.tools.registry import ToolRegistry
from agent_platform.tools.spec import ToolSpec
from product_assistant.domain.models import MatchResult
from product_assistant.service import ProductAssistantService

logger = get_logger(__name__)

AGENT_NAME = "product_assistant"

# 从自然语言里提取客户编号（演示数据用 C1001 / CU001 这类格式；真实系统应改用上下文注入）
_CUSTOMER_ID_PATTERN = re.compile(r"\b[A-Z]{1,3}\d{3,6}\b", re.IGNORECASE)
# 从自然语言里提取产品编号（如 BANK-A-GJJ-001）
_PRODUCT_CODE_PATTERN = re.compile(r"\bBANK-[A-Z0-9-]+\b", re.IGNORECASE)

# 路由关键词
_PROPOSAL_KEYS = ("方案书", "生成方案", "出方案", "方案文档", "出一份", "整理成方案")
_MATCH_KEYS = ("匹配", "推荐", "方案", "能办", "能不能做", "做什么产品", "产品组合", "适合")


def _extract_customer_id(state: AgentState) -> str:
    """优先取上下文里的 customer_id，其次从输入文本里提取。"""
    ctx = state.get("context") or {}
    cid = str(ctx.get("customer_id") or "").strip()
    if cid:
        return cid
    match = _CUSTOMER_ID_PATTERN.search(state.get("user_input", ""))
    return match.group(0).upper() if match else ""


def _extract_product_code(state: AgentState) -> str:
    ctx = state.get("context") or {}
    code = str(ctx.get("product_code") or "").strip()
    if code:
        return code
    match = _PRODUCT_CODE_PATTERN.search(state.get("user_input", ""))
    return match.group(0).upper() if match else ""


def _as_of_from(state: AgentState) -> str:
    """业务上下文里可以指定"按哪天的政策"（回测/复现历史报价时用）。"""
    ctx = state.get("context") or {}
    return str(ctx.get("as_of") or "")


def _observations_of(state: AgentState, tool: str) -> list[dict[str, Any]]:
    return [o for o in state.get("observations", []) if o.get("tool") == tool]


def build_product_assistant_agent(
    service: ProductAssistantService,
    tools: ToolRegistry,
) -> AgentCapabilities:
    """构造产品知识助手的 Agent 能力声明。"""

    # ------------------------------------------------------------------ #
    # ① 意图路由
    # ------------------------------------------------------------------ #
    def route(state: AgentState) -> str:
        text = state.get("user_input", "")
        cid = _extract_customer_id(state)

        if any(k in text for k in _PROPOSAL_KEYS):
            return "proposal"
        # 出现客户编号 + 方案类词 → 走匹配
        if cid and any(k in text for k in _MATCH_KEYS):
            return "match"
        if any(k in text for k in ("匹配", "推荐产品", "产品组合")):
            return "match"
        return "qa"

    # ------------------------------------------------------------------ #
    # ② 作用域：按路由只给相关工具（token 优化的来源）
    # ------------------------------------------------------------------ #
    def tool_tags_for(state: AgentState) -> list[str]:
        mapping = {
            "qa": ["product"],              # 检索/详情/图谱
            "match": ["customer", "matching"],  # 档案 + 匹配
            "proposal": ["proposal"],       # 方案书
        }
        return mapping.get(state.get("route", "qa"), ["product"])

    # ------------------------------------------------------------------ #
    # ③ 规划（给人看的步骤，同时便于观测）
    # ------------------------------------------------------------------ #
    def plan(state: AgentState, scoped: list[ToolSpec]) -> list[str]:
        route_name = state.get("route", "qa")
        if route_name == "match":
            return [
                "读取客户档案（资质/征信/负债/需求）",
                "执行硬性准入筛选，记录淘汰原因",
                "按通过率→利率→额度多目标排序",
                "产出主力 + 备选组合，无结果则转人工",
            ]
        if route_name == "proposal":
            return ["复用匹配结果", "填充方案书模板", "附加合规免责与风险提示"]
        return ["检索产品政策条款（混合检索 + 生效日期过滤）", "判断是否有政策依据", "组织带引用的回答"]

    # ------------------------------------------------------------------ #
    # ④ 执行决策（必须确定性：HITL 恢复会重放本节点）
    # ------------------------------------------------------------------ #
    def decide(state: AgentState, scoped: list[ToolSpec]) -> NextAction:
        route_name = state.get("route", "qa")
        cid = _extract_customer_id(state)
        as_of = _as_of_from(state)

        def has_handoff() -> bool:
            return bool(_observations_of(state, "platform.handoff_to_human"))

        # ---------------- 问答 ----------------
        if route_name == "qa":
            calls = _observations_of(state, "product.search_policy")
            if not calls:
                args: dict[str, Any] = {"query": state.get("user_input", "")}
                code = _extract_product_code(state)
                if code:
                    args["product_code"] = code
                if as_of:
                    args["as_of"] = as_of
                return NextAction(tool="product.search_policy", arguments=args, thought="先检索政策条款")

            latest = calls[-1]
            data = latest.get("data") or {}
            # 有政策依据 → 收手，交给 Critic/Respond
            if latest.get("ok") and not data.get("refused"):
                return NextAction(tool=None, thought="已取得政策依据，组织回答")
            # 无依据 → 转人工（平台内建逃生舱，不会被作用域裁剪挡掉）
            if not has_handoff():
                return NextAction(
                    tool="platform.handoff_to_human",
                    arguments={"reason": "产品问题无政策依据，需人工顾问核实", "urgency": "normal"},
                    thought="无政策依据，转人工",
                )
            return NextAction(tool=None, thought="已转人工，输出拒答说明")

        # ---------------- 匹配 ----------------
        if route_name == "match":
            if not cid:
                if not has_handoff():
                    return NextAction(
                        tool="platform.handoff_to_human",
                        arguments={"reason": "缺少客户档案编号，无法执行方案匹配", "urgency": "normal"},
                        thought="缺少客户信息，转人工补全",
                    )
                return NextAction(tool=None)

            calls = _observations_of(state, "product.match")
            if not calls:
                args = {"customer_id": cid}
                if as_of:
                    args["as_of"] = as_of
                return NextAction(tool="product.match", arguments=args, thought="执行结构化方案匹配")

            latest = calls[-1]
            data = latest.get("data") or {}
            if latest.get("ok") and data.get("matched"):
                return NextAction(tool=None, thought="已匹配到方案，输出组合")
            if not has_handoff():
                reason = data.get("handoff_reason") or "无匹配产品"
                return NextAction(
                    tool="platform.handoff_to_human",
                    arguments={"reason": f"方案匹配无结果：{reason}", "urgency": "high"},
                    thought="无匹配产品，转人工专家",
                )
            return NextAction(tool=None)

        # ---------------- 方案书 ----------------
        if not cid:
            if not has_handoff():
                return NextAction(
                    tool="platform.handoff_to_human",
                    arguments={"reason": "缺少客户档案编号，无法生成方案书", "urgency": "normal"},
                    thought="缺少客户信息，转人工补全",
                )
            return NextAction(tool=None)

        calls = _observations_of(state, "product.generate_proposal")
        if not calls:
            args = {"customer_id": cid}
            if as_of:
                args["as_of"] = as_of
            return NextAction(tool="product.generate_proposal", arguments=args, thought="生成结构化方案书")

        latest = calls[-1]
        data = latest.get("data") or {}
        if latest.get("ok") and data.get("generated"):
            return NextAction(tool=None, thought="方案书已生成")
        if not has_handoff():
            return NextAction(
                tool="platform.handoff_to_human",
                arguments={"reason": f"方案书生成失败：{data.get('reason', '无匹配产品')}", "urgency": "normal"},
                thought="生成失败，转人工",
            )
        return NextAction(tool=None)

    # ------------------------------------------------------------------ #
    # ⑤ 评审（合规校验）—— 这是本 Agent 的"质检环节"
    # ------------------------------------------------------------------ #
    def critique(state: AgentState) -> dict[str, Any]:
        route_name = state.get("route", "qa")
        handed_off = bool(_observations_of(state, "platform.handoff_to_human"))
        checks: dict[str, Any] = {}

        if route_name == "qa":
            calls = _observations_of(state, "product.search_policy")
            data = (calls[-1].get("data") or {}) if calls else {}
            checks["has_citation"] = bool(data.get("citations"))
            checks["refused"] = bool(data.get("refused"))
            # 合规规则：有回答就必须有引用；拒答就必须已转人工
            passed = checks["has_citation"] or (checks["refused"] and handed_off)
            notes = [] if passed else ["回答缺少政策引用，或拒答后未转人工 —— 违反合规要求"]
            return {"passed": passed, "checks": checks, "notes": notes}

        if route_name == "match":
            calls = _observations_of(state, "product.match")
            data = (calls[-1].get("data") or {}) if calls else {}
            matched = bool(data.get("matched"))
            checks["matched"] = matched
            checks["handoff"] = handed_off
            passed = matched or (not matched and handed_off)
            return {
                "passed": passed,
                "checks": checks,
                "notes": [] if passed else ["无匹配产品但未转人工，按业务规则不允许硬配"],
            }

        # 方案书必须生成成功，否则应已转人工
        calls = _observations_of(state, "product.generate_proposal")
        data = (calls[-1].get("data") or {}) if calls else {}
        generated = bool(data.get("generated"))
        checks["generated"] = generated
        checks["handoff"] = handed_off
        passed = generated or handed_off
        return {"passed": passed, "checks": checks, "notes": [] if passed else ["方案书未生成且未转人工"]}

    # ------------------------------------------------------------------ #
    # ⑥ 应答
    # ------------------------------------------------------------------ #
    def respond(state: AgentState) -> dict[str, Any]:
        route_name = state.get("route", "qa")
        handoff_obs = _observations_of(state, "platform.handoff_to_human")
        handoff_detail = (handoff_obs[-1].get("data") or {}) if handoff_obs else None

        if route_name == "qa":
            calls = _observations_of(state, "product.search_policy")
            data = (calls[-1].get("data") or {}) if calls else {}
            return {
                "route": "qa",
                "answer": data.get("answer", ""),
                "citations": data.get("citations", []),
                "refused": data.get("refused", False),
                "related_products": data.get("related_products", []),
                "handoff": handoff_detail,
                "retrieval": data.get("hits", []),
            }

        if route_name == "match":
            calls = _observations_of(state, "product.match")
            data = (calls[-1].get("data") or {}) if calls else {}
            if data.get("matched"):
                try:
                    result = MatchResult.model_validate(data)
                    summary = result.to_text()
                except Exception:  # noqa: BLE001 - 渲染失败不影响结构化结果返回
                    summary = "已生成方案匹配结果（结构化数据见 match 字段）"
            else:
                summary = data.get("handoff_reason") or "未匹配到合适产品，已转人工专家处理。"
            return {
                "route": "match",
                "match": data,
                "summary": summary,
                "handoff": handoff_detail,
            }

        calls = _observations_of(state, "product.generate_proposal")
        data = (calls[-1].get("data") or {}) if calls else {}
        return {
            "route": "proposal",
            "generated": data.get("generated", False),
            "proposal_id": data.get("proposal_id"),
            "markdown": data.get("markdown", ""),
            "amount_range": data.get("amount_range", ""),
            "rate_range": data.get("rate_range", ""),
            "handoff": handoff_detail,
        }

    return AgentCapabilities(
        name=AGENT_NAME,
        description="助贷产品知识助手与智能方案匹配：政策问答（可溯源）/ 结构化方案匹配 / 方案书生成",
        tools=tools,
        decide=decide,
        respond=respond,
        route=route,
        tool_tags_for=tool_tags_for,
        plan=plan,
        critique=critique,  # 有 Critic：合规校验
        max_critique_rounds=2,
        version="1.0.0",
        tags=("product", "rag", "matching", "proposal"),
    )
