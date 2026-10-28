"""评审打回重做（revise）链路测试。

锁住两件事：
    1. Critic 不通过时，图真的会回退到 execute 重做一次（不是纯记录器）；
    2. 重做时**写工具不得重复产生副作用** —— 只回放上次结果；
       读工具则允许真实重放（幂等，重放无副作用）。
"""

from __future__ import annotations

from typing import Any

from agent_platform.graph import AgentCapabilities, NextAction
from agent_platform.runtime import AgentRuntime
from agent_platform.tools import ToolSpec


def _runtime(settings, checkpointer) -> AgentRuntime:
    return AgentRuntime(settings=settings, checkpointer=checkpointer)


def _register_tool(rt: AgentRuntime, name: str, calls: dict[str, int], *, idempotent: bool) -> None:
    def handler(x: str) -> dict[str, Any]:
        calls["tool"] += 1
        return {"echo": x, "tool": name}

    rt.tools.register(
        ToolSpec(
            name=name,
            description="测试用工具",
            parameters={
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
            handler=handler,
            tags=("biz",),
            risk_level="low",
            idempotent=idempotent,
        )
    )


def _make_decide(tool_name: str, max_calls: int):
    """只在该工具被评审打回后重做，且重做次数受上限约束。"""

    def decide(state, scoped):
        observations = [o for o in state.get("observations", []) if o.get("tool") == tool_name]
        if len(observations) >= max_calls:
            return NextAction(tool=None)
        if not observations:
            return NextAction(tool=tool_name, arguments={"x": "1"}, thought="首次执行")
        critique = state.get("critique") or {}
        if critique and not critique.get("passed"):
            return NextAction(tool=tool_name, arguments={"x": "1"}, thought="评审打回后重做")
        return NextAction(tool=None)

    return decide


def _make_critique(calls: dict[str, int]):
    """第 1 轮不通过（触发 revise），第 2 轮通过。"""

    def critique(state):
        calls["critique"] += 1
        if calls["critique"] == 1:
            return {
                "passed": False,
                "reasons": ["输出缺少合规免责声明"],
                "revise_hint": {"action": "redo", "note": "补上免责声明"},
            }
        return {"passed": True, "reasons": []}

    return critique


def test_revise_does_not_replay_write_tool(settings, checkpointer):
    rt = _runtime(settings, checkpointer)
    calls = {"tool": 0, "critique": 0}
    _register_tool(rt, "biz.send_sms", calls, idempotent=False)

    rt.register(
        AgentCapabilities(
            name="t.revise_write",
            description="测试：写工具重做保护",
            tools=rt.tools,
            decide=_make_decide("biz.send_sms", max_calls=2),
            respond=lambda s: {"ok": True},
            tool_tags_for=lambda s: ["biz"],
            critique=_make_critique(calls),
        )
    )

    result = rt.invoke("t.revise_write", "给客户发短信", thread_id="th_revise_write")

    assert result.status == "completed"
    assert calls["critique"] == 2, "Critic 必须真的打回一次并复评"
    assert calls["tool"] == 1, "写工具在一次 run 内只能真正执行一次"
    # 重做时被拦下（只回放上次结果），因此 tool_calls 也只有一条真实调用
    assert len(result.tool_calls) == 1
    assert rt.metrics.counters().get("agent_write_replay_blocked_total{tool=biz.send_sms}") == 1


def test_revise_replays_read_tool(settings, checkpointer):
    rt = _runtime(settings, checkpointer)
    calls = {"tool": 0, "critique": 0}
    _register_tool(rt, "biz.lookup", calls, idempotent=True)

    rt.register(
        AgentCapabilities(
            name="t.revise_read",
            description="测试：读工具可重放",
            tools=rt.tools,
            decide=_make_decide("biz.lookup", max_calls=2),
            respond=lambda s: {"ok": True},
            tool_tags_for=lambda s: ["biz"],
            critique=_make_critique(calls),
        )
    )

    result = rt.invoke("t.revise_read", "查一下资料", thread_id="th_revise_read")

    assert result.status == "completed"
    assert calls["critique"] == 2
    assert calls["tool"] == 2, "幂等读工具允许在重做时真实重放"
    assert len(result.tool_calls) == 2
    assert "agent_write_replay_blocked_total{tool=biz.lookup}" not in rt.metrics.counters()
