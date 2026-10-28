"""意图路由（P1-1）测试：多意图、否定陷阱、低置信澄清、分层降级。"""

from __future__ import annotations

import json

from agent_platform.llm.base import EmbeddingProvider, LLMProvider, LLMResponse, Message
from agent_platform.routing import (
    EmbeddingRouter,
    LLMRouter,
    RouteResult,
    RuleRouter,
    ThreeTierRouter,
    loan_intent_keywords,
)
from agent_platform.routing.presets import LOAN_INTENT_PRIORITY


def _rule() -> RuleRouter:
    return RuleRouter(keywords=loan_intent_keywords(), priority=LOAN_INTENT_PRIORITY)


def _allowed() -> set[str]:
    return set(loan_intent_keywords())


def test_multi_intent_returns_both():
    result = _rule().route("我这个情况能贷多少，需要什么材料", allowed=_allowed())
    assert {"match", "qa"}.issubset(set(result.names))
    assert result.top_intent == "match"


def test_negation_not_routed_to_audit():
    result = _rule().route("帮我生成一条不违规的朋友圈文案", allowed=_allowed())
    assert result.names == ["generate"], result.to_dict()


def test_low_confidence_asks_clarification():
    class _Weak:
        def route(self, text: str, *, allowed: set[str]) -> RouteResult:
            return RouteResult(intents=(("audit", 0.55),), source="rule")

    result = ThreeTierRouter(rule=_Weak()).route("随便说点什么", allowed=_allowed())
    assert result.need_clarify is True
    assert result.clarify_question
    assert result.top_intent == "audit"


def test_rule_router_is_case_insensitive():
    result = _rule().route("ROI 是多少", allowed=_allowed())
    assert result.top_intent == "attribution"


class _TableEmbedder(EmbeddingProvider):
    """按文本查表的确定性向量，用于验证阈值/差距判定而不是模型效果。"""

    def __init__(self, table: dict[str, list[float]]) -> None:
        super().__init__(dim=2)
        self.table = table

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.table[t] for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self.table[text]


def test_embedding_router_requires_margin():
    embedder = _TableEmbedder(
        {
            "额度问题": [1.0, 0.0],
            "资料问题": [0.0, 1.0],
            "两个都像": [1.0, 0.9],
        }
    )
    router = EmbeddingRouter(
        embedder=embedder,
        exemplars={"match": ("额度问题",), "qa": ("资料问题",)},
    )
    decisive = router.route("额度问题", allowed={"match", "qa"})
    assert decisive.source == "embedding"
    assert decisive.names == ["match"]

    ambiguous = router.route("两个都像", allowed={"match", "qa"})
    assert ambiguous.source == "embedding_low"
    assert len(ambiguous.intents) == 2


class _JsonLLM(LLMProvider):
    name = "stub"

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def complete(self, messages: list[Message], **kwargs) -> LLMResponse:
        return LLMResponse(content=json.dumps(self.payload, ensure_ascii=False), model="stub")


def test_llm_router_drops_invented_intent():
    router = LLMRouter(
        llm=_JsonLLM(
            {
                "intents": [
                    {"name": "refund", "confidence": 0.99},
                    {"name": "fee", "confidence": 0.81},
                ],
                "need_clarify": False,
            }
        )
    )
    result = router.route("费用多少", allowed={"fee", "qa"})
    assert result.names == ["fee"]
    assert result.source == "llm"


def test_three_tier_falls_back_to_clarify_when_all_weak():
    class _Empty:
        def route(self, text: str, *, allowed: set[str]) -> RouteResult:
            return RouteResult(source="rule")

    router = ThreeTierRouter(rule=_Empty(), embedding=_Empty(), llm=_Empty())
    result = router.route("今天天气怎么样", allowed=_allowed())
    assert result.need_clarify is True
    assert result.source == "fallback"
    assert result.names == []
