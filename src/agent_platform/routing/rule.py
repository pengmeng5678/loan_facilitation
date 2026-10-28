"""第一级：规则路由（零成本基线）。

与旧版"关键词命中即短路返回单个意图"的差别：
    1. 输出**多意图 + 置信度**，不再一个词决定全部；
    2. 处理简单否定（"不违规的文案"不能路由到合规审核）；
    3. 打分为启发式（命中数 + 关键词长度），只用于分层决策，不宣称是概率。
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_platform.routing.base import RouteResult

# 否定字：紧邻关键词前一个字出现时，视为被否定（如"不违规"）
_NEGATION_CHARS = "不别没免勿"


def _is_negated(text: str, index: int) -> bool:
    """只看关键词前**一个**字。

    不能用前两个字："这句话有没有红线"里的"没"是疑问句式（"有没有"），
    不是否定；按两字窗口会把正常的审核请求误判成否定而漏路由。
    """
    if index == 0:
        return False
    return text[index - 1] in _NEGATION_CHARS


def _matches(text: str, keywords: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    lowered = text.lower()
    for kw in keywords:
        start = lowered.find(kw.lower())
        if start == -1:
            continue
        if _is_negated(lowered, start):
            continue
        found.append(kw)
    return found


@dataclass
class RuleRouter:
    """按关键词集合给每个意图打分。

    置信度公式：`0.60 + 0.12 * (命中数 - 1) + 0.01 * 最长命中词长度`，上限 0.95。
    这组系数是**可调基线**，调参依据是评测集（scripts/eval_intent.py），
    不是拍脑袋：单命中约 0.62，三命中约 0.86，天然落到"低置信升级"区间。

    同分排序用业务优先级（priority），不用"关键词出现在句中位置"：
    位置启发式对"这个方案报价多少"会把方案排在报价前面，反而错。
    """

    keywords: dict[str, tuple[str, ...]]
    priority: tuple[str, ...] = ()
    source: str = "rule"

    def _score(self, text: str) -> dict[str, float]:
        scores: dict[str, float] = {}
        for intent, kws in self.keywords.items():
            found = _matches(text, kws)
            if not found:
                continue
            raw = 0.60 + 0.12 * (len(found) - 1) + 0.01 * max(len(k) for k in found)
            scores[intent] = min(0.95, round(raw, 4))
        return scores

    def route(self, text: str, *, allowed: set[str]) -> RouteResult:
        if not text:
            return RouteResult(source=self.source)
        scores = {k: v for k, v in self._score(text).items() if k in allowed}
        if not scores:
            return RouteResult(source=self.source)

        order = {name: i for i, name in enumerate(self.priority)}
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], order.get(kv[0], 999)))
        return RouteResult(intents=tuple(ranked), source=self.source)
