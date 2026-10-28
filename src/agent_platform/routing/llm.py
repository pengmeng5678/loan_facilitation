"""第三级：LLM 路由（只在低置信/多意图冲突时调用）。

两个约束：
    1. 意图枚举由业务注入，模型**不能发明新意图**（schema enum + 二次校验）；
    2. 输出必须结构化 JSON，解析失败即降级，不让路由成为新的单点故障。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_platform.llm.base import LLMProvider, Message
from agent_platform.logging_config import get_logger
from agent_platform.routing.base import RouteResult

logger = get_logger(__name__)

_SYSTEM = """你是助贷业务对话的意图分类器。
只能从给定意图中选择，不要发明新意图；可以返回多个意图（按置信度降序）。
只输出 JSON，不要输出解释。"""


def _schema_for(allowed: set[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "intents": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "enum": sorted(allowed)},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                    "required": ["name", "confidence"],
                },
            },
            "need_clarify": {"type": "boolean"},
        },
        "required": ["intents", "need_clarify"],
    }


@dataclass
class LLMRouter:
    """结构化输出的 LLM 意图路由。"""

    llm: LLMProvider
    temperature: float = 0.0
    source: str = "llm"

    def route(self, text: str, *, allowed: set[str]) -> RouteResult:
        if not text or not allowed:
            return RouteResult(source=self.source)

        messages = [
            Message(role="system", content=_SYSTEM),
            Message(role="user", content=f"可选意图：{sorted(allowed)}\n用户输入：{text}"),
        ]
        try:
            payload = self.llm.complete_json(
                messages, _schema_for(allowed), temperature=self.temperature
            )
        except Exception as exc:  # noqa: BLE001 - 路由失败必须降级而不是中断对话
            logger.warning("LLM 意图路由失败，降级：%s", exc)
            return RouteResult(source=f"{self.source}_failed")

        intents: list[tuple[str, float]] = []
        for item in payload.get("intents") or []:
            name = str((item or {}).get("name") or "")
            if name not in allowed:  # 防"模型发明意图"
                continue
            try:
                confidence = float((item or {}).get("confidence") or 0.0)
            except (TypeError, ValueError):
                continue
            intents.append((name, max(0.0, min(1.0, confidence))))

        intents.sort(key=lambda kv: -kv[1])
        return RouteResult(
            intents=tuple((name, round(score, 4)) for name, score in intents),
            source=self.source,
            need_clarify=bool(payload.get("need_clarify")),
        )
