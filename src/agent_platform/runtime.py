"""Agent 运行时 —— 平台的统一执行入口。

对外只暴露三个动作：
    invoke()   启动一次任务
    resume()   对挂起的任务（HITL 审批）下达决策后继续
    get_state() 查看某个会话的当前状态

把"怎么跑图"这件事彻底收口在这里，业务侧与 API 层都不需要碰 LangGraph。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from langgraph.types import Command
from pydantic import BaseModel, Field

from agent_platform.agents.registry import AgentRegistration, AgentRegistry
from agent_platform.approvals_store import SqliteApprovalStore
from agent_platform.checkpointing import build_checkpointer
from agent_platform.config import Settings, get_settings
from agent_platform.graph.capabilities import AgentCapabilities
from agent_platform.graph.nodes import GraphDeps
from agent_platform.hitl import ApprovalManager, ApprovalPolicy
from agent_platform.llm.base import EmbeddingProvider, LLMProvider
from agent_platform.llm.providers import build_embedder, build_llm
from agent_platform.logging_config import bind_run_context, get_logger
from agent_platform.observability import Metrics, Tracer
from agent_platform.resilience import BudgetGuard
from agent_platform.state.schema import initial_state
from agent_platform.thread_map import ThreadAgentMap
from agent_platform.tools.builtin import build_builtin_tools
from agent_platform.tools.registry import ToolRegistry
from agent_platform.tracing import current_trace_id

logger = get_logger(__name__)


class RunResult(BaseModel):
    """一次运行的完整结果（API 直接返回这个结构）。"""

    run_id: str
    thread_id: str
    trace_id: str = ""
    agent: str
    status: str
    """completed | interrupted | escalated | failed"""
    route: str = ""
    steps: int = 0
    tokens_used: int = 0
    output: dict[str, Any] = Field(default_factory=dict)
    interrupts: list[dict[str, Any]] = Field(default_factory=list)
    """非空表示任务被 HITL 挂起，需要调用 resume() 继续。"""

    pending_approval: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    approvals: list[dict[str, Any]] = Field(default_factory=list)
    route_traceability: dict[str, Any] = Field(default_factory=dict)
    """保留字段：业务可把溯源/审计信息放这里透出。"""
    latency_ms: float = 0.0
    error: str | None = None
    trajectory: list[dict[str, Any]] = Field(default_factory=list)
    """span 级决策轨迹，用于排障回放。"""


class AgentRuntime:
    """平台运行时。一个进程内通常只创建一个实例。"""

    def _build_approval_store(self) -> Any:
        if self.settings.approval_store_backend == "sqlite":
            self.settings.ensure_dirs()
            return SqliteApprovalStore(self.settings.approvals_sqlite_abs_path)
        return None  # ApprovalManager 内部退化为内存实现

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        llm: LLMProvider | None = None,
        embedder: EmbeddingProvider | None = None,
        tools: ToolRegistry | None = None,
        checkpointer: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.llm = llm or build_llm(self.settings)
        self.embedder = embedder or build_embedder(self.settings)

        # ---- 治理与观测组件 ----
        self.metrics = Metrics()
        self.tracer = Tracer(metrics=self.metrics)
        self.tools = tools or ToolRegistry(metrics=self.metrics)
        # 注册平台内置工具（业务工具由各业务服务后续追加注册）
        for spec in build_builtin_tools():
            if not self.tools.has(spec.name):
                self.tools.register(spec)

        self.approvals = ApprovalManager(
            policy=ApprovalPolicy(),
            timeout_seconds=self.settings.approval_timeout_seconds,
            timeout_strategy=self.settings.approval_timeout_strategy,
            store=self._build_approval_store(),
            metrics=self.metrics,
        )
        self.guard = BudgetGuard(
            max_steps=self.settings.max_agent_steps,
            max_tokens=self.settings.max_agent_tokens,
        )
        self.checkpointer = (
            checkpointer if checkpointer is not None else build_checkpointer(self.settings)
        )

        self.deps = GraphDeps(
            tools=self.tools,
            approvals=self.approvals,
            guard=self.guard,
            tracer=self.tracer,
            metrics=self.metrics,
            settings=self.settings,
        )
        self.registry = AgentRegistry(deps=self.deps, checkpointer=self.checkpointer)

        # thread_id → agent 名称的映射。
        # 持久化说明见 thread_map.py：HITL 挂起可能跨重启/跨副本恢复，
        # 纯内存映射会把恢复路径逼成"遍历所有 Agent 反查"；
        # checkpointer_backend=memory（纯内存测试）时退化为进程内 dict。
        if self.settings.checkpointer_backend == "memory":
            self._thread_map = ThreadAgentMap(None)
        else:
            self._thread_map = ThreadAgentMap(self.settings.runtime_map_abs_path)

    # ------------------------------------------------------------------ #
    # 注册
    # ------------------------------------------------------------------ #
    def register(self, capabilities: AgentCapabilities) -> AgentRegistration:
        """注册一个业务 Agent，返回注册信息（含装配好的图）。"""
        reg = self.registry.register(capabilities)
        return reg

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #
    def invoke(
        self,
        agent: str,
        user_input: str,
        *,
        context: dict[str, Any] | None = None,
        thread_id: str | None = None,
        run_id: str | None = None,
    ) -> RunResult:
        """启动一次任务。

        thread_id 相同 → 视为同一会话，状态可续（Checkpointer 保证）；
        不传则自动生成一个新会话。
        """
        reg = self.registry.get(agent)
        thread_id = thread_id or f"th_{uuid.uuid4().hex[:12]}"
        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        self._thread_map.put(thread_id, agent)

        bind_run_context(run_id, thread_id)
        self.tracer.bind(run_id, thread_id)
        started = time.perf_counter()

        state_in = initial_state(
            run_id=run_id,
            agent_name=agent,
            thread_id=thread_id,
            user_input=user_input,
            context=context or {},
        )
        config = self._config(thread_id, reg)

        try:
            final_state = reg.graph.invoke(state_in, config)
        except Exception as exc:  # noqa: BLE001 - 平台必须兜住把失败结构化返回
            logger.exception("Agent 运行失败：%s", agent)
            self.metrics.incr("agent_runs_total", agent=agent, status="failed")
            return RunResult(
                run_id=run_id,
                thread_id=thread_id,
                agent=agent,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.perf_counter() - started) * 1000,
                trajectory=self.tracer.to_trajectory(),
            )

        return self._to_result(
            reg, run_id, thread_id, final_state, started, resumed=False
        )

    def resume(
        self,
        thread_id: str,
        decision: dict[str, Any] | str | bool,
        *,
        run_id: str | None = None,
    ) -> RunResult:
        """对 HITL 挂起的任务下达决策并继续执行。

        decision 会被作为 interrupt() 的返回值传回节点：
            {"approved": True, "decided_by": "张主管", "comment": "材料已核对"}
        """
        agent = self._thread_map.get(thread_id)
        if agent is None:
            # 兜底：无法得知 agent 时，遍历所有已注册 Agent 找该 thread 的状态
            agent = self._guess_agent_by_thread(thread_id)
        reg = self.registry.get(agent)

        run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        bind_run_context(run_id, thread_id)
        # 注意：恢复时不重置 tracer 的 run_id，但需要保留已发生的 span，
        # 所以这里不调用 tracer.bind（否则会清掉中断前的轨迹）
        started = time.perf_counter()
        config = self._config(thread_id, reg)

        # langgraph 的恢复语义：把 resume 值作为 interrupt() 的返回值
        resume_value = decision if isinstance(decision, dict) else {"approved": bool(decision)}

        try:
            final_state = reg.graph.invoke(Command(resume=resume_value), config)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Agent 恢复失败：%s", agent)
            return RunResult(
                run_id=run_id,
                thread_id=thread_id,
                agent=agent,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.perf_counter() - started) * 1000,
                trajectory=self.tracer.to_trajectory(),
            )

        return self._to_result(reg, run_id, thread_id, final_state, started, resumed=True)

    def get_state(self, thread_id: str) -> dict[str, Any]:
        """查询会话当前状态（不推进执行）。"""
        agent = self._thread_map.get(thread_id) or self._guess_agent_by_thread(thread_id)
        reg = self.registry.get(agent)
        snapshot = reg.graph.get_state(self._config(thread_id, reg))
        values = dict(snapshot.values) if snapshot and snapshot.values else {}
        return {
            "thread_id": thread_id,
            "agent": agent,
            "status": values.get("status", "unknown"),
            "next": list(snapshot.next) if snapshot else [],
            "values": _jsonable(values),
        }

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _config(self, thread_id: str, reg: AgentRegistration) -> dict[str, Any]:
        """构造 LangGraph 运行配置。

        recursion_limit 必须显式给：否则 max_steps 较大时会先撞图递归上限，
        导致"熔断转人工"这条业务路径走不到（详见 builder.recursion_limit_for 注释）。
        """
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": reg.recursion_limit,
        }

    def _guess_agent_by_thread(self, thread_id: str) -> str:
        """在已注册 Agent 中反查该 thread 归属（用于进程重启后的恢复场景）。"""
        for reg in self.registry.all():
            try:
                snapshot = reg.graph.get_state(self._config(thread_id, reg))
            except Exception:  # noqa: BLE001
                continue
            if snapshot and snapshot.values:
                name = snapshot.values.get("agent_name")
                if name:
                    self._thread_map.put(thread_id, str(name))
                    return str(name)
        raise KeyError(f"无法定位会话 {thread_id} 所属的 Agent")

    def _to_result(
        self,
        reg: AgentRegistration,
        run_id: str,
        thread_id: str,
        state: dict[str, Any],
        started: float,
        *,
        resumed: bool,
    ) -> RunResult:
        """把图返回的原始 state 转成对外 RunResult。"""
        # HITL 挂起时，LangGraph 会在 state 里塞一个 __interrupt__ 键
        raw_interrupts = state.get("__interrupt__") or []
        interrupts: list[dict[str, Any]] = []
        for item in raw_interrupts:
            value = getattr(item, "value", item)
            interrupts.append(
                {
                    "id": getattr(item, "id", None),
                    "value": _jsonable(value),
                }
            )

        if interrupts:
            status = "interrupted"
        else:
            status = str(state.get("status") or "completed")

        pending = interrupts[0]["value"] if interrupts else None

        return RunResult(
            run_id=run_id,
            thread_id=thread_id,
            agent=reg.name,
            status=status,
            route=str(state.get("route", "")),
            steps=int(state.get("step", 0)),
            tokens_used=int(state.get("tokens_used", 0)),
            output=_jsonable(state.get("output") or {}),
            interrupts=interrupts,
            pending_approval=pending,
            tool_calls=_jsonable(state.get("tool_calls") or []),
            approvals=_jsonable(state.get("approvals") or []),
            latency_ms=(time.perf_counter() - started) * 1000,
            error=state.get("error") or None,
            trace_id=current_trace_id() or run_id,
            trajectory=self.tracer.to_trajectory(),
        )

    # ------------------------------------------------------------------ #
    # 观测 / 治理信息
    # ------------------------------------------------------------------ #
    def metrics_snapshot(self) -> dict[str, Any]:
        return self.metrics.snapshot()

    def tools_describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "tags": list(t.tags),
                "risk_level": t.risk_level,
                "idempotent": t.idempotent,
            }
            for t in self.tools.all()
        ]


def _jsonable(value: Any) -> Any:
    """把任意对象转成可 JSON 序列化的结构。

    Checkpointer 与 API 都要求可序列化；这里做一次递归清洗，
    避免"某个工具返回了 datetime / 自定义对象"导致整个响应 500。
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump())
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
