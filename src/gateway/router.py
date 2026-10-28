"""复杂度路由（Complexity Routing）—— 成本治理三板斧之三。

核心思想：**别用小推车拉集装箱，也别用卡车送一封信**。
    "客户是想问公积金利率吗" 这种轻任务，用大模型纯属浪费；
    而"对比三套方案并给出风险提示"这种复杂推理，用小模型一定翻车。

所以路由的关键不是"省钱"，而是**在不降低质量的前提下省钱**。为此有两条硬约束：
    ① 路由规则**确定性**（不调模型来判断，否则又多一次成本且不可复现）；
    ② 关键任务有**档位保底**（如政策问答不低于 medium）—— 为省钱把质量
       搞崩是本末倒置，这条比省钱重要。
"""

from __future__ import annotations

from typing import Any

from agent_platform.llm.base import Message, estimate_tokens
from gateway.domain import ModelTier, RouteDecision

# --------------------------------------------------------------------------- #
# 复杂度信号
# --------------------------------------------------------------------------- #
# 出现这些词，往往意味着"需要跨信息推理"，而不是"查一条事实"
_REASONING_MARKERS: tuple[str, ...] = (
    "分析", "对比", "比较", "权衡", "推理", "为什么", "论证", "评估", "评测",
    "综合", "规划", "多步", "分别", "逐条", "取舍", "风险提示", "优化建议", "方案对比",
)

# 任务先验复杂度：不同业务任务的"天然难度"不同。
# 这是可调参数，不是玄学 —— 上线后按实际 token 分布校准即可。
_TASK_PRIOR: dict[str, float] = {
    "policy_qa": 0.18,       # 检索 + 引用，轻
    "match": 0.30,           # 多条件筛选，中
    "proposal": 0.45,        # 组合 + 生成，偏重
    "roleplay": 0.35,        # 多轮扮演，中
    "judge": 0.50,           # 四维评分 + 证据，重
    "material": 0.30,        # 素材生成，中
    "compliance": 0.20,      # 规则为主，轻
    "activation": 0.30,      # 个性化 + 节奏，中
    "eval": 0.60,            # 评测与判断，最重
    "default": 0.20,
}

# 档位保底：这些任务的**质量**比省钱重要，不允许被路由降级到 small。
_TASK_FLOOR: dict[str, ModelTier] = {
    "policy_qa": "medium",   # 利率/额度答错是事故
    "proposal": "medium",    # 方案书要发给客户
    "judge": "medium",       # 评分要有依据
    "material": "medium",    # 素材会公开发布，代表公司形象
    "activation": "medium",  # 直接触达客户，措辞不当会客诉
    "eval": "medium",
}

_TIER_ORDER: dict[ModelTier, int] = {"small": 0, "medium": 1, "large": 2}


class ComplexityRouter:
    """确定性复杂度路由。"""

    def __init__(
        self,
        *,
        threshold_medium: float = 0.35,
        threshold_large: float = 0.70,
        tier_models: dict[str, str] | None = None,
        fallback_enabled: bool = True,
    ) -> None:
        self.threshold_medium = threshold_medium
        self.threshold_large = threshold_large
        self.tier_models = tier_models or {
            "small": "qwen-turbo",
            "medium": "qwen-plus",
            "large": "qwen-max",
        }
        self.fallback_enabled = fallback_enabled

    # ------------------------------------------------------------------ #
    # 复杂度打分
    # ------------------------------------------------------------------ #
    def score(self, messages: list[Message], *, task: str = "") -> tuple[float, dict[str, Any]]:
        """返回 (复杂度得分, 各信号明细)。

        得分是**加权求和**，权重与公式都写在这里，可复现、可解释、可校准。
        """
        signals: dict[str, Any] = {}
        total_chars = sum(len(m.content) for m in messages)
        prompt_tokens = estimate_tokens("\n".join(m.content for m in messages))
        text = "\n".join(m.content for m in messages)

        # ① 体量：prompt 越大越可能复杂（封顶 0.30）
        size_score = min(0.30, total_chars / 6000 * 0.30)
        signals["size"] = round(size_score, 3)

        # ② 长文档：单条消息很长 → 像"给你一份材料让你读"（+0.25）
        longest = max((len(m.content) for m in messages), default=0)
        doc_score = 0.25 if longest >= 1500 else 0.0
        signals["long_document"] = doc_score

        # ③ 推理词：命中越多越像复杂任务（每个 0.06，封顶 0.30）
        hits = [w for w in _REASONING_MARKERS if w in text]
        reason_score = min(0.30, len(hits) * 0.06)
        signals["reasoning_markers"] = hits
        signals["reasoning_score"] = round(reason_score, 3)

        # ④ 任务先验：天然难度
        prior = _TASK_PRIOR.get(task, _TASK_PRIOR["default"])
        signals["task_prior"] = prior

        # ⑤ 多轮上下文：轮次越多，越需要在长历史里保持一致性
        turns = sum(1 for m in messages if m.role == "user")
        turns_score = min(0.15, max(0, turns - 1) * 0.05)
        signals["turns"] = turns
        signals["turns_score"] = round(turns_score, 3)

        complexity = round(size_score + doc_score + reason_score + prior + turns_score, 4)
        signals["prompt_tokens"] = prompt_tokens
        return complexity, signals

    # ------------------------------------------------------------------ #
    # 路由
    # ------------------------------------------------------------------ #
    def route(
        self,
        messages: list[Message],
        *,
        task: str = "",
        force_tier: ModelTier | None = None,
    ) -> RouteDecision:
        """给出本轮请求应该走哪个档位。"""
        complexity, signals = self.score(messages, task=task)

        if force_tier is not None:
            tier: ModelTier = force_tier
            reason = f"调用方强制指定 {tier}"
            forced = True
        else:
            if complexity < self.threshold_medium:
                tier = "small"
            elif complexity < self.threshold_large:
                tier = "medium"
            else:
                tier = "large"
            reason = (
                f"复杂度 {complexity:.3f} "
                f"（<{self.threshold_medium}→small / <{self.threshold_large}→medium / else→large）"
            )
            forced = False

            # 档位保底：关键任务不允许为了省钱降级
            floor = _TASK_FLOOR.get(task)
            if floor and _TIER_ORDER[tier] < _TIER_ORDER[floor]:
                reason += f"；任务 {task} 有档位保底 {floor}，已上调"
                tier = floor

        model = self.tier_models.get(tier, tier)
        return RouteDecision(
            tier=tier,
            model=model,
            complexity=complexity,
            reason=reason,
            signals=signals,
            forced=forced,
            fallback_chain=self.fallback_chain(tier),
        )

    def fallback_chain(self, tier: ModelTier) -> list[str]:
        """降级顺序：模型不可用时依次尝试。

        规则：**只能往下退，绝不能往上顶**。
        往上顶会瞬间打爆成本（medium 比 small 贵好几倍，large 更夸张），
        而且多半也解决不了问题 —— 失败原因通常是"上游挂了"或"配额用尽"，
        不是"模型太弱"。

        所以 small 档没有后备：小档失败就失败（让调用方决定重试还是升级），
        绝不悄悄升档。要升档必须由调用方**显式**指定 tier ——
        这样"成本超预期"永远能追溯到一次明确的决定，而不是一次隐式兜底。
        """
        if not self.fallback_enabled:
            return [self.tier_models.get(tier, tier)]
        order: dict[ModelTier, list[ModelTier]] = {
            "large": ["large", "medium", "small"],
            "medium": ["medium", "small"],
            "small": ["small"],
        }
        return [self.tier_models.get(t, t) for t in order.get(tier, [tier])]
