"""Judge：把"回答好不好"变成可计算的分数。

分两层，缺一不可：
    **底层 rubric（确定性）** —— 关键词命中、禁止词、拒答正确性。
        优点是零成本、可复现、可解释，且**合规红线可以一票否决**。
        这是评测的主干：任何与上一次的结果差异都能追溯到具体用例。

    **上层 LLM-as-Judge（可选）** —— 对"措辞是否得体、是否答到点子上"
        这类主观质量补充打分。但必须知道它的偏差来源（偏好长回答、
        位置偏见），所以配套 **双盲人工抽检校准**（见 calibrate）。

判分顺序很重要：**先查禁止词**。命中合规红线直接 0 分，
不给"其他维度表现好"任何翻盘机会 —— 与项目三"合规一票否决"同一逻辑。
"""

from __future__ import annotations

import json
from statistics import mean

from agent_platform.llm.base import LLMProvider, Message
from agent_platform.logging_config import get_logger
from gateway.domain import CaseResult, EvalCase

logger = get_logger(__name__)


class Judge:
    """评测判分器。"""

    def __init__(self, llm: LLMProvider | None = None, *, use_llm: bool = True) -> None:
        self.llm = llm
        self.use_llm = use_llm and llm is not None

    # ------------------------------------------------------------------ #
    # 单条判分
    # ------------------------------------------------------------------ #
    def score_case(self, case: EvalCase, actual: dict) -> CaseResult:
        """对一条用例判分。

        actual 约定字段：
            answer   str   模型/系统的实际输出
            refused  bool  是否执行了拒答
            extra    dict  附带信息（路由档位、工具调用等）
        """
        answer = str(actual.get("answer") or "")
        refused = bool(actual.get("refused"))
        preview = answer[:160].replace("\n", " ")

        # ---- ① 合规红线：一票否决 ----
        violations = [w for w in case.forbidden_keywords if w in answer]
        if violations:
            return CaseResult(
                case_id=case.case_id,
                scene=case.scene,
                passed=False,
                score=0.0,
                violations=violations,
                actual_preview=preview,
                reason=f"命中合规红线词：{violations}",
                judge_source="rules",
            )

        # ---- ② 结构化断言：有确定预期的场景（如"轻任务必须路由到 small 档"）----
        # 为什么要有这一条：像成本路由这类需求，"答案好不好"根本不是重点，
        # 重点是**行为有没有回退**（有人把阈值一改，所有请求都走大模型，成本翻十倍，
        # 而关键词判分完全看不出来）。所以对这类场景直接断言结构化字段。
        expect_tier = (case.context or {}).get("expect_tier")
        if expect_tier:
            actual_tier = str((actual.get("extra") or {}).get("tier") or "")
            passed = actual_tier == str(expect_tier)
            return CaseResult(
                case_id=case.case_id,
                scene=case.scene,
                passed=passed,
                score=1.0 if passed else 0.0,
                actual_preview=preview,
                reason="" if passed else f"路由档位不符：期望 {expect_tier}，实际 {actual_tier or '未知'}",
                judge_source="rules",
            )

        # ---- ③ 拒答正确性 ----
        if case.must_refuse:
            passed = refused
            return CaseResult(
                case_id=case.case_id,
                scene=case.scene,
                passed=passed,
                score=1.0 if passed else 0.0,
                actual_preview=preview,
                reason="" if passed else "该拒答但未拒答（疑似幻觉/编造）",
                judge_source="rules",
            )

        # ---- ④ 关键词命中 ----
        hits = [k for k in case.expect_keywords if k in answer]
        misses = [k for k in case.expect_keywords if k not in answer]

        if case.expect_keywords:
            if case.expect_mode == "any":
                # 任一命中即"该事实已说到"。这里**不给部分分**：expect_mode=any
                # 的场景里，多个关键词通常是同一事实的不同写法（如 600000 / 600,000），
                # 按命中率算分会让一个完全正确的回答只拿 0.5 分 —— 分数就失真了。
                coverage = 1.0 if hits else 0.0
                rule_passed = bool(hits)
            else:
                # expect_mode=all：每一项都必需，命中率=完成度。
                # 这样"只答了一半"能直接从得分上看出来（0.5 而不是 0 或 1）。
                coverage = len(hits) / len(case.expect_keywords)
                rule_passed = not misses
        else:
            coverage = 1.0 if answer.strip() else 0.0
            rule_passed = bool(answer.strip())

        score = round(coverage, 4)
        reason = "" if rule_passed else f"未命中期望关键词：{misses}"
        judge_source = "rules"

        # ---- ⑤ LLM-as-Judge 补充（可选，语义质量层）----
        if self.use_llm and case.reference:
            llm_score, llm_pass, llm_reason = self._llm_judge(case, answer)
            if llm_score is not None:
                score = round(0.5 * score + 0.5 * llm_score, 4)
                rule_passed = rule_passed and llm_pass
                judge_source = "blended"
                reason = reason or ("" if llm_pass else llm_reason)

        return CaseResult(
            case_id=case.case_id,
            scene=case.scene,
            passed=rule_passed,
            score=score,
            hits=hits,
            misses=misses,
            actual_preview=preview,
            reason=reason,
            judge_source=judge_source,
        )

    def _llm_judge(self, case: EvalCase, answer: str) -> tuple[float | None, bool, str]:
        """让模型按参考回答给一个 0-1 的语义质量分。失败则退化为纯 rubric。"""
        assert self.llm is not None
        schema = {
            "type": "object",
            "properties": {
                "score": {"type": "number", "description": "0-1，回答与参考的语义一致度"},
                "pass": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["score", "pass", "reason"],
        }
        system = (
            "你是严格的评测裁判。判断【待评回答】与【参考回答】在**关键事实上是否一致**，"
            "不要因为措辞、长度不同而扣分。返回 JSON：score(0-1)、pass(bool)、reason。"
        )
        user = (
            f"问题：{case.question}\n参考回答：{case.reference}\n"
            f"待评回答：{answer}\n请打分。"
        )
        try:
            data = self.llm.complete_json(
                [Message(role="system", content=system), Message(role="user", content=user)],
                schema,
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001 - 判官失败不能让整轮评测崩
            logger.warning("LLM 判官调用失败，退化为规则判分：%s", exc)
            return None, True, ""

        try:
            score = float(data.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(1.0, score))
        return score, bool(data.get("pass", score >= 0.6)), str(data.get("reason", ""))

    # ------------------------------------------------------------------ #
    # 双盲校准
    # ------------------------------------------------------------------ #
    @staticmethod
    def calibrate(
        judge_scores: list[float], human_scores: list[float], *, tolerance: float = 10.0
    ) -> dict:
        """Judge 与人工专家的一致性校准。

        做法：同一批样本，Judge 打一次分、人工专家盲打一次分（互不可见），
        比对**一致率**（差值在容差内）与**平均绝对偏差**。

        为什么必须做：LLM-as-Judge 有系统性偏差（偏好长回答、位置偏见）。
        不做校准就把它当基准，等于用一个有偏的尺子去卡发布门槛。
        """
        if len(judge_scores) != len(human_scores):
            raise ValueError("judge_scores 与 human_scores 长度必须一致")
        if not judge_scores:
            return {"samples": 0, "agreement_rate": 0.0, "mean_abs_diff": 0.0, "tolerance": tolerance}

        diffs = [abs(j - h) for j, h in zip(judge_scores, human_scores, strict=True)]
        agree = [d for d in diffs if d <= tolerance]
        return {
            "samples": len(diffs),
            "agreement_rate": round(len(agree) / len(diffs), 4),
            "mean_abs_diff": round(mean(diffs), 2),
            "max_abs_diff": round(max(diffs), 2),
            "tolerance": tolerance,
            "passed": len(agree) / len(diffs) >= 0.8,
        }

    @staticmethod
    def dump_case_result(result: CaseResult) -> str:
        return json.dumps(result.model_dump(), ensure_ascii=False)
