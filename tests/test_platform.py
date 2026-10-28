"""项目五（编排平台）测试。

覆盖：工具作用域裁剪、权限边界、参数校验、重试、熔断、预算守卫、
HITL 中断恢复、编排端到端、观测指标。
"""

from __future__ import annotations

import pytest

from agent_platform.errors import (
    StepBudgetExceeded,
    TokenBudgetExceeded,
    ToolValidationError,
)
from agent_platform.graph import AgentCapabilities, NextAction
from agent_platform.hitl import ApprovalManager, ApprovalPolicy
from agent_platform.resilience import BudgetGuard, CircuitBreaker, RetryPolicy, retry_call
from agent_platform.runtime import AgentRuntime
from agent_platform.tools import ToolRegistry, ToolSpec
from agent_platform.tools.registry import _validate_arguments


# =========================================================================== #
# 一、工具与作用域裁剪
# =========================================================================== #
def _make_tool(name: str, tags: tuple[str, ...], risk: str = "low", idem: bool = True):
    return ToolSpec(
        name=name,
        description=f"测试工具 {name}",
        parameters={
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
        },
        handler=lambda x: {"echo": x, "tool": name},
        tags=tags,
        risk_level=risk,  # type: ignore[arg-type]
        idempotent=idem,
    )


def test_scoping_selects_only_matching_tags():
    reg = ToolRegistry()
    reg.register(_make_tool("a.read", ("a", "read")))
    reg.register(_make_tool("a.write", ("a", "write")))
    reg.register(_make_tool("b.read", ("b", "read")))

    # 命中任一标签即入选（并集语义）：这条规则决定"按意图注入哪些工具"
    assert {t.name for t in reg.select(tags=["a"])} == {"a.read", "a.write"}
    assert {t.name for t in reg.select(tags=["b"])} == {"b.read"}
    assert {t.name for t in reg.select(tags=["nothing"])} == set()
    # 显式指定名字优先于标签
    assert {t.name for t in reg.select(names=["b.read"])} == {"b.read"}
    # max_tools 上限，避免召回过多又回到"上下文爆炸"
    assert len(reg.select(tags=["a", "b"], max_tools=2)) == 2


def test_scoping_comparison_reports_token_saving():
    reg = ToolRegistry()
    for i in range(20):
        reg.register(_make_tool(f"tool_{i}", ("bulk",) if i else ("special",)))

    scoped = reg.select(tags=["special"])
    comparison = reg.scoping_comparison(scoped)

    assert comparison["total_tools"] == 20
    assert comparison["scoped_tools"] == 1
    assert comparison["saved_percent"] > 50  # 裁剪应显著降低 prompt token
    assert comparison["scoped_prompt_tokens"] < comparison["full_prompt_tokens"]


def test_scoping_on_real_registry_saves_85_to_97_percent(app_context_fixture):
    """真实装配（全量工具生态）上的作用域裁剪 —— 锁住 README 与简历的对外口径。

    上面的合成用例只证明"逻辑成立"；这里用装配好的真实注册中心，
    断言各业务路由单轮注入的 token 都落在 85%–97%（实测 88%–95%）。
    这个断言就是"裁剪省 token"这句话的可复现依据（对应面试追问）。
    """
    runtime = app_context_fixture.runtime
    registry = runtime.tools
    full_tokens = registry.scoping_comparison([])["full_prompt_tokens"]
    assert full_tokens > 2000, "全量工具 schema 应确实很贵，否则裁剪没有意义"

    routes = {
        "product_assistant": ("qa", "match", "proposal"),
        "marketing": ("generate", "case", "audit", "attribution", "activation"),
        "gateway": ("report", "route", "eval"),
    }
    for agent_name, route_names in routes.items():
        caps = runtime.registry.get(agent_name).capabilities
        for route in route_names:
            scoped = caps.resolve_scoped_tools({"route": route})
            cmp = registry.scoping_comparison(scoped)
            assert cmp["total_tools"] == len(registry.all())
            assert 85 <= cmp["saved_percent"] <= 97, (agent_name, route, cmp)
            assert 0 < cmp["scoped_prompt_tokens"] < full_tokens


def test_invoke_blocks_tool_outside_scope():
    """作用域即安全边界：不在作用域内的工具必须被拒绝（Prompt 注入防护）。"""
    reg = ToolRegistry()
    reg.register(_make_tool("danger.write", ("danger", "write")))

    result = reg.invoke("danger.write", {"x": "1"}, allowed={"safe.read"})
    assert result.ok is False
    assert result.blocked is True
    assert "不在当前作用域" in (result.error or "")


def test_invoke_validates_missing_required_argument():
    reg = ToolRegistry()
    reg.register(_make_tool("a.read", ("a",)))
    result = reg.invoke("a.read", {}, allowed={"a.read"})
    assert result.ok is False
    assert "缺少必填参数" in (result.error or "")


def test_validate_arguments_rejects_bool_for_integer():
    spec = ToolSpec(
        name="t", description="d",
        parameters={"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
        handler=lambda n: n,
    )
    with pytest.raises(ToolValidationError):
        _validate_arguments(spec, {"n": True})  # True 是 int 子类，必须被显式拒绝


# =========================================================================== #
# 二、韧性组件
# =========================================================================== #
def test_retry_policy_backoff_is_exponential_and_capped():
    policy = RetryPolicy(max_retries=5, base_seconds=0.1, max_seconds=0.5, jitter=0.0)
    assert policy.delay_for(1) == pytest.approx(0.1)
    assert policy.delay_for(2) == pytest.approx(0.2)
    assert policy.delay_for(3) == pytest.approx(0.4)
    assert policy.delay_for(4) == pytest.approx(0.5)  # 触顶


def test_retry_call_succeeds_after_transient_failures():
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] < 3:
            raise TimeoutError("temporary")
        return "ok"

    value, attempts = retry_call(flaky, RetryPolicy(max_retries=3, base_seconds=0, jitter=0))
    assert value == "ok"
    assert attempts == 3


def test_circuit_breaker_opens_after_threshold():
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=9999)
    assert breaker.allow("t") is True
    breaker.record_success("t")          # 成功会清零失败计数
    breaker.record_failure("t")
    assert breaker.allow("t") is True    # 只失败 1 次，未达阈值
    breaker.record_failure("t")
    assert breaker.allow("t") is False   # 达到阈值 → 打开
    assert breaker.snapshot()["t"]["state"] == "open"


def test_circuit_breaker_half_open_then_recovers():
    """冷却时间到 → 半开放行探测 → 成功则恢复。"""
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=0)
    breaker.record_failure("t")
    assert breaker.allow("t") is True                    # reset_seconds=0 → 立即半开
    assert breaker.snapshot()["t"]["state"] == "half_open"
    breaker.record_success("t")
    assert breaker.snapshot()["t"]["state"] == "closed"


def test_circuit_breaker_half_open_failure_reopens():
    breaker = CircuitBreaker(failure_threshold=1, reset_seconds=0)
    breaker.record_failure("t")
    assert breaker.allow("t") is True                    # 半开
    breaker.record_failure("t")                          # 探测失败
    assert breaker.snapshot()["t"]["state"] == "open"


def test_budget_guard_raises_on_limits():
    guard = BudgetGuard(max_steps=3, max_tokens=100)
    guard.check_steps(2)  # 不抛
    with pytest.raises(StepBudgetExceeded):
        guard.check_steps(3)
    with pytest.raises(TokenBudgetExceeded):
        guard.check_tokens(100)
    assert guard.remaining_steps(1) == 2


# =========================================================================== #
# 三、HITL 审批
# =========================================================================== #
def test_approval_policy_requires_approval_for_high_risk():
    policy = ApprovalPolicy()
    high = _make_tool("x.send", ("x",), risk="high", idem=False)
    low = _make_tool("x.read", ("x",))

    need, reason = policy.requires_approval(high)
    assert need is True
    assert "高风险" in reason

    need_low, _ = policy.requires_approval(low)
    assert need_low is False


def test_approval_manager_records_decision():
    manager = ApprovalManager(timeout_seconds=60)
    spec = _make_tool("x.send", ("x",), risk="high", idem=False)
    req = manager.create(run_id="r1", thread_id="t1", spec=spec, arguments={"x": "1"}, reason="高风险")

    assert len(manager.pending()) == 1
    decided = manager.resolve(req.request_id, approved=False, decided_by="张主管", comment="内容不合规")

    assert decided.status == "rejected"
    assert decided.decided_by == "张主管"
    assert len(manager.pending()) == 0


def test_approval_timeout_never_auto_approves():
    """超时只能"放弃"或"升级"，绝不能自动放行 —— 这是设计的底线。"""
    manager = ApprovalManager(timeout_seconds=0, timeout_strategy="escalate")
    spec = _make_tool("x.send", ("x",), risk="high", idem=False)
    req = manager.create(run_id="r1", thread_id="t1", spec=spec, arguments={"x": "1"}, reason="高风险")

    timed_out = manager.apply_timeout(req.request_id)
    # escalate 策略下状态必须是"已升级"，而不是含糊的 timeout
    assert timed_out.status == "escalated"
    assert timed_out.status != "approved"
    # abort 策略仍落 timeout
    abort_mgr = ApprovalManager(timeout_seconds=0, timeout_strategy="abort")
    req2 = abort_mgr.create(run_id="r2", thread_id="t2", spec=spec, arguments={"x": "1"}, reason="高风险")
    assert abort_mgr.apply_timeout(req2.request_id).status == "timeout"


# =========================================================================== #
# 四、编排端到端
# =========================================================================== #
def _runtime(settings, checkpointer) -> AgentRuntime:
    return AgentRuntime(settings=settings, checkpointer=checkpointer)


def test_agent_orchestration_runs_tools_and_responds(settings, checkpointer):
    rt = _runtime(settings, checkpointer)
    rt.tools.register(_make_tool("biz.read", ("biz",)))

    def decide(state, scoped):
        if [o for o in state.get("observations", []) if o.get("ok")]:
            return NextAction(tool=None)
        return NextAction(tool="biz.read", arguments={"x": "hello"}, thought="查询")

    def respond(state):
        hits = [o for o in state.get("observations", []) if o.get("ok")]
        return {"answer": hits[-1]["data"] if hits else None}

    rt.register(AgentCapabilities(
        name="t.agent", description="测试", tools=rt.tools,
        decide=decide, respond=respond, tool_tags_for=lambda s: ["biz"],
    ))
    result = rt.invoke("t.agent", "帮我查一下", thread_id="th1")

    assert result.status == "completed"
    assert result.output["answer"]["echo"] == "hello"
    assert [t["tool"] for t in result.tool_calls] == ["biz.read"]
    assert any(span["kind"] == "tool" for span in result.trajectory)  # 有 span 级埋点


def test_agent_planner_and_critic_are_optional_but_tracked(settings, checkpointer):
    rt = _runtime(settings, checkpointer)
    calls = {"critique": 0}

    def decide(state, scoped):
        return NextAction(tool=None)

    def plan(state, scoped):
        return ["步骤1", "步骤2"]

    def critique(state):
        calls["critique"] += 1
        return {"passed": True, "checks": {}}

    rt.register(AgentCapabilities(
        name="t.plan", description="测试", tools=rt.tools, decide=decide,
        respond=lambda s: {"ok": True}, plan=plan, critique=critique,
    ))
    result = rt.invoke("t.plan", "给我一个规划", thread_id="th_plan")

    assert result.status == "completed"
    assert calls["critique"] == 1


def test_hitl_interrupt_then_resume_with_audit_trail(settings, checkpointer):
    """高风险工具 → 挂起 → 审批通过 → 继续执行，并留下审批留痕。"""
    rt = _runtime(settings, checkpointer)

    def decide(state, scoped):
        done = [o for o in state.get("observations", [])
                if o.get("tool") == "platform.send_external_message"]
        if done:
            return NextAction(tool=None)
        return NextAction(
            tool="platform.send_external_message",
            arguments={"target": "wxid_1", "content": "测试消息"},
        )

    rt.register(AgentCapabilities(
        name="t.hitl", description="测试", tools=rt.tools, decide=decide,
        respond=lambda s: {"sent": True}, tool_tags_for=lambda s: ["platform"],
    ))

    first = rt.invoke("t.hitl", "发一条消息", thread_id="th_hitl")
    assert first.status == "interrupted"
    assert first.pending_approval["tool"] == "platform.send_external_message"

    resumed = rt.resume(
        "th_hitl", {"approved": True, "decided_by": "张主管", "comment": "已核对"}
    )
    assert resumed.status == "completed"
    assert len(resumed.approvals) == 1
    assert resumed.approvals[0]["approved"] is True
    assert resumed.approvals[0]["decided_by"] == "张主管"


def test_hitl_rejection_stops_the_action(settings, checkpointer):
    rt = _runtime(settings, checkpointer)

    def decide(state, scoped):
        done = [o for o in state.get("observations", [])
                if o.get("tool") == "platform.send_external_message"]
        if done:
            return NextAction(tool=None)
        return NextAction(
            tool="platform.send_external_message",
            arguments={"target": "wxid_1", "content": "测试消息"},
        )

    rt.register(AgentCapabilities(
        name="t.reject", description="测试", tools=rt.tools, decide=decide,
        respond=lambda s: {"sent": False}, tool_tags_for=lambda s: ["platform"],
    ))
    rt.invoke("t.reject", "发一条消息", thread_id="th_reject")
    resumed = rt.resume("th_reject", {"approved": False, "decided_by": "张主管"})

    assert resumed.status == "completed"
    assert resumed.approvals[0]["approved"] is False
    # 工具未被真正执行
    assert not any(t["ok"] for t in resumed.tool_calls)


def test_budget_breaker_escalates_instead_of_looping_forever(settings, checkpointer):
    """死循环必须被熔断并转人工 —— 且不能撞上 LangGraph 自身的递归上限。"""
    rt = _runtime(settings, checkpointer)

    def decide(state, scoped):
        return NextAction(tool="platform.now", arguments={})  # 永远有下一步

    rt.register(AgentCapabilities(
        name="t.loop", description="测试", tools=rt.tools, decide=decide,
        respond=lambda s: {"escalated": True, "error": s.get("error")},
        tool_tags_for=lambda s: ["platform"],
    ))
    result = rt.invoke("t.loop", "触发死循环", thread_id="th_loop")

    assert result.status == "escalated"
    assert "最大步数" in (result.error or "")


def test_invalid_tool_choice_is_fed_back_for_self_correction(settings, checkpointer):
    """模型选了不存在的工具 → 错误回灌到 observations，而不是直接崩。"""
    rt = _runtime(settings, checkpointer)

    def decide(state, scoped):
        failed = [o for o in state.get("observations", []) if not o.get("ok")]
        if failed:
            return NextAction(tool=None)  # 收到错误后收手
        return NextAction(tool="not.exist", arguments={})

    def respond(state):
        return {"observations": state.get("observations", [])}

    rt.register(AgentCapabilities(
        name="t.invalid", description="测试", tools=rt.tools, decide=decide,
        respond=respond, tool_tags_for=lambda s: ["platform"],
    ))
    result = rt.invoke("t.invalid", "调一个不存在的工具", thread_id="th_invalid")

    assert result.status == "completed"
    observations = result.output["observations"]
    assert len(observations) == 1
    assert observations[0]["ok"] is False
    assert "不存在" in observations[0]["error"]


def test_duplicate_agent_registration_raises(settings, checkpointer):
    rt = _runtime(settings, checkpointer)
    caps = AgentCapabilities(
        name="t.dup", description="测试", tools=rt.tools,
        decide=lambda s, sc: NextAction(), respond=lambda s: {},
    )
    rt.register(caps)
    with pytest.raises(ValueError):
        rt.register(caps)


def test_observability_metrics_and_spans(settings, checkpointer):
    rt = _runtime(settings, checkpointer)

    rt.register(AgentCapabilities(
        name="t.obs", description="测试", tools=rt.tools,
        decide=lambda s, sc: NextAction(), respond=lambda s: {"ok": True},
    ))
    result = rt.invoke("t.obs", "观测测试", thread_id="th_obs")

    counters = rt.metrics_snapshot()["counters"]
    assert any("agent_runs_total" in k for k in counters)

    # 决策轨迹：路由与应答一跳都不能少（否则排障时看不到首尾）
    names = [s["name"] for s in result.trajectory]
    assert "route" in names
    assert "decide" in names
    assert "respond" in names
    assert all("duration_ms" in s for s in result.trajectory)


def test_get_state_returns_thread_snapshot(settings, checkpointer):
    rt = _runtime(settings, checkpointer)
    rt.register(AgentCapabilities(
        name="t.state", description="测试", tools=rt.tools,
        decide=lambda s, sc: NextAction(), respond=lambda s: {"ok": True},
    ))
    rt.invoke("t.state", "问题", thread_id="th_state")

    snapshot = rt.get_state("th_state")
    assert snapshot["agent"] == "t.state"
    assert snapshot["status"] == "completed"
