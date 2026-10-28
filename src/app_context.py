"""应用装配上下文（Composition Root）。

**这是整个仓库最应该看懂的一个文件**：它把"平台"和"业务"装配在一起。
顺序体现了依赖方向 —— 业务依赖平台，平台不知道业务存在：

    ① 建平台运行时（LLM / 向量 / Checkpointer / 工具 / 治理 / 观测）
    ② 建业务服务（产品助手、话术陪练、素材推广），各服务只拿到自己需要的依赖
    ③ 把业务能力注册成平台的"工具"
    ④ 把业务 Agent 的能力声明注册进平台 → 平台装配出可运行的编排图
    ⑤ API 层通过 get_app_context() 拿到这一切

新增一个业务 Agent 只需要在这里加三行（建服务、注册工具、注册 Agent），
不需要改动平台任何代码 —— 这就是"新 Agent 接入周期从 60 人日降到 30 人日"的来源。

另外两个项目的装配位置很能说明它们的定位：
  · **项目六（推理网关）在建运行时之前装配** —— 因为它就是"模型访问的统一入口"，
    打开开关后它会作为 LLMProvider 注入运行时，所有业务的模型调用都从它过。
  · **项目七（素材推广）在建运行时之后装配** —— 它是一个普通业务 Agent，
    和其他业务一样注册工具、注册 Agent，走同一套编排与 HITL。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from agent_platform.config import Settings, get_settings
from agent_platform.logging_config import configure_logging, get_logger
from agent_platform.runtime import AgentRuntime
from coaching.agent import build_coaching_agent
from coaching.service import CoachingService
from data_platform.service import DataPlatformService
from flow_ops.agent import build_flow_ops_agent
from flow_ops.scheduler import SopScheduler, build_scheduler
from flow_ops.service import FlowOpsService
from gateway.agent import build_gateway_agent
from gateway.evaluation.runner import EvalTarget
from gateway.service import GatewayService
from marketing.agent import build_marketing_agent
from marketing.service import MarketingService
from product_assistant.agent import build_product_assistant_agent
from product_assistant.service import ProductAssistantService
from voice_service.agent import build_voice_agent
from voice_service.service import VoiceService

logger = get_logger(__name__)


@dataclass
class AppContext:
    """全局装配上下文。"""

    settings: Settings
    runtime: AgentRuntime
    data_platform: DataPlatformService
    product: ProductAssistantService
    coaching: CoachingService
    marketing: MarketingService
    gateway_service: GatewayService
    voice: VoiceService
    flow_ops: FlowOpsService
    #: SOP 定时调度器（未启用时为 None）。在 lifespan 里启停 ——
    #: create_task 需要运行中的事件循环，装配阶段（同步）还没有。
    flow_scheduler: SopScheduler | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "agents": self.runtime.registry.names(),
            "tools": len(self.runtime.tools.all()),
            "llm_provider": self.runtime.llm.name,
            "embedding_provider": self.runtime.embedder.name,
            "checkpointer": type(self.runtime.checkpointer).__name__,
            # 数据中台只带关键计数（/ready 是探针，不塞整份统计）
            "data": {
                key: value
                for key, value in self.data_platform.stats().items()
                if key in ("raw_messages", "blocks", "staging", "approved_eligible", "chunks", "articles")
            },
            "product": self.product.stats(),
            "coaching": self.coaching.stats(),
            "marketing": self.marketing.stats(),
            "gateway": self.gateway_service.stats(),
            "voice": self.voice.stats(),
            "flow_ops": self.flow_ops.stats(),
            "flow_scheduler": (
                None if self.flow_scheduler is None
                else {
                    "enabled": True,
                    "running": self.flow_scheduler.running,
                    "interval_seconds": self.flow_scheduler.interval_seconds,
                }
            ),
        }


# =========================================================================== #
# 评测目标（项目六的核心设计点：网关**不 import 任何业务代码**）
# =========================================================================== #
def build_eval_targets(
    product: ProductAssistantService,
    marketing: MarketingService,
    gateway_service: GatewayService,
) -> dict[str, EvalTarget]:
    """把"评测场景 → 调用哪个业务能力"的映射放在装配层。

    为什么不在网关里写死"policy_qa 就去调产品助手"：
        那会让基础设施依赖业务，业务一改网关就得跟着发版。
        写成"装配层注入 callable"之后，网关只知道自己要跑一个函数，
        被测系统是什么、有几套，它完全不关心 —— 于是同一套评测框架
        可以同时评测产品助手、素材生成、甚至路由策略本身。
    """

    # ---- 政策问答 / 拒答 / 合规：本质都是"产品问答"，只是判分维度不同 ----
    def _qa(case: Any) -> dict[str, Any]:
        result = product.answer_question(case.question)
        return {
            "answer": result.answer,
            "refused": result.refused,
            "extra": {"citations": result.citations, "reason": result.reason},
        }

    # ---- 路由：断言"该省的省"，防止有人改阈值把成本打回原形 ----
    def _routing(case: Any) -> dict[str, Any]:
        task = str((case.context or {}).get("task") or "")
        decision = gateway_service.gateway.route_preview(case.question, task=task)
        return {
            "answer": f"路由档位 {decision.tier}（{decision.model}），复杂度 {decision.complexity:.2f}",
            "refused": False,
            "extra": {"tier": decision.tier, "complexity": decision.complexity},
        }

    # ---- 素材：直接跑真实生成链路（含合规与质量分），断言产出里结构完整 ----
    def _material(case: Any) -> dict[str, Any]:
        ctx = case.context or {}
        channel = str(ctx.get("channel") or "moments")
        theme = str(ctx.get("theme") or "")
        # persist=False：评测是"试跑"，不应该把评测产物灌进素材库
        drafts = marketing.generate_materials(channel, theme=theme, count=1, persist=False)
        if not drafts:
            return {"answer": "", "refused": True, "extra": {"note": "素材库为空"}}
        draft = drafts[0]
        return {
            "answer": draft.to_text(),
            "refused": False,
            "extra": {
                "channel": channel,
                "status": draft.status,
                "quality_score": draft.quality_score,
            },
        }

    return {
        "policy_qa": _qa,
        "refusal": _qa,
        "compliance": _qa,
        "routing": _routing,
        "material": _material,
    }


def build_app_context(
    settings: Settings | None = None,
    *,
    checkpointer: Any | None = None,
) -> AppContext:
    """构建完整应用上下文（可注入 checkpointer 便于测试）。"""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    # ① 项目六：推理网关与可观测评测平台
    #    先建它，是因为它要作为"模型访问的统一入口"注入运行时。
    #    评测目标由下面的装配层注入（网关本身不认识任何业务代码）。
    gateway_service = GatewayService(settings=settings)
    logger.info(
        "推理网关就绪：impl=%s 缓存阈值=%.2f 配额=%d tokens",
        gateway_service.gateway.impl_name,
        settings.gateway_semantic_cache_threshold,
        settings.gateway_quota_tokens_per_scope,
    )

    # ② 平台运行时
    #    开启 gateway_enabled 后，网关会作为 LLMProvider 注入：
    #    于是产品问答、话术陪练、素材生成的所有模型调用都会经过
    #    复杂度路由 / 缓存 / 限流 / 密钥轮转 / 成本记账。
    #    默认关闭，是为了让本地与测试在零配置下行为和以前完全一致。
    runtime = AgentRuntime(
        settings=settings,
        llm=gateway_service.gateway if settings.gateway_enabled else None,
        checkpointer=checkpointer,
    )
    logger.info("平台运行时就绪：LLM=%s Embedding=%s", runtime.llm.name, runtime.embedder.name)

    # ③ 项目四：助贷对话数据中台
    #    放在最前面，因为它**是其他业务的数据源头**：
    #      · 客户姓名词典 → 脱敏（从项目二档案读，只读不依赖）
    #      · 已批准对话   → 项目七的案例语料（见下面的 conversation_provider）
    #    它没有独立 Agent（不面向客户对话），只向平台注册 data.* 工具。
    data_platform = DataPlatformService(
        settings=settings, llm=runtime.llm, embedder=runtime.embedder
    )
    data_platform.bootstrap()

    # ④ 项目二：产品知识助手
    product = ProductAssistantService(settings=settings, llm=runtime.llm, embedder=runtime.embedder)
    product.bootstrap()

    # ⑤ 项目三：话术陪练与 AI 考核
    #    profile_provider 注入项目二的客户档案仓储 —— 两个业务服务通过"数据"咬合，
    #    而不是互相 import 业务代码（服务之间保持解耦）。
    coaching = CoachingService(
        settings=settings, llm=runtime.llm, profile_provider=product.profiles.get
    )
    coaching.bootstrap()

    # ⑥ 项目七：销售素材推广与客户激活
    #    同样只通过"数据"与别的服务咬合：
    #      name_resolver         ← 客户档案（案例脱敏后的称呼）
    #      conversation_provider ← 优先取数据中台「已审核通过」的对话；
    #                              中台还没数据时回退到项目三语料。
    #    这就是"中台是唯一可信数据源"的落地点：没被审核的数据进不了素材。
    marketing = MarketingService(
        settings=settings,
        llm=runtime.llm,
        name_resolver=_make_name_resolver(product),
        conversation_provider=_make_conversation_provider(settings, coaching, data_platform),
    )
    marketing.bootstrap()

    # ⑦ 项目一：AI 实时语音对话智能客服（火山 RTC + ASR + TTS + 第三方 LLM 回调）
    #    只通过"检索函数"与项目二咬合：语音模块不认识 product_assistant，
    #    它只知道"给我一个能按问题检索政策条款的函数" —— 于是它也能单独部署成
    #    RTC 回调服务，不受其他业务发版影响。
    voice = VoiceService(
        settings=settings,
        llm=runtime.llm,
        retrieve_fn=product.retriever.retrieve,
    )
    voice.bootstrap()

    # ⑧ 项目八：助贷业务流程数字员工
    #    两个 provider 把它和别的服务接起来（同样只走"数据"，不 import 业务代码）：
    #      plan_provider    ← 项目二：到店前的方案初稿（"怎么配方案"归项目二）
    #      profile_provider ← 项目二客户档案：资质/征信/需求（到店前一页纸的原料）
    flow_ops = FlowOpsService(
        settings=settings,
        llm=runtime.llm,
        plan_provider=_make_plan_provider(product),
        profile_provider=_make_profile_provider(product),
    )
    flow_ops.bootstrap()

    # ⑨ 组装评测目标（项目六 ← 项目二/项目七），注入网关服务
    #    项目一（语音）不挂在网关评测里：它有自己更贴业务的 Golden Set
    #    （合规红线 + 拒答正确性 + 语速长度），见 voice_service/evaluation.py。
    #    项目八（流程）同样不挂：它评的是流程行为（SOP/幂等/HITL/口径），
    #    见 flow_ops/evaluation.py。
    gateway_service.targets = build_eval_targets(product, marketing, gateway_service)

    # ⑩ 注册工具到平台
    data_platform.register_tools(runtime.tools)
    product.register_tools(runtime.tools)
    coaching.register_tools(runtime.tools)
    marketing.register_tools(runtime.tools)
    voice.register_tools(runtime.tools)
    gateway_service.register_tools(runtime.tools)
    flow_ops.register_tools(runtime.tools)

    # ⑪ 注册业务 Agent（平台负责装配编排图）
    #    注意：数据中台不注册 Agent —— 它是数据层，不面向客户对话，
    #    只以工具形式（data.*）参与编排。避免"为了凑数硬造 Agent"。
    runtime.register(build_product_assistant_agent(product, runtime.tools))
    runtime.register(build_coaching_agent(coaching, runtime.tools))
    runtime.register(build_marketing_agent(marketing, runtime.tools))
    runtime.register(build_voice_agent(voice, runtime.tools))
    runtime.register(build_gateway_agent(gateway_service, runtime.tools))
    runtime.register(build_flow_ops_agent(flow_ops, runtime.tools))

    ctx = AppContext(
        settings=settings,
        runtime=runtime,
        data_platform=data_platform,
        product=product,
        coaching=coaching,
        marketing=marketing,
        gateway_service=gateway_service,
        voice=voice,
        flow_ops=flow_ops,
        # SOP 调度器：默认关闭（FLOW_SCHEDULER_ENABLED），生产/compose 打开。
        # 只创建不启动 —— start() 要事件循环，归 FastAPI lifespan 管。
        flow_scheduler=build_scheduler(settings, flow_ops, runtime.approvals),
    )
    logger.info("应用装配完成：%s", ctx.summary())
    return ctx


def _make_plan_provider(product: ProductAssistantService):
    """客户编号 → 方案初稿摘要（转调项目二）。

    项目八与项目二的边界（面试必答）：
        项目二负责"怎么配方案"，项目八负责"在对的时机把它准备好"。
    所以这里是调用关系：本项目 import 的只是装配层的一个函数，
    product_assistant 完全不知道有项目八存在。
    """

    def _plan(customer_id: str) -> dict:
        try:
            match = product.match_customer(customer_id)
        except Exception as exc:  # noqa: BLE001 - 项目二不可用时降级为人工匹配
            logger.warning("方案匹配失败（转人工）：%s: %s", customer_id, exc)
            return {"handoff_reason": f"方案匹配服务异常：{exc}"}
        if not match.matched or match.primary is None:
            return {"handoff_reason": match.handoff_reason or "无匹配产品，建议转人工专家"}
        primary = match.primary
        rejected = [
            f"{item.get('product_name', '')}：{item.get('reason', '')}"
            for item in match.rejected_summary[:5]
        ]
        return {
            "product_code": getattr(primary, "product_code", ""),
            "product_name": primary.product_name,
            "summary": match.to_text(),
            "citations": [],
            "rejected": rejected,
            "handoff_reason": "",
        }

    return _plan


def _make_profile_provider(product: ProductAssistantService):
    """客户编号 → ProfileSummary（项目八的档案模型）。

    字段映射在 `ProfileSummary.from_customer_profile` 里单点维护，
    装配层与种子脚本共用同一份口径。
    """
    from flow_ops.domain import ProfileSummary

    def _profile(customer_id: str) -> ProfileSummary | None:
        p = product.profiles.get(customer_id)
        if p is None:
            return None
        return ProfileSummary.from_customer_profile(p)

    return _profile


def _make_conversation_provider(
    settings: Settings,
    coaching: CoachingService,
    data_platform: DataPlatformService,
):
    """项目七的案例语料源：**优先用数据中台已审核的对话**。

    为什么不是直接 `lambda: coaching.conversations`：
        项目三的语料是演示文件，谁都能改；而"成单案例"是要对外发布的素材，
        必须来自经过人工审核的唯一可信源（中台 approved）。
        中台还没有已审数据时回退到旧语料，保证演示开箱可用。
    """

    def _conversations() -> list[dict]:
        if settings.data_feed_enabled:
            try:
                fed = data_platform.feed_conversations()
                if fed:
                    logger.info("素材语料：使用数据中台已审核对话 %d 条", len(fed))
                    return fed
            except Exception as exc:  # noqa: BLE001 - 中台不可用不阻塞素材生成
                logger.warning("读取数据中台语料失败，回退项目三语料：%s", exc)
        return coaching.conversations

    return _conversations


def _make_name_resolver(product: ProductAssistantService):
    """客户编号 → 脱敏称呼。

    素材里出现真名是隐私事故，所以这里取档案里已经脱敏的字段；
    CaseBuilder 还会再走一遍 mask_name 兜底（不依赖词表）。
    """

    def _resolve(customer_id: str) -> str:
        profile = product.profiles.get(customer_id)
        if profile is None:
            return ""
        return getattr(profile, "name_masked", "") or getattr(profile, "name", "") or ""

    return _resolve


@lru_cache(maxsize=1)
def get_app_context() -> AppContext:
    """进程内单例（FastAPI 依赖注入用）。测试中可调用 cache_clear() 重置。"""
    return build_app_context()
