"""Agent 能力声明（装配契约）。

这是"装配式开发"的关键抽象：平台只负责**编排骨架**（路由 → 规划 → 执行 → 评审 → 应答
＋ 中断恢复 ＋ 失败自愈 ＋ 观测），业务 Agent 只需要提供自己的"能力函数"。

于是新增一个业务 Agent 的成本 = 写几个纯函数 + 注册工具，
而不是重写一遍编排、状态、中断、熔断、观测 —— 这就是"新 Agent 接入 60 人日 → 30 人日"的来源。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_platform.state.schema import AgentState
from agent_platform.tools.registry import ToolRegistry
from agent_platform.tools.spec import ToolSpec

# 永远可用的工具标签。
# 为什么需要这个例外：作用域裁剪是"按意图只给相关工具"，但**兜底逃生舱
# （转人工、告警）绝对不能因为裁剪而不可用** —— 否则 Agent 走到能力边界时
# 连"转人工"都调不到，只能硬编一个答案，风险更大。
ALWAYS_AVAILABLE_TAGS: tuple[str, ...] = ("escalation",)


@dataclass
class NextAction:
    """Executor 的下一步决策。

    tool = None 表示"信息已足够，不再调工具"，交给 Critic / Respond。
    这种"由业务自己决定何时收手"的设计，避免了平台层猜业务意图。
    """

    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    thought: str = ""


# ---- 业务 Agent 需要提供的回调签名 ---------------------------------------- #
RouteFn = Callable[[AgentState], str]
"""意图路由：读 state，返回路由标签（如 "qa" / "match" / "proposal"）。"""

ToolTagResolver = Callable[[AgentState], list[str]]
"""作用域解析：按当前状态返回本轮应注入的工具标签。这就是 scoping 的钩子。"""

PlanFn = Callable[[AgentState, list[ToolSpec]], list[str]]
"""规划器：把任务拆成步骤描述（可选）。"""

DecideFn = Callable[[AgentState, list[ToolSpec]], NextAction]
"""执行决策：给定状态与本轮可用工具，决定下一个动作。

**重要约束**：必须是"状态纯函数"（同样输入 → 同样输出）。
原因：LangGraph 在 HITL 恢复时会**重放整个节点**，若决策不确定，
恢复后可能选到不同的工具，导致审批与执行不一致。
"""

CritiqueFn = Callable[[AgentState], dict[str, Any]]
"""评审器：对当前产出做校验，返回 {"passed": bool, ...}（可选）。"""

RespondFn = Callable[[AgentState], dict[str, Any]]
"""应答器：生成最终业务输出（结构化 dict，供 API 直接返回）。"""


@dataclass
class AgentCapabilities:
    """一个业务 Agent 的完整能力声明。"""

    name: str
    """Agent 唯一名，如 product_assistant.qa。"""

    description: str
    """给平台/运维看的一句话说明。"""

    tools: ToolRegistry
    """该 Agent 可用的工具注册表。"""

    decide: DecideFn
    """必需：执行决策函数。"""

    respond: RespondFn
    """必需：最终应答函数。"""

    route: RouteFn | None = None
    """可选：意图路由。不提供则跳过路由节点。"""

    tool_tags_for: ToolTagResolver | None = None
    """可选：作用域标签解析。不提供则全量工具（不推荐，会失去 token 优化）。"""

    plan: PlanFn | None = None
    """可选：规划器。不提供则跳过规划节点，直接进入执行循环。"""

    critique: CritiqueFn | None = None
    """可选：评审器（Critic）。不提供则跳过评审节点。"""

    max_critique_rounds: int = 2
    """评审回退重做的最大轮数，防止"评审-重做"死循环。

    必须 ≥ 2 才能让"打回重做"真正发生一次：第 1 轮不通过 → revise → 第 2 轮
    才强制接受。取 1 会让 revise 分支永远走不到（Critic 变成纯记录器）。
    """

    version: str = "1.0.0"
    tags: tuple[str, ...] = ()
    """业务标签，便于按业务线检索 Agent。"""

    def resolve_scoped_tools(self, state: AgentState) -> list[ToolSpec]:
        """按当前状态解析本轮作用域内的工具（含 fallback 与逃生舱）。"""
        if self.tool_tags_for is None:
            scoped = self.tools.all()
        else:
            tags = self.tool_tags_for(state)
            if not tags:
                scoped = self.tools.all()
            else:
                # fallback：标签召回为空时放开全量，避免"裁剪过度导致无工具可用"
                scoped = self.tools.select(tags=tags) or self.tools.all()

        # 追加"永远可用"的逃生舱工具（转人工等），并去重
        by_name = {t.name: t for t in scoped}
        for spec in self.tools.all():
            if set(spec.tags) & set(ALWAYS_AVAILABLE_TAGS):
                by_name.setdefault(spec.name, spec)
        return list(by_name.values())
