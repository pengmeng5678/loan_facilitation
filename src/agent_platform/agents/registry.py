"""Agent 注册中心。

平台不关心业务，只关心"谁注册了什么能力"。注册后平台负责：
    - 按能力装配编排图（含/不含 planner、critic）
    - 计算该 Agent 的 recursion_limit
    - 提供统一的调用入口（见 runtime.AgentRuntime）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_platform.errors import AgentNotFoundError
from agent_platform.graph.builder import build_agent_graph, recursion_limit_for
from agent_platform.graph.capabilities import AgentCapabilities
from agent_platform.graph.nodes import GraphDeps
from agent_platform.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class AgentRegistration:
    """一个已注册的 Agent：能力声明 + 编译后的图 + 运行参数。"""

    capabilities: AgentCapabilities
    graph: Any
    recursion_limit: int

    @property
    def name(self) -> str:
        return self.capabilities.name

    def describe(self) -> dict[str, Any]:
        caps = self.capabilities
        return {
            "name": caps.name,
            "description": caps.description,
            "version": caps.version,
            "tags": list(caps.tags),
            "has_planner": caps.plan is not None,
            "has_critic": caps.critique is not None,
            "tool_count": len(caps.tools.all()),
            "tools": caps.tools.names(),
            "recursion_limit": self.recursion_limit,
        }


@dataclass
class AgentRegistry:
    """Agent 注册表。"""

    deps: GraphDeps
    checkpointer: Any | None = None
    _agents: dict[str, AgentRegistration] = field(default_factory=dict)

    def register(self, capabilities: AgentCapabilities) -> AgentRegistration:
        """装配并注册一个业务 Agent。"""
        if capabilities.name in self._agents:
            raise ValueError(f"Agent 重复注册：{capabilities.name}")

        graph = build_agent_graph(capabilities, self.deps, checkpointer=self.checkpointer)
        # 评审上限从能力声明取，不再写死：业务把 max_critique_rounds 配大时，
        # recursion_limit 必须同步放大，否则评审还没强制接受就先撞图递归上限
        has_critique = capabilities.critique is not None
        limit = recursion_limit_for(
            max_steps=self.deps.settings.max_agent_steps,
            has_critique=has_critique,
            critique_rounds=(capabilities.max_critique_rounds + 1) if has_critique else None,
        )
        reg = AgentRegistration(capabilities=capabilities, graph=graph, recursion_limit=limit)
        self._agents[capabilities.name] = reg
        logger.info("注册 Agent：%s（v%s）", capabilities.name, capabilities.version)
        return reg

    def get(self, name: str) -> AgentRegistration:
        if name not in self._agents:
            raise AgentNotFoundError(
                f"Agent 未注册：{name}。已注册：{sorted(self._agents)}"
            )
        return self._agents[name]

    def has(self, name: str) -> bool:
        return name in self._agents

    def all(self) -> list[AgentRegistration]:
        return list(self._agents.values())

    def names(self) -> list[str]:
        return list(self._agents.keys())

    def describe_all(self) -> list[dict[str, Any]]:
        return [a.describe() for a in self._agents.values()]
