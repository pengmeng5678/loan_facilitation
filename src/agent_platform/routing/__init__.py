"""意图路由（Intent Routing）：规则基线 → 向量 → LLM 兜底的三级路由。

为什么需要单独一层：
    关键词路由在演示数据上"看起来很准"，但真实会话里换一种说法就漏；
    而直接上 LLM 路由又会给每一通对话加一次模型调用（延迟与成本都不可接受）。
    生产做法是**分级**：规则先跑（0 成本），置信度不足才升级，
    并给"确实拿不准"的输入一个明确的澄清出口，而不是硬猜一个意图。

约定：
    * 路由只返回意图与置信度，**不执行业务逻辑**；
    * 业务 Agent 把允许的意图集合注入进来，模型不能发明新意图；
    * 任何一级都拿不准时返回 need_clarify=True，由上层回澄清话术、不调工具。
"""

from agent_platform.routing.base import IntentRouter, RouteResult
from agent_platform.routing.composite import ThreeTierRouter
from agent_platform.routing.embedding import EmbeddingRouter
from agent_platform.routing.llm import LLMRouter
from agent_platform.routing.presets import loan_intent_exemplars, loan_intent_keywords
from agent_platform.routing.rule import RuleRouter

__all__ = [
    "IntentRouter",
    "RouteResult",
    "RuleRouter",
    "EmbeddingRouter",
    "LLMRouter",
    "ThreeTierRouter",
    "loan_intent_keywords",
    "loan_intent_exemplars",
]
