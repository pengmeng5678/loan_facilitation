"""EvalRunner：把评测跑起来，并回答"这次变更能不能发布"。

关键设计：**评测目标是注入的**（`target: Callable[[EvalCase], dict]`），
所以网关包不需要 import 任何业务代码 —— 谁想被评测，谁把自己的调用
包装成一个函数传进来即可。这样评测平台才能同时评测产品助手、话术陪练、
素材生成、甚至网关自己的路由。

CI 准入（gate）是这套东西的价值落点：
    Prompt / 模型变更 → 跑一遍 Golden Set → 与基线比 → 退化超阈值就拦发布。
    这一下就把"改 Prompt 靠感觉"变成了"改 Prompt 必须过门槛"。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from agent_platform.logging_config import get_logger
from gateway.domain import CaseResult, EvalCase, EvalReport, GateDecision
from gateway.evaluation.judge import Judge

logger = get_logger(__name__)

# 评测目标函数签名：吃一条用例，吐一个 dict（answer / refused / extra）
EvalTarget = Callable[[EvalCase], dict[str, Any]]


class EvalRunner:
    """评测执行器。"""

    def __init__(
        self,
        judge: Judge | None = None,
        *,
        store: Any | None = None,
        gateway: Any | None = None,
    ) -> None:
        self.judge = judge or Judge()
        self.store = store
        self.gateway = gateway  # 若有网关，顺带记录本轮评测的 token 成本

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #
    def run(
        self,
        cases: list[EvalCase],
        target: EvalTarget,
        *,
        scene: str = "all",
        persist: bool = True,
    ) -> EvalReport:
        """跑一轮评测。"""
        started = time.perf_counter()
        cost_before = self._cost_total()

        results: list[CaseResult] = []
        for case in cases:
            results.append(self._run_one(case, target))

        total_weight = sum(c.weight for c in cases) or 1.0
        # strict=True：results 与 cases 一一对应，长度不一致属于编码错误，
        # 必须当场炸出来（否则会静默算出一个偏低的加权分）
        weighted = sum(r.score * c.weight for r, c in zip(results, cases, strict=True)) / total_weight
        passed = sum(1 for r in results if r.passed)

        report = EvalReport(
            eval_id=f"eval_{uuid.uuid4().hex[:10]}",
            scene=scene,
            total=len(results),
            passed=passed,
            failed=len(results) - passed,
            score=round(weighted, 4),
            case_results=results,
            token_cost=round(max(0.0, self._cost_total() - cost_before), 6),
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
            judge_source=("blended" if any(r.judge_source == "blended" for r in results) else "rules"),
        )

        if persist and self.store is not None:
            try:
                self.store.add_eval_run(report)
            except Exception:  # noqa: BLE001 - 落盘失败不影响评测结论
                logger.warning("评测结果落盘失败", exc_info=True)

        logger.info("评测完成：%s（通过 %d/%d，得分 %.3f）", report.eval_id, passed, len(results), weighted)
        return report

    def _run_one(self, case: EvalCase, target: EvalTarget) -> CaseResult:
        """跑一条用例。任何异常都转成"该用例失败"，而不是整轮崩掉。"""
        try:
            actual = target(case) or {}
        except Exception as exc:  # noqa: BLE001 - 被测系统崩了也是一种评测结论
            return CaseResult(
                case_id=case.case_id,
                scene=case.scene,
                passed=False,
                score=0.0,
                reason=f"目标调用异常：{type(exc).__name__}: {exc}",
                judge_source="error",
            )

        result = self.judge.score_case(case, actual)

        # 轨迹评测：把"走了哪条路"的证据附在结果上。
        # 这是 Agent 评测与普通模型评测最大的区别 —— 只看最终答案会漏掉中间问题。
        extra = actual.get("extra") or {}
        if extra:
            trace_bits = [
                f"{k}={extra[k]}" for k in ("tier", "model", "cache", "fallbacks", "tools") if k in extra
            ]
            if trace_bits:
                result.reason = (result.reason + "；" if result.reason else "") + "轨迹：" + ", ".join(trace_bits)
        return result

    def _cost_total(self) -> float:
        if self.gateway is None:
            return 0.0
        try:
            return float(self.gateway.observer.ledger.total()["cost"])
        except Exception:  # noqa: BLE001
            return 0.0

    # ------------------------------------------------------------------ #
    # CI 准入
    # ------------------------------------------------------------------ #
    def compare(
        self,
        current: EvalReport,
        baseline: EvalReport | None,
        *,
        max_regression: float = 0.02,
    ) -> GateDecision:
        """与基线比对，给出准入结论。"""
        if baseline is None:
            return GateDecision(
                allowed=True,
                reasons=["无基线（首次评测），默认放行并以此作为新基线"],
                current_score=current.score,
                baseline_score=0.0,
                delta=current.score,
            )

        delta = round(current.score - baseline.score, 4)
        reasons: list[str] = []

        # ① 整体得分退化
        if delta < -abs(max_regression):
            reasons.append(
                f"整体加权通过率退化 {delta:+.4f}，超过允许幅度 {max_regression}"
            )

        # ② 具体用例回归：新失败的用例必须逐条列出（否则无人能定位）
        base_map = {c.case_id: c for c in baseline.case_results}
        regressions = [
            c.case_id
            for c in current.case_results
            if c.case_id in base_map and not c.passed and base_map[c.case_id].passed
        ]
        if regressions:
            reasons.append(f"新失败用例：{regressions}")

        # ③ 合规红线：只要出现就拦，不看总分
        violations = [c.case_id for c in current.case_results if c.violations]
        if violations:
            reasons.append(f"命中合规红线，直接拦截：{violations}")

        allowed = not reasons
        if allowed:
            reasons.append("未发现退化，允许发布")

        return GateDecision(
            allowed=allowed,
            reasons=reasons,
            current_score=current.score,
            baseline_score=baseline.score,
            delta=delta,
            regressions=regressions,
        )

    @staticmethod
    def baseline_from_db(store: Any, scene: str = "all") -> EvalReport | None:
        """从库里取最近一次评测作为基线（CI 里就是"上一次主干的结果"）。

        取的是**完整报告**（含逐条用例），这样退化时能定位到具体哪条用例。
        """
        if store is None or not hasattr(store, "latest_eval_report"):
            return None
        return store.latest_eval_report(scene)
