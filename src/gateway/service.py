"""网关服务门面（项目六）。

对外提供四类能力，正好对应四个痛点：
    route_preview / demo_generate  → 统一入口 + 成本治理（能省钱）
    cost_report / replay           → 可观测（能定位、能算账）
    run_eval / gate_eval           → 评测闭环（敢发布）
    calibrate                      → Judge 校准（判分可信）

并把自己的能力注册成平台工具，这样"运维/研发"也能通过平台 Agent 查询网关状态
—— 平台与业务解耦，但网关可以把自己"挂"到平台上，这正是编排平台的价值。
"""

from __future__ import annotations

import uuid
from typing import Any

from agent_platform.config import Settings, get_settings
from agent_platform.llm.base import LLMProvider, Message
from agent_platform.logging_config import get_logger
from agent_platform.tools.registry import ToolRegistry
from agent_platform.tools.spec import ToolSpec
from gateway.domain import CachePolicy, EvalReport, GateDecision, GatewayStats, ModelTier
from gateway.evaluation.dataset import builtin_golden_cases, cases_for_scene, load_golden_set, scenes
from gateway.evaluation.judge import Judge
from gateway.evaluation.runner import EvalRunner, EvalTarget
from gateway.gateway import LLMGateway, build_gateway

logger = get_logger(__name__)


class GatewayService:
    """推理网关与评测平台的门面。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        gateway: LLMGateway | None = None,
        llm: LLMProvider | None = None,
        targets: dict[str, EvalTarget] | None = None,
    ) -> None:
        self.settings = settings or get_settings()

        # 网关本体（可用外部注入的替换，便于测试与灰度）
        self.gateway = gateway or build_gateway(self.settings)

        # 评测用例集：优先用外部 Golden Set（jsonl），否则用内置代表性集合
        golden_path = (self.settings.gateway_eval_golden_path or "").strip()
        if golden_path:
            try:
                self.cases = load_golden_set(golden_path)
                logger.info("已加载外部 Golden Set：%s（%d 条）", golden_path, len(self.cases))
            except Exception as exc:  # noqa: BLE001 - 外部文件有问题不能挡住服务启动
                logger.warning("外部 Golden Set 加载失败，回退内置集合：%s", exc)
                self.cases = builtin_golden_cases()
        else:
            self.cases = builtin_golden_cases()

        # 判官：离线模式（echo）下 LLM 判官没有实际判别力，所以只在真实模型下启用，
        # 避免"用假模型判分"污染评测结论 —— 这是评测可信度的底线。
        judge_llm = llm if (llm is not None and getattr(llm, "name", "") != "echo") else None
        self.judge = Judge(llm=judge_llm, use_llm=judge_llm is not None)
        self.runner = EvalRunner(self.judge, store=self.gateway.store, gateway=self.gateway)

        # 评测目标：scene → 调用函数（由装配层注入，网关不 import 任何业务代码）
        self.targets: dict[str, EvalTarget] = dict(targets or {})
        # 内存里的"上次基线"，让一次进程内就能演示"变更 → 回归 → 拦截"
        self._baselines: dict[str, EvalReport] = {}

    # ------------------------------------------------------------------ #
    # 成本治理
    # ------------------------------------------------------------------ #
    def route_preview(self, text: str, *, task: str = "") -> dict[str, Any]:
        """路由预览：告诉你这个问题会走哪档模型、为什么。"""
        return self.gateway.route_preview(text, task=task).model_dump(mode="json")

    def demo_generate(
        self,
        prompt: str,
        *,
        task: str = "",
        thread_id: str = "",
        cache: CachePolicy | None = None,
        tier: ModelTier | None = None,
        scope: str = "",
    ) -> dict[str, Any]:
        """走一次真实网关调用（用于演示/压测成本治理效果）。"""
        thread_id = thread_id or f"gw_{uuid.uuid4().hex[:8]}"
        resp = self.gateway.complete(
            [
                Message(role="system", content="你是助贷业务 AI 助手。"),
                Message(role="user", content=prompt),
            ],
            task=task,
            thread_id=thread_id,
            scope=scope or task or "demo",
            cache=cache,
            tier=tier,
        )
        records = [r for r in self.gateway.observer.ledger.all() if r.thread_id == thread_id]
        last = records[-1] if records else None
        return {
            "thread_id": thread_id,
            # 统一用"档位模型名"（=台账/路由的口径）。缓存命中时走的是另一条路径，
            # 若这里回 resp.model，命中/未命中两次会显示成两个不同模型，看着像个 bug。
            "model": (last.model if last else "") or resp.model,
            "provider_model": resp.model,
            "content": resp.content,
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "cost": last.cost if last else 0.0,
            "baseline_cost": last.baseline_cost if last else 0.0,
            "saved_percent": last.saved_percent if last else 0.0,
            "cache_status": last.cache_status if last else "miss",
            "tier": last.tier if last else "",
        }

    def cost_report(self, *, thread_id: str = "") -> dict[str, Any]:
        """成本报告：总量、按档位、按任务、以及**每会话单价**。"""
        ledger = self.gateway.observer.ledger
        return {
            "total": ledger.total(),
            "by_tier": ledger.by("tier"),
            "by_task": ledger.by("task"),
            "by_scope": ledger.by("scope"),
            "per_session": ledger.per_session() if not thread_id else ledger.by("thread_id"),
            "cache_hit_rate": ledger.cache_hit_rate(),
            "persisted": self.gateway.store.cost_summary(),
        }

    # ------------------------------------------------------------------ #
    # 可观测
    # ------------------------------------------------------------------ #
    def replay(self, thread_id: str) -> dict[str, Any]:
        """按会话回放决策轨迹（路由/缓存/降级/成本）。"""
        events = self.gateway.observer.replay(thread_id)
        return {
            "thread_id": thread_id,
            "events": events,
            "count": len(events),
            "costs": [r.model_dump(mode="json") for r in self.gateway.store.costs(thread_id=thread_id, limit=50)],
        }

    def stats(self) -> dict[str, Any]:
        st: GatewayStats = self.gateway.stats()
        payload = st.model_dump(mode="json")
        # enabled 的口径是"业务调用是否真的经过网关"，而不是"网关对象是否存在"。
        # LLMGateway 自己无从得知这一点（它只知道自己在运行），所以这里以配置为准。
        # 否则运维会看到 enabled=true 就以为成本治理已生效 —— 那是完全相反的结论。
        payload["enabled"] = bool(self.settings.gateway_enabled)
        payload["reachable"] = True
        return payload

    # ------------------------------------------------------------------ #
    # 评测闭环
    # ------------------------------------------------------------------ #
    def list_scenes(self) -> dict[str, Any]:
        all_scenes = scenes(self.cases)
        return {
            "scenes": all_scenes,
            "case_count": len(self.cases),
            "available_targets": sorted(self.targets),
            "hint": "外部可挂 jsonl 扩充用例集（GATEWAY_EVAL_GOLDEN_PATH）",
        }

    def run_eval(self, scene: str = "all", *, target: str = "", persist: bool = True) -> dict[str, Any]:
        """跑一轮评测。

        target 省略时：若该场景注册了目标就用它；否则若是 all 则聚合所有已注册目标。
        """
        cases = cases_for_scene(scene, self.cases)
        if not cases:
            raise KeyError(f"场景无可用用例：{scene}（可用：{scenes(self.cases)}）")

        fn = self._resolve_target(scene, target)
        report = self.runner.run(cases, fn, scene=scene, persist=persist)
        payload = report.model_dump(mode="json")
        payload["report_text"] = report.to_text()
        return payload

    def gate_eval(
        self, scene: str = "all", *, target: str = "", max_regression: float | None = None
    ) -> dict[str, Any]:
        """跑评测 + 与基线比对 → 给出**能否发布**的结论（CI 准入）。

        基线来源优先级：进程内上次结果 > 数据库历史 > 无基线（首跑放行）。
        """
        threshold = (
            self.settings.gateway_eval_max_regression if max_regression is None else max_regression
        )
        cases = cases_for_scene(scene, self.cases)
        fn = self._resolve_target(scene, target)

        # 关键顺序：**先取基线，再跑本次评测**。
        # 反过来的话，本次结果已经落库，"基线"就变成了自己 —— gate 永远通过，
        # 这是这类系统最隐蔽也最致命的 bug（评测自己把自己比没了）。
        baseline = self.runner.baseline_from_db(self.gateway.store, scene)

        current = self.runner.run(cases, fn, scene=scene, persist=True)
        decision: GateDecision = self.runner.compare(current, baseline, max_regression=threshold)

        # 本次结果成为新基线（内存 + 库），下次比对就有参照
        self._baselines[scene] = current

        return {
            "scene": scene,
            "current": {
                "eval_id": current.eval_id,
                "score": current.score,
                "passed": current.passed,
                "failed": current.failed,
                "total": current.total,
            },
            "baseline": (
                {"eval_id": baseline.eval_id, "score": baseline.score} if baseline else None
            ),
            "gate": decision.model_dump(mode="json"),
        }

    def _resolve_target(self, scene: str, target: str) -> EvalTarget:
        """解析评测目标函数。"""
        if target:
            if target not in self.targets:
                raise KeyError(f"评测目标未注册：{target}（可用：{sorted(self.targets)}）")
            return self.targets[target]
        if scene in self.targets:
            return self.targets[scene]
        if not self.targets:
            raise KeyError(
                "没有任何已注册的评测目标。评测目标必须由装配层注入"
                "（网关不 import 业务代码），见 app_context 的 EVAL_TARGETS 装配。"
            )
        if scene == "all":
            # all 场景：把用例分发到各自场景对应的目标上
            return self._dispatch_by_case_scene()
        raise KeyError(f"场景 {scene} 没有注册评测目标（可用：{sorted(self.targets)}）")

    def _dispatch_by_case_scene(self) -> EvalTarget:
        """把 all 场景的每条用例，按其自身 scene 路由到对应目标。"""

        def _target(case) -> dict[str, Any]:
            fn = self.targets.get(case.scene)
            if fn is None:
                return {"answer": "", "refused": False, "extra": {"note": f"场景 {case.scene} 无目标"}}
            return fn(case)

        return _target

    @staticmethod
    def calibrate(
        judge_scores: list[float], human_scores: list[float], *, tolerance: float = 10.0
    ) -> dict[str, Any]:
        """Judge 与人工双盲一致性校准。"""
        return Judge.calibrate(judge_scores, human_scores, tolerance=tolerance)

    # ------------------------------------------------------------------ #
    # 工具注册（把网关能力挂到平台上）
    # ------------------------------------------------------------------ #
    def register_tools(self, registry: ToolRegistry) -> list[str]:
        registered: list[str] = []

        def _route_preview(text: str, task: str = "") -> dict[str, Any]:
            return self.route_preview(text, task=task)

        registry.register(ToolSpec(
            name="gateway.route_preview",
            description=(
                "预览一次请求会被路由到哪个模型档位（small/medium/large）及理由。"
                "用于排查'为什么这次调用变贵了/变慢了'。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "待预览的请求文本"},
                    "task": {"type": "string", "description": "任务标签，如 policy_qa"},
                },
                "required": ["text"],
            },
            handler=_route_preview,
            tags=("gateway", "route", "read"),
        ))
        registered.append("gateway.route_preview")

        def _cost(thread_id: str = "") -> dict[str, Any]:
            return self.cost_report(thread_id=thread_id)

        registry.register(ToolSpec(
            name="gateway.cost_report",
            description=(
                "查询推理成本报告：总成本、按档位/任务分布、每会话单价、缓存命中率、节省比例。"
            ),
            parameters={
                "type": "object",
                "properties": {"thread_id": {"type": "string", "description": "只看某个会话（可空）"}},
                "required": [],
            },
            handler=_cost,
            tags=("gateway", "cost", "read"),
        ))
        registered.append("gateway.cost_report")

        def _replay(thread_id: str) -> dict[str, Any]:
            return self.replay(thread_id)

        registry.register(ToolSpec(
            name="gateway.replay",
            description=(
                "按会话回放完整决策轨迹：路由档位、缓存命中、降级次数、每步 token 与成本。"
                "用于定位'中间某一步静默失败'这类故障。"
            ),
            parameters={
                "type": "object",
                "properties": {"thread_id": {"type": "string"}},
                "required": ["thread_id"],
            },
            handler=_replay,
            tags=("gateway", "observe", "read"),
        ))
        registered.append("gateway.replay")

        def _stats() -> dict[str, Any]:
            return self.stats()

        registry.register(ToolSpec(
            name="gateway.stats",
            description="网关运行态快照：调用量、缓存命中率、路由分布、节省比例、密钥与限流状态。",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_stats,
            tags=("gateway", "observe", "cost", "read"),
        ))
        registered.append("gateway.stats")

        def _run_eval(scene: str = "all") -> dict[str, Any]:
            return self.run_eval(scene)

        registry.register(ToolSpec(
            name="gateway.run_eval",
            description=(
                "跑一轮 Golden Set 评测，返回加权通过率与失败用例明细。"
                "用于 Prompt/模型变更后的效果评估。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "scene": {"type": "string", "description": "场景名或 all"},
                },
                "required": [],
            },
            handler=_run_eval,
            tags=("gateway", "eval", "read"),
        ))
        registered.append("gateway.run_eval")

        def _gate(scene: str = "all") -> dict[str, Any]:
            return self.gate_eval(scene)

        registry.register(ToolSpec(
            name="gateway.gate_eval",
            description=(
                "跑评测并与基线比对，给出'能否发布'的准入结论。"
                "任何关键词/模型变更都应以它作为发布门槛。"
            ),
            parameters={
                "type": "object",
                "properties": {"scene": {"type": "string"}},
                "required": [],
            },
            handler=_gate,
            tags=("gateway", "eval", "read"),
        ))
        registered.append("gateway.gate_eval")

        logger.info("网关服务注册工具 %d 个：%s", len(registered), registered)
        return registered

    # ------------------------------------------------------------------ #
    # 其他
    # ------------------------------------------------------------------ #
    def stats_summary(self) -> dict[str, Any]:
        return {"gateway": self.stats(), "eval": self.list_scenes()}

    def close(self) -> None:
        self.gateway.close()
