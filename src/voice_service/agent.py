"""把项目一（AI 实时语音对话智能客服）装配成平台上的业务 Agent。

五条路由：
    room    开语音接待（开房 + 拉云端 AI 进房）—— **高风险，走人工审批**
    ask     文本模拟一轮（默认）：走真实 RAG + 提示词 + 内容安全
    audit   出口内容安全自查（业务员想知道"这句话能不能跟客户说"）
    eval    Golden Set 评测 / 提示词版本准入
    stats   语音链路指标（首字延迟 p50/p95、拒答率、转人工率）

为什么 `voice.start_session` 是 high 风险：
    它同时触发两件"不该让 AI 自己决定"的事 —— **真的打电话给客户**、**真的产生费用**。
    平台会因此自动插入人工审批检查点（interrupt），超时只能 abort/escalate，
    绝不会自动放行。这和项目七的"对外发消息必须有人点头"是同一条治理原则：
    **不可逆的对外动作，永远由人按下最后那一下。**
"""

from __future__ import annotations

import re
from typing import Any

from agent_platform.graph.capabilities import AgentCapabilities, NextAction
from agent_platform.logging_config import get_logger
from agent_platform.state.schema import AgentState
from agent_platform.tools.registry import ToolRegistry
from agent_platform.tools.spec import ToolSpec
from voice_service.service import VoiceService

logger = get_logger(__name__)

AGENT_NAME = "voice_cs"

_SESSION_PATTERN = re.compile(r"\bvs_[0-9a-zA-Z]+\b")

# 意图词表：越"专"的越靠前，否则会被泛化词吃掉（项目七踩过的坑）
_AUDIT_KEYS = ("审核", "合规", "能不能说", "会不会违规", "过一遍", "审一下", "红线", "能不能这么讲")
_EVAL_KEYS = ("评测", "基线", "准入", "golden", "回归", "提示词版本", "跑一遍评测")
_STATS_KEYS = ("指标", "延迟", "首字", "p95", "p50", "拒答率", "转人工率", "快不快")
_ROOM_KEYS = ("开房", "开一场", "语音接待", "打电话", "客户来电", "拉进房间", "接起来", "接通")

# 场景识别
_SCENE_KEYS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("额度", "能贷多少", "能批多少", "amount"), "amount"),
    (("资料", "材料", "准备什么", "docs"), "docs"),
    (("接待", "顾问", "咨询", "consultant"), "consultant"),
)


def _ctx_of(state: AgentState) -> dict[str, Any]:
    return state.get("context") or {}


def _resolve_scene(state: AgentState, service: VoiceService) -> str:
    ctx = _ctx_of(state)
    explicit = str(ctx.get("scene_id") or "").strip()
    if explicit and service.scene_of(explicit):
        return explicit
    text = state.get("user_input", "")
    for keys, scene_id in _SCENE_KEYS:
        if any(k in text for k in keys):
            return scene_id
    return "consultant"


def _resolve_audit_text(state: AgentState) -> str:
    """取出"真正要检查的那段话"。

    指令和正文经常写在一起（"帮我看看这句能不能说：保证下款"），
    如果整句直接送检，命中的往往是"能不能说"这类指令词，报告会失真。
    规则：冒号（中英文）后面是正文；冒号前超过 30 字的当正文本身。
    """
    text = (state.get("user_input") or "").strip()
    for sep in ("：", ":"):
        if sep in text:
            head, tail = text.split(sep, 1)
            if len(head) <= 30 and tail.strip():
                return tail.strip()
    return text


def build_voice_agent(service: VoiceService, tools: ToolRegistry) -> AgentCapabilities:
    """构建语音客服 Agent。"""

    # ------------------------------------------------------------------ #
    # ① 意图路由
    # ------------------------------------------------------------------ #
    def route(state: AgentState) -> str:
        text = state.get("user_input", "")
        lowered = text.lower()
        if any(k in text for k in _AUDIT_KEYS):
            return "audit"
        if any(k in lowered for k in _EVAL_KEYS):
            return "eval"
        if any(k in text for k in _STATS_KEYS):
            return "stats"
        if any(k in text for k in _ROOM_KEYS):
            return "room"
        return "ask"

    # ------------------------------------------------------------------ #
    # ② 作用域裁剪
    # ------------------------------------------------------------------ #
    def tool_tags_for(state: AgentState) -> list[str]:
        """按意图只注入相关工具。

        注意：**不要把 "voice" 这个命名空间标签放进返回值** ——
        所有语音工具的 tags 里都带 "voice"，一旦返回它就会召回全部工具，
        裁剪静默失效（项目七踩过这个坑，这里写下来避免重犯）。
        """
        mapping = {
            "room": ["room"],
            "ask": ["dialog"],
            "audit": ["audit"],
            "eval": ["eval"],
            "stats": ["observe"],
        }
        return mapping.get(state.get("route", "ask"), ["dialog"])

    # ------------------------------------------------------------------ #
    # ③ 规划
    # ------------------------------------------------------------------ #
    def plan(state: AgentState, scoped: list[ToolSpec]) -> list[str]:
        """把本轮任务拆成可读的步骤描述（给人看的，不影响执行）。"""
        route_name = state.get("route", "ask")
        return {
            "room": ["签发进房凭证", "调用 StartVoiceChat 把 AI 拉进房间", "返回房间与配置"],
            "ask": ["检索知识库", "组装提示词", "生成回答并过内容安全"],
            "audit": ["逐条规则检查", "命中红线则给出兜底话术"],
            "eval": ["跑 Golden Set", "与基线比对得出准入结论"],
            "stats": ["汇总首字延迟与业务指标"],
        }.get(route_name, [])

    # ------------------------------------------------------------------ #
    # ④ 执行决策（确定性）
    # ------------------------------------------------------------------ #
    def decide(state: AgentState, scoped: list[ToolSpec]) -> NextAction:
        route_name = state.get("route", "ask")
        observations = state.get("observations", [])

        def obs_of(tool: str) -> list[dict[str, Any]]:
            return [o for o in observations if o.get("tool") == tool]

        if route_name == "room":
            if not obs_of("voice.start_session"):
                return NextAction(
                    tool="voice.start_session",
                    arguments={
                        "scene_id": _resolve_scene(state, service),
                        "user_id": str(_ctx_of(state).get("user_id") or ""),
                    },
                    thought="开语音接待（真实开房会产生费用，需人工审批）",
                )
            return NextAction(tool=None, thought="会话已建立")

        if route_name == "audit":
            if not obs_of("voice.audit"):
                return NextAction(
                    tool="voice.audit",
                    arguments={"text": _resolve_audit_text(state)},
                    thought="检查这段话说出去会不会踩红线",
                )
            report = obs_of("voice.audit")[-1].get("data") or {}
            if report.get("needs_human") and not obs_of("platform.handoff_to_human"):
                return NextAction(
                    tool="platform.handoff_to_human",
                    arguments={
                        "reason": f"语音话术命中高风险项（{report.get('max_severity')}），需人工复核后再使用",
                        "urgency": "high",
                    },
                    thought="高风险话术转人工",
                )
            return NextAction(tool=None)

        if route_name == "eval":
            if not obs_of("voice.eval_prompt"):
                ctx = _ctx_of(state)
                return NextAction(
                    tool="voice.eval_prompt",
                    arguments={
                        "prompt_key": str(ctx.get("prompt_key") or "consultant"),
                        "version": str(ctx.get("version") or ""),
                    },
                    thought="跑语音 Golden Set 评测",
                )
            return NextAction(tool=None)

        if route_name == "stats":
            if not obs_of("voice.metrics"):
                return NextAction(tool="voice.metrics", arguments={}, thought="汇总语音链路指标")
            return NextAction(tool=None)

        # ask：一问一答
        if not obs_of("voice.ask"):
            return NextAction(
                tool="voice.ask",
                arguments={
                    "question": state.get("user_input", ""),
                    "scene_id": _resolve_scene(state, service),
                },
                thought="走真实语音对话链路（RAG + 提示词 + 内容安全）",
            )
        return NextAction(tool=None)

    # ------------------------------------------------------------------ #
    # ⑤ 评审（流程完整性）
    # ------------------------------------------------------------------ #
    def critique(state: AgentState) -> dict[str, Any]:
        route_name = state.get("route", "ask")
        observations = state.get("observations", [])

        def ok(tool: str) -> bool:
            return any(o.get("tool") == tool and o.get("ok") for o in observations)

        def data_of(tool: str) -> dict[str, Any]:
            for o in reversed(observations):
                if o.get("tool") == tool:
                    return o.get("data") or {}
            return {}

        if route_name == "room":
            started = ok("voice.start_session")
            rtc_ok = bool(data_of("voice.start_session").get("rtc", {}).get("ok"))
            return {
                "passed": started and rtc_ok,
                "checks": {"session_created": started, "rtc_started": rtc_ok},
                "notes": [] if (started and rtc_ok) else ["会话未成功建立或 RTC 未返回成功"],
            }

        if route_name == "audit":
            done = ok("voice.audit")
            needs_human = bool(data_of("voice.audit").get("needs_human"))
            handoff = any(o.get("tool") == "platform.handoff_to_human" for o in observations)
            passed = done and (handoff if needs_human else True)
            return {
                "passed": passed,
                "checks": {"audited": done, "handoff_attempted": handoff},
                "notes": [] if passed else ["命中高风险却未转人工复核"],
            }

        if route_name == "ask":
            done = ok("voice.ask")
            report = data_of("voice.ask")
            return {
                "passed": done,
                "checks": {"answered": done},
                "notes": [] if done else ["未产出回答"],
                # 拒答不算失败 —— 没有依据时拒答才是正确行为
                "refused": bool(report.get("refused")),
            }

        return {"passed": True, "checks": {}, "notes": []}

    # ------------------------------------------------------------------ #
    # ⑥ 应答
    # ------------------------------------------------------------------ #
    def respond(state: AgentState) -> dict[str, Any]:
        route_name = state.get("route", "ask")
        observations = state.get("observations", [])
        approvals = state.get("approvals") or []

        def data_of(tool: str) -> dict[str, Any]:
            for o in reversed(observations):
                if o.get("tool") == tool:
                    return o.get("data") or {}
            return {}

        if route_name == "room":
            started = data_of("voice.start_session")
            session = started.get("session") or {}
            return {
                "route": "room",
                "session_id": session.get("session_id"),
                "room_id": session.get("room_id"),
                "scene_id": session.get("scene_id"),
                "join": started.get("join"),
                "rtc": started.get("rtc"),
                "approvals": approvals,
                "note": "开房属于高风险动作：未获人工审批时不会真正下发 StartVoiceChat",
            }

        if route_name == "audit":
            return {
                "route": "audit",
                "report": data_of("voice.audit"),
                "handoff": data_of("platform.handoff_to_human"),
            }

        if route_name == "eval":
            return {"route": "eval", "report": data_of("voice.eval_prompt")}

        if route_name == "stats":
            return {"route": "stats", "metrics": data_of("voice.metrics")}

        return {"route": "ask", "turn": data_of("voice.ask")}

    return AgentCapabilities(
        name=AGENT_NAME,
        description=(
            "AI 实时语音对话智能客服：火山 RTC + 双向流 ASR + TTS 音色复刻 + 第三方 LLM 回调，"
            "接助贷知识库做实时问答；开口前过内容安全，无依据即转人工。"
        ),
        tools=tools,
        route=route,
        tool_tags_for=tool_tags_for,
        plan=plan,
        decide=decide,
        critique=critique,
        max_critique_rounds=2,
        respond=respond,
        version="1.0.0",
        tags=("voice", "customer_service"),
    )
