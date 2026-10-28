"""路由结果与路由器的公共契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class RouteResult:
    """一次意图路由的结论。

    intents 按置信度降序排列，保留**多意图**（如"额度多少 + 需要什么材料"）。
    置信度是各实现自己的启发式数值，只用于分层决策与观测，不是概率承诺。
    """

    intents: tuple[tuple[str, float], ...] = ()
    source: str = "rule"
    """rule | embedding | llm | fallback"""
    need_clarify: bool = False
    clarify_question: str = ""

    @property
    def top_intent(self) -> str:
        return self.intents[0][0] if self.intents else ""

    @property
    def top_confidence(self) -> float:
        return self.intents[0][1] if self.intents else 0.0

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.intents]

    def to_dict(self) -> dict[str, object]:
        return {
            "intents": [[name, round(score, 4)] for name, score in self.intents],
            "source": self.source,
            "need_clarify": self.need_clarify,
            "clarify_question": self.clarify_question,
        }


class IntentRouter(Protocol):
    """路由实现的最小契约（三种实现可互换）。"""

    def route(self, text: str, *, allowed: set[str]) -> RouteResult: ...


@dataclass(frozen=True)
class ClarifyPolicy:
    """低置信度时的澄清策略。

    宁可问一句，也不要猜错意图后调错工具 —— 助贷场景里"调错工具"的代价
    远高于"多问一句"（例如把"查进度"识别成"改方案"）。
    """

    threshold: float = 0.6
    question: str = "我需要先确认一下：您是想了解产品额度/资料，还是想查询办理进度？"
    fallback_intent: str = ""
    """澄清时的临时路由（通常留空，表示不进入任何业务子流）。"""

    def apply(self, result: RouteResult) -> RouteResult:
        if result.intents and result.top_confidence >= self.threshold:
            return result
        return RouteResult(
            intents=result.intents,
            source=result.source,
            need_clarify=True,
            clarify_question=self.question,
        )


@dataclass(frozen=True)
class IntentConfig:
    """意图关键词/示例句的配置载体（便于按业务线替换）。"""

    keywords: dict[str, tuple[str, ...]] = field(default_factory=dict)
    exemplars: dict[str, tuple[str, ...]] = field(default_factory=dict)
    priority: tuple[str, ...] = ()
