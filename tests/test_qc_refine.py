"""QC 第二段（LLM 精判）回归测试。

历史 bug：模型按 keyword 回传、代码按 excerpt 查表，两边 key 空间不一致，
`is_violation` 永远走默认 True —— 精判等于没做。这里锁定 finding_id 契约。
"""

from __future__ import annotations

from typing import Any

from agent_platform.llm.base import LLMProvider, LLMResponse, Message
from coaching.qc import QCEngine


class FakeLLM(LLMProvider):
    name = "fake"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[list[Message]] = []

    def complete(self, messages: list[Message], **_: Any) -> LLMResponse:  # pragma: no cover
        raise NotImplementedError

    def complete_json(self, messages: list[Message], schema: dict, **_: Any) -> dict[str, Any]:
        self.calls.append(messages)
        return self.payload


_CONV = {
    "conversation_id": "T-QC",
    "agent": "业务员甲",
    "outcome": "lost",
    "messages": [
        {"role": "business", "text": "我们包过，保证下款。", "ts": 0},
        {"role": "customer", "text": "真的吗？", "ts": 10},
    ],
}


def _violation(report) -> list:
    return [f for f in report.findings if f.rule_id == "QC-001"]


def test_refine_downgrades_false_positive():
    engine = QCEngine()
    # 先跑一次规则，拿到真实 finding_id
    base = engine.check(_CONV)
    targets = _violation(base)
    assert targets and targets[0].finding_id

    fake = FakeLLM({
        "results": [
            {"finding_id": targets[0].finding_id, "is_violation": False, "reason": "仅描述流程"},
        ]
    })
    engine2 = QCEngine(llm=fake)
    report = engine2.check(_CONV)

    downgraded = {f.finding_id: f for f in _violation(report)}
    assert downgraded[targets[0].finding_id].severity == "low"
    assert report.score > base.score
    # prompt 里必须带 id，否则模型无法原样回传
    assert targets[0].finding_id in fake.calls[0][-1].content


def test_refine_unmatched_keeps_original():
    fake = FakeLLM({"results": [{"finding_id": "does-not-exist", "is_violation": False}]})
    engine = QCEngine(llm=fake)
    report = engine.check(_CONV)

    assert all(f.severity == "high" for f in _violation(report))
    assert engine.metrics.counters().get("qc_refine_unmatched_total") == 1.0


def test_refine_missing_verdict_keeps_original():
    fake = FakeLLM({"results": []})
    engine = QCEngine(llm=fake)
    report = engine.check(_CONV)

    assert all(f.severity == "high" for f in _violation(report))
    assert engine.metrics.counters().get("qc_refine_missing_total", 0.0) >= 1.0
