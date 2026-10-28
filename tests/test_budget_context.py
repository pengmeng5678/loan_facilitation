"""预算守卫必须按"全上下文"记账。

旧实现只算 `user_input + 工具 schema`，observations/messages 累积再多也不会
触发熔断 —— 多轮任务的 token 预算等于没有。
"""

from __future__ import annotations

from agent_platform.graph import AgentCapabilities, NextAction
from agent_platform.graph.nodes import estimate_context_tokens
from agent_platform.runtime import AgentRuntime
from agent_platform.tools.spec import ToolSpec


def test_context_tokens_grow_with_accumulated_observations():
    small = {"user_input": "你好"}
    big = {
        "user_input": "你好",
        "observations": [{"step": i, "data": "x" * 2000} for i in range(5)],
    }
    assert estimate_context_tokens(big) > estimate_context_tokens(small) * 10


def test_budget_breaker_fires_on_accumulated_context(settings, checkpointer):
    # 预算调到很低：只有把 observations 计入才可能在第 3 步熔断
    tight = settings.model_copy(update={"max_agent_tokens": 1000})
    rt = AgentRuntime(settings=tight, checkpointer=checkpointer)

    rt.tools.register(ToolSpec(
        name="big.return",
        description="返回一大段文本",
        parameters={"type": "object", "properties": {}},
        handler=lambda **_: {"payload": "长" * 4000},
        tags=("big",),
    ))

    def decide(state, scoped):
        # 连续跑两步，让 observations 累积到第二步开头才触发预算检查
        if len(state.get("observations", [])) < 2:
            return NextAction(tool="big.return", arguments={})
        return NextAction(tool=None)

    rt.register(AgentCapabilities(
        name="t.budget",
        description="测试",
        tools=rt.tools,
        decide=decide,
        respond=lambda s: {"error": s.get("error", "")},
        tool_tags_for=lambda s: ["big"],
    ))

    result = rt.invoke("t.budget", "跑一个很大的返回", thread_id="th_budget_ctx")
    assert result.status == "escalated"
    assert "预算" in (result.error or "")
