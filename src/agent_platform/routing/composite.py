"""三级路由编排：规则 → 向量 → LLM，并统一澄清策略。

升级判定（为什么不直接全用 LLM）：
    * 规则 top1 >= 澄清阈值且意图之间没有明显冲突 → 直接采纳（0 成本）；
    * 否则向量路由，需过"绝对阈值 + 与次优的差距"双门槛；
    * 仍不成立且配置了 LLM → 交给 LLM 做最后一次判定；
    * 全都不成立 → 回退规则结果，并在低于阈值时请求澄清（不调业务工具）。
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_platform.logging_config import get_logger
from agent_platform.routing.base import ClarifyPolicy, IntentRouter, RouteResult

logger = get_logger(__name__)


@dataclass
class ThreeTierRouter:
    rule: IntentRouter
    embedding: IntentRouter | None = None
    llm: IntentRouter | None = None
    clarify: ClarifyPolicy = ClarifyPolicy()
    rule_margin: float = 0.05
    """规则 top1 与 top2 的最小差距；小于它说明两个意图打平，交给下一级。"""

    def route(self, text: str, *, allowed: set[str]) -> RouteResult:
        rule_result = self.rule.route(text, allowed=allowed)
        if self._rule_is_confident(rule_result):
            return self.clarify.apply(rule_result)

        if self.embedding is not None:
            embed_result = self.embedding.route(text, allowed=allowed)
            if embed_result.intents and embed_result.source == "embedding":
                return self.clarify.apply(embed_result)

        if self.llm is not None:
            llm_result = self.llm.route(text, allowed=allowed)
            if llm_result.intents:
                return self.clarify.apply(llm_result)

        # 全链路都不确定：保留规则打分用于观测，但不选任何业务子流
        fallback = RouteResult(
            intents=rule_result.intents,
            source="fallback",
            need_clarify=True,
            clarify_question=self.clarify.question,
        )
        logger.info("意图路由未达置信门槛，请求澄清：%s", fallback.to_dict())
        return fallback

    def _rule_is_confident(self, result: RouteResult) -> bool:
        if not result.intents:
            return False
        if result.top_confidence < self.clarify.threshold:
            return False
        if len(result.intents) > 1 and (result.top_confidence - result.intents[1][1]) < self.rule_margin:
            return False
        return True
