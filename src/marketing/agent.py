"""把项目七（素材推广与客户激活）装配成平台上的业务 Agent。

五条路由：
    generate     素材批量生成 + 渠道语感适配（默认路由）
    case         成单案例脱敏包装（客户证言式素材）
    activation   未成单客户激活（对症素材 + 发送节奏）
    audit        合规审核（先审后发，业务员自己写的也能先过一遍）
    attribution  素材效果归因（哪种钩子真的带来线索）

两条贯穿全局的**流程约束**（不是技术选择，是合规要求）：

1. **先审后发**。素材只有在 `MaterialGenerator.assemble()` 里才会被构造出来，
   而 assemble 内部一定会跑合规引擎 —— 也就是说"生成"与"过审"在代码上
   是同一个动作，不存在"绕过审核直接产素材"的路径。没通过（rejected）
   的素材不会被激活计划采用（ActivationEngine 只取 approved/sanitized）。

2. **发布/发送必须有人点头**。`platform.send_external_message` 是 high 风险 +
   非幂等工具，平台会自动插入人工审批检查点；并且本 Agent 做了**最小权限裁剪**：
   只有在用户明确说"发出去/推给他"时，才把 message 标签放进作用域 —— 其余
   路由连这个工具都看不到，Agent 想发也发不了。
"""

from __future__ import annotations

import re
from typing import Any

from agent_platform.graph.capabilities import AgentCapabilities, NextAction
from agent_platform.logging_config import get_logger
from agent_platform.state.schema import AgentState
from agent_platform.tools.registry import ToolRegistry
from agent_platform.tools.spec import ToolSpec
from marketing.domain import Channel, channel_name
from marketing.service import MarketingService

logger = get_logger(__name__)

AGENT_NAME = "marketing"

# 客户编号（如 C1001）与素材编号（如 mat_xxx）的正则解析
_CUSTOMER_PATTERN = re.compile(r"\bC\d{3,6}\b")
_MATERIAL_PATTERN = re.compile(r"\bmat_[0-9a-zA-Z]+")
_COUNT_PATTERN = re.compile(r"(\d+)\s*(?:条|篇|个)")

# ---- 意图关键词 --------------------------------------------------------- #
# 顺序很重要：越"专"的意图排在越前面，否则会被泛化词吃掉。
_AUDIT_KEYS = (
    "审核", "合规", "审一下", "帮我审", "审查", "过审", "能不能发", "违规", "红线", "检查一下",
)
_ATTRIBUTION_KEYS = ("归因", "效果", "哪条素材", "哪条最好", "表现最好", "线索占比", "转化率", "投放")
_ACTIVATION_KEYS = ("激活", "未成单", "沉睡", "唤醒", "召回", "跟进", "待跟进", "触达")
_CASE_KEYS = ("案例", "证言", "成交故事", "客户反馈", "成功故事")

# 只有出现这些词，才认为用户想"真的发出去"（此时才开放 message 作用域）
_SEND_KEYS = (
    "发出去", "发送", "发布", "推送", "发给", "发给他", "发消息", "群发", "帮他发", "发条",
)

# 渠道识别：口语说法 → 渠道枚举
_CHANNEL_KEYS: tuple[tuple[tuple[str, ...], Channel], ...] = (
    (("抖音", "douyin", "口播", "短视频"), "douyin"),
    (("小红书", "xiaohongshu", "笔记"), "xiaohongshu"),
    (("朋友圈", "moments", "微信", "盆友圈"), "moments"),
)


def _ctx_of(state: AgentState) -> dict[str, Any]:
    return state.get("context") or {}


def _resolve_channel(state: AgentState, service: MarketingService) -> Channel:
    """从 context 或自然语言里解析目标渠道；都取不到就用配置的默认渠道。"""
    ctx = _ctx_of(state)
    explicit = str(ctx.get("channel") or "").strip()
    if explicit:
        return explicit  # type: ignore[return-value]
    text = state.get("user_input", "")
    lowered = text.lower()
    for keywords, channel in _CHANNEL_KEYS:
        if any(k in lowered for k in keywords):
            return channel
    return service.settings.marketing_default_channel  # type: ignore[return-value]


def _resolve_theme(state: AgentState, service: MarketingService, channel: Channel) -> str:
    """解析素材主题。

    优先用调用方显式传入的 theme；否则在模板库里找"同渠道 + 名称/主题出现在
    用户话里"的模板 —— 这样"写条小红书征信解读"能精确落到对应模板上。
    """
    ctx = _ctx_of(state)
    explicit = str(ctx.get("theme") or "").strip()
    if explicit:
        return explicit
    text = state.get("user_input", "")
    for bp in service.blueprints:
        if bp.channel != channel:
            continue
        if bp.theme and bp.theme in text:
            return bp.theme
        if bp.name and bp.name in text:
            return bp.theme
    return ""


def _resolve_count(state: AgentState, default: int = 1) -> int:
    ctx = _ctx_of(state)
    if ctx.get("count"):
        try:
            return max(1, min(int(ctx["count"]), 20))
        except (TypeError, ValueError):
            pass
    found = _COUNT_PATTERN.search(state.get("user_input", ""))
    if found:
        return max(1, min(int(found.group(1)), 20))
    return default


def _resolve_audit_text(state: AgentState) -> str:
    """取出"真正要审的那段文本"。

    为什么不直接把用户原话当待审文本：用户说的是
    「帮我审一下这段：黑户也能做，百分百下款」，如果把整句拿去审，
    审核报告里的证据原文会带上"帮我审一下这段"这类指令，读起来很脏；
    更麻烦的是指令里若恰好含合规词，还会造成误报。
    所以约定：**冒号后是正文**（冒号前是简短指令时），与人的书写习惯一致。
    """
    ctx = _ctx_of(state)
    explicit = str(ctx.get("text") or "").strip()
    if explicit:
        return explicit
    text = state.get("user_input", "").strip()
    for sep in ("：", ":"):
        head, found, tail = text.partition(sep)
        if found and tail.strip() and len(head) <= 30:
            return tail.strip()
    return text


def build_marketing_agent(service: MarketingService, tools: ToolRegistry) -> AgentCapabilities:
    """构造素材推广 Agent 的能力声明。"""

    # ------------------------------------------------------------------ #
    # ① 意图路由（纯规则，确定性、可解释、可回归）
    # ------------------------------------------------------------------ #
    def route(state: AgentState) -> str:
        text = state.get("user_input", "")
        if any(k in text for k in _AUDIT_KEYS):
            return "audit"
        if any(k in text for k in _ATTRIBUTION_KEYS):
            return "attribution"
        if any(k in text for k in _ACTIVATION_KEYS):
            return "activation"
        if any(k in text for k in _CASE_KEYS):
            return "case"
        return "generate"

    # ------------------------------------------------------------------ #
    # ② 工具作用域裁剪（既是省 token，也是安全边界）
    # ------------------------------------------------------------------ #
    def tool_tags_for(state: AgentState) -> list[str]:
        route_name = state.get("route", "generate")
        # 注意：这里**故意不写** "marketing" 这个命名空间标签。
        # 所有 marketing.* 工具都带 "marketing" 标签，一旦把它放进作用域，
        # select() 会把 6 个工具全召回 —— 作用域裁剪看起来还在，实际已经失效
        # （token 一点没省）。所以作用域只用"路由专属标签"，namespace 只作检索用。
        mapping = {
            "generate": ["generate"],          # 只会命中 marketing.generate_materials
            "case": ["case"],                 # 只会命中 marketing.pack_cases
            "activation": ["activation"],     # plan_activation + activate_customer
            "audit": ["audit"],               # marketing.audit_text
            "attribution": ["attribution"],   # marketing.attribute
        }
        tags = list(mapping.get(route_name, ["generate"]))
        text = state.get("user_input", "")
        # 只有"激活"路由 + 用户明确要求发布时，才把对外发送工具暴露出来。
        # 其余情况 Agent 手里根本没有 message 工具，从根上杜绝误发。
        if route_name == "activation" and any(k in text for k in _SEND_KEYS):
            tags.append("message")
        return tags

    # ------------------------------------------------------------------ #
    # ③ 规划
    # ------------------------------------------------------------------ #
    def plan(state: AgentState, scoped: list[ToolSpec]) -> list[str]:
        route_name = state.get("route", "generate")
        if route_name == "audit":
            return ["抽取待审文本", "规则引擎粗筛（承诺性话术/规避审查/绝对化用语）", "输出可否发布的结论"]
        if route_name == "attribution":
            return ["汇总各素材触达数据", "按渠道/素材算线索与成单", "给出加量建议"]
        if route_name == "activation":
            return ["筛选达标沉睡客户（防骚扰）", "按犹豫原因匹配对症素材", "生成建议发送时间", "如需发布则走人工审批"]
        if route_name == "case":
            return ["读取已成交对话", "脱敏提炼证言要素", "包装成渠道素材并过审"]
        return ["解析渠道与主题", "套用高转化结构模板（钩子→痛点→方案→CTA）", "生成并过合规与质量分"]

    # ------------------------------------------------------------------ #
    # ④ 执行决策（确定性：不靠模型决定调用什么工具）
    # ------------------------------------------------------------------ #
    def decide(state: AgentState, scoped: list[ToolSpec]) -> NextAction:
        route_name = state.get("route", "generate")
        text = state.get("user_input", "")
        ctx = _ctx_of(state)
        observations = state.get("observations", [])

        def obs_of(tool: str) -> list[dict[str, Any]]:
            return [o for o in observations if o.get("tool") == tool]

        # ---------------- 素材生成 ----------------
        if route_name == "generate":
            if not obs_of("marketing.generate_materials"):
                channel = _resolve_channel(state, service)
                theme = _resolve_theme(state, service, channel)
                return NextAction(
                    tool="marketing.generate_materials",
                    arguments={"channel": channel, "theme": theme, "count": _resolve_count(state)},
                    thought=f"生成 {channel_name(channel)} 素材（主题：{theme or '自动'}）",
                )
            result = (obs_of("marketing.generate_materials")[-1].get("data") or {})
            # 有素材没过审 → 必须转人工（AI 不改写红线话术，也不放行）
            if result.get("rejected", 0) > 0 and not obs_of("platform.handoff_to_human"):
                return NextAction(
                    tool="platform.handoff_to_human",
                    arguments={
                        "reason": (
                            f"{result.get('rejected')} 条素材命中合规红线未通过，"
                            "需人工修改后再发布（AI 不改写承诺性话术）"
                        ),
                        "urgency": "normal",
                    },
                    thought="存在未过审素材，转人工处理",
                )
            return NextAction(tool=None, thought="素材生成完成")

        # ---------------- 案例包装 ----------------
        if route_name == "case":
            if not obs_of("marketing.pack_cases"):
                return NextAction(
                    tool="marketing.pack_cases",
                    arguments={"channel": _resolve_channel(state, service)},
                    thought="把成交对话包装成证言式素材",
                )
            return NextAction(tool=None)

        # ---------------- 合规审核 ----------------
        if route_name == "audit":
            if not obs_of("marketing.audit_text"):
                return NextAction(
                    tool="marketing.audit_text",
                    arguments={"text": _resolve_audit_text(state)},
                    thought="对文本执行合规审核",
                )
            report = (obs_of("marketing.audit_text")[-1].get("data") or {})
            # 命中 high 级风险 → 转人工复核（AI 只给判定，不放行）
            if report.get("needs_human") and not obs_of("platform.handoff_to_human"):
                return NextAction(
                    tool="platform.handoff_to_human",
                    arguments={
                        "reason": f"文本命中高风险合规项（{report.get('max_severity')}），需人工复核",
                        "urgency": "high",
                    },
                    thought="高风险合规项转人工",
                )
            return NextAction(tool=None)

        # ---------------- 效果归因 ----------------
        if route_name == "attribution":
            if not obs_of("marketing.attribute"):
                material_id = str(ctx.get("material_id") or "").strip()
                if not material_id:
                    found = _MATERIAL_PATTERN.search(text)
                    material_id = found.group(0) if found else ""
                return NextAction(
                    tool="marketing.attribute",
                    arguments={"material_id": material_id},
                    thought="生成素材归因报告",
                )
            return NextAction(tool=None)

        # ---------------- 客户激活 ----------------
        # ① 先生成激活计划（含防骚扰过滤）
        if not obs_of("marketing.plan_activation"):
            limit = 10
            if ctx.get("limit"):
                try:
                    limit = max(1, min(int(ctx["limit"]), 50))
                except (TypeError, ValueError):
                    limit = 10
            return NextAction(
                tool="marketing.plan_activation", arguments={"limit": limit}, thought="生成激活计划"
            )

        # ② 指定了客户 → 取出该客户的触达包
        customer_id = str(ctx.get("customer_id") or "").strip()
        if not customer_id:
            found = _CUSTOMER_PATTERN.search(text)
            customer_id = found.group(0) if found else ""

        if customer_id and not obs_of("marketing.activate_customer"):
            return NextAction(
                tool="marketing.activate_customer",
                arguments={"customer_id": customer_id},
                thought=f"准备 {customer_id} 的触达包",
            )

        # ③ 用户明确要求发布 → 调对外发送（high 风险，平台自动挂起等审批）
        package = (obs_of("marketing.activate_customer")[-1].get("data") or {}) if obs_of(
            "marketing.activate_customer"
        ) else {}
        wants_send = any(k in text for k in _SEND_KEYS)
        if wants_send and package.get("found") and not obs_of("platform.send_external_message"):
            return NextAction(
                tool="platform.send_external_message",
                arguments={
                    "target": package.get("customer_id", ""),
                    # 只发素材正文 + CTA：状态标注、质量分这些内部信息不能发给客户
                    "content": package.get("message_body") or package.get("material_text", ""),
                },
                thought="按业务员指令发送激活素材（需人工审批）",
            )
        return NextAction(tool=None)

    # ------------------------------------------------------------------ #
    # ⑤ 评审（流程完整性检查：该走的步骤一步都不能少）
    # ------------------------------------------------------------------ #
    def critique(state: AgentState) -> dict[str, Any]:
        route_name = state.get("route", "generate")
        observations = state.get("observations", [])

        def ok(tool: str) -> bool:
            return any(o.get("tool") == tool and o.get("ok") for o in observations)

        def data_of(tool: str) -> dict[str, Any]:
            for o in reversed(observations):
                if o.get("tool") == tool:
                    return o.get("data") or {}
            return {}

        if route_name == "generate":
            done = ok("marketing.generate_materials")
            checks: dict[str, Any] = {"generated": done}
            # 有 rejected 素材就必须已经发起转人工（发不发是人的事，但动作不能少）
            rejected = int(data_of("marketing.generate_materials").get("rejected", 0) or 0)
            if rejected:
                checks["handoff_attempted"] = any(
                    o.get("tool") == "platform.handoff_to_human" for o in observations
                )
                passed = done and checks["handoff_attempted"]
            else:
                passed = done
            return {
                "passed": bool(passed),
                "checks": checks,
                "notes": [] if passed else ["存在未过审素材但未转人工，不符合先审后发流程"],
            }

        if route_name == "audit":
            done = ok("marketing.audit_text")
            checks = {"audited": done}
            needs_human = bool(data_of("marketing.audit_text").get("needs_human"))
            if needs_human:
                checks["handoff_attempted"] = any(
                    o.get("tool") == "platform.handoff_to_human" for o in observations
                )
                passed = done and checks["handoff_attempted"]
            else:
                passed = done
            return {
                "passed": bool(passed),
                "checks": checks,
                "notes": [] if passed else ["命中高风险合规项却未转人工复核"],
            }

        if route_name == "activation":
            done = ok("marketing.plan_activation")
            checks = {"planned": done}
            # 用户要求发布却没发起审批动作 → 流程不完整
            wants_send = any(k in state.get("user_input", "") for k in _SEND_KEYS)
            if wants_send:
                checks["package_ready"] = ok("marketing.activate_customer")
                checks["send_attempted"] = any(
                    o.get("tool") == "platform.send_external_message" for o in observations
                )
                # 注意：这里只要求"发起过发送动作"，是否批准由人决定
                passed = done and checks["package_ready"] and checks["send_attempted"]
            else:
                passed = done
            return {
                "passed": bool(passed),
                "checks": checks,
                "notes": [] if passed else ["要求发布但未走人工审批流程"],
            }

        if route_name == "case":
            done = ok("marketing.pack_cases")
            return {"passed": done, "checks": {"packed": done}, "notes": []}

        done = ok("marketing.attribute")
        return {"passed": done, "checks": {"attributed": done}, "notes": []}

    # ------------------------------------------------------------------ #
    # ⑥ 应答
    # ------------------------------------------------------------------ #
    def respond(state: AgentState) -> dict[str, Any]:
        route_name = state.get("route", "generate")
        observations = state.get("observations", [])
        approvals = state.get("approvals") or []

        def data_of(tool: str) -> dict[str, Any]:
            for o in reversed(observations):
                if o.get("tool") == tool:
                    return o.get("data") or {}
            return {}

        if route_name == "generate":
            result = data_of("marketing.generate_materials")
            return {
                "route": "generate",
                "channel": result.get("channel"),
                "channel_name": result.get("channel_name"),
                "count": result.get("count", 0),
                "rejected": result.get("rejected", 0),
                "needs_human": result.get("needs_human", 0),
                "materials": result.get("materials", []),
                "texts": result.get("texts", []),
                "handoff": data_of("platform.handoff_to_human"),
            }

        if route_name == "case":
            result = data_of("marketing.pack_cases")
            return {
                "route": "case",
                "count": result.get("count", 0),
                "cases": result.get("cases", []),
                "materials": result.get("materials", []),
                "texts": result.get("texts", []),
            }

        if route_name == "audit":
            return {
                "route": "audit",
                "report": data_of("marketing.audit_text"),
                "handoff": data_of("platform.handoff_to_human"),
            }

        if route_name == "attribution":
            return {"route": "attribution", "report": data_of("marketing.attribute")}

        # 激活
        plan = data_of("marketing.plan_activation")
        package = data_of("marketing.activate_customer")
        sent = data_of("platform.send_external_message")
        return {
            "route": "activation",
            "plan_id": plan.get("plan_id"),
            "generated": plan.get("generated", 0),
            "skipped": plan.get("skipped", []),
            "tasks": plan.get("tasks", []),
            "plan_text": plan.get("plan_text", ""),
            "package": package,
            # 是否真的发出去了 —— 由人工审批决定；未审批时为 False + 审批单信息
            "send_approvals": approvals,
            "send_result": sent,
            "sent": bool(sent.get("sent")),
        }

    return AgentCapabilities(
        name=AGENT_NAME,
        description=(
            "销售素材推广与客户激活 AI：多渠道素材生成与语感适配 / 成单案例脱敏包装 / "
            "未成单客户对症激活（含防骚扰）/ 合规先审后发 / 素材效果归因"
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
        tags=("marketing", "material", "activation", "compliance", "attribution"),
    )
