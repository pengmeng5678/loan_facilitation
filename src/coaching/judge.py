"""四维评分（LLM-as-Judge 的工程化实现）。

四个维度与为什么单列（对应简历项目三）：
    合规红线  有无"包过/不看征信"等违规承诺 —— **一票否决项**，助贷监管刚需
    需求挖掘  有没有问到额度/用途/资质/征信 —— 决定方案能否配
    异议处理  面对"利率太高""再考虑考虑"的应对 —— 成单关键能力
    邀约促成  有没有明确推进到店动作 —— 到店是成交前置

两层实现：
    第一层（确定性 rubric）—— 关键词与槽位覆盖度计算，**永远执行**。
        合规判定必须走这一层：它是可审计的业务规则，不能交给模型拍脑袋。
    第二层（LLM Judge）—— 在接了真实模型时叠加，负责"语义层面的表达质量"评分。
        两层按比例融合；但**合规红线始终以确定性结果为准**。

偏差校准：
    LLM Judge 最大的风险是系统性偏差（偏好长回答、偏好顺从）。
    所以提供 calibrate()：拿 Judge 分与人工专家分做双盲比对，输出一致率与各维度偏差，
    用于迭代评分 Prompt —— 这就是简历里"一致率从 81% 校准到 94%"的方法论落地。
"""

from __future__ import annotations

import re
from statistics import mean

from agent_platform.llm.base import LLMProvider, Message
from agent_platform.logging_config import get_logger
from coaching.domain import (
    DIMENSION_WEIGHTS,
    DimensionScore,
    RoleplaySession,
    ScoreCard,
    Violation,
    now_str,
)

logger = get_logger(__name__)

# --------------------------------------------------------------------------- #
# 违规承诺词库（合规红线）。
# 说明：这是"规则引擎粗筛"，保证召回；语义层的漏网由 LLM 精判补（两段式）。
# --------------------------------------------------------------------------- #
VIOLATION_PATTERNS: list[tuple[str, str]] = [
    (r"包过|包批|包下款|保过", "承诺包过"),
    (r"保证.{0,4}(下款|批|通过|放款)", "保证下款/保证批贷"),
    (r"一定.{0,4}(能批|批下来|下款|能过|通过)", "承诺一定批贷"),
    (r"百分百|100\s*%|百分之百", "百分百承诺"),
    (r"不看征信|无视征信|不看查询", "承诺不看征信"),
    (r"必过|稳下款|稳批|肯定能过", "绝对化承诺"),
    (r"绝对.{0,3}(能|可以)(批|下款|办)", "绝对化承诺"),
    (r"零成本|不要利息|免息", "虚假利率宣传"),
]

# 需求挖掘要覆盖的四个槽位
_NEED_SLOTS: dict[str, tuple[str, ...]] = {
    "资质": ("公积金", "社保", "房产", "房子", "车产", "保单", "营业执照", "执照", "收入", "缴存"),
    "征信": ("征信", "查询", "逾期", "负债", "网贷", "信用卡", "贷款笔数"),
    "用途": ("用途", "做什么用", "资金用途", "用来", "需求", "借来"),
    "额度": ("多少额度", "需要多少", "想贷多少", "金额", "要多少"),
}

# 客户异议的信号词
_OBJECTION_MARKERS = (
    "考虑", "别家", "太高", "太贵", "能不能保证", "骗人", "忙", "没时间",
    "白跑", "不放心", "再想想", "对比", "凭什么", "不信",
)

# 合规表述（业务员做出正确风险提示的信号）
_COMPLIANCE_MARKERS = ("以银行审批为准", "不承诺", "不保证", "无法保证", "预估", "仅供参考")
_EMPATHY_MARKERS = ("理解", "明白您", "我理解", "确实", "担心是正常的")

# 邀约信号
# 邀约信号（提到具体星期几本身就是强邀约信号）
_INVITE_MARKERS = (
    "到店", "面签", "来一趟", "过来", "上门", "线下", "约个时间", "方便过来", "到我们这",
    "来店", "店里", "面谈", "来我们", "到公司",
    "周一", "周二", "周三", "周四", "周五", "周六", "周日", "周天",
)
_INVITE_SPECIFIC_MARKERS = ("周六", "周日", "周天", "明天", "后天", "上午", "下午", "几点", "这周", "下周")


class Judge:
    """四维评分器。"""

    def __init__(self, llm: LLMProvider | None = None, *, llm_weight: float = 0.5) -> None:
        self.llm = llm
        self.llm_weight = llm_weight
        self._use_llm = llm is not None and getattr(llm, "name", "echo") != "echo"

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def score_session(self, session: RoleplaySession) -> ScoreCard:
        """对一次陪练会话打分。"""
        compliance, violations = self._score_compliance(session)
        needs = self._score_needs(session)
        objection = self._score_objection(session)
        invite = self._score_invite(session)

        dimensions = [compliance, needs, objection, invite]
        source = "rules"
        degraded = False

        # ---- 第二层：LLM Judge 融合（合规维度不参与融合，保持确定性）----
        if self._use_llm:
            llm_dims, llm_degraded = self._score_with_llm(session)
            degraded = llm_degraded
            if llm_dims:
                merged: list[DimensionScore] = []
                for dim in dimensions:
                    if dim.dimension == "合规红线":
                        merged.append(dim)
                        continue
                    llm_value = llm_dims.get(dim.dimension)
                    if llm_value is None:
                        # 缺维度/越界的维度保持规则分原样，并在备注里说明降级原因
                        #——评分卡要能自己解释"这一维为什么没有融合模型意见"
                        merged.append(
                            dim.model_copy(
                                update={"notes": dim.notes + "（LLM Judge 输出异常，已回退规则分）"}
                            )
                        )
                        continue
                    blended = (1 - self.llm_weight) * dim.score + self.llm_weight * llm_value
                    merged.append(
                        dim.model_copy(
                            update={
                                "score": round(blended, 3),
                                "passed": blended >= 0.6,
                                "notes": dim.notes + "（已与 LLM Judge 融合）",
                            }
                        )
                    )
                dimensions = merged
                source = "blended"

        total = sum(d.score * d.weight for d in dimensions) * 100
        vetoed = bool(violations)
        if vetoed:
            # 一票否决：合规越线时总分压到 60 以下且直接判不通过
            total = min(total, 59.0)

        weak_points, recommendations = self._build_feedback(dimensions, violations)

        card = ScoreCard(
            session_id=session.session_id,
            persona_id=session.persona.persona_id,
            trainee=session.trainee,
            total_score=round(total, 1),
            passed=(not vetoed) and total >= 60,
            vetoed=vetoed,
            dimensions=dimensions,
            violations=violations,
            weak_points=weak_points,
            recommendations=recommendations,
            judge_source=source,
            degraded=degraded,
            created_at=now_str(),
        )
        logger.info(
            "评分完成 session=%s 总分=%.1f 通过=%s 违规=%d 降级=%s",
            session.session_id, card.total_score, card.passed, len(violations), degraded,
        )
        return card

    # ------------------------------------------------------------------ #
    # 维度一：合规红线（一票否决）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _score_compliance(session: RoleplaySession) -> tuple[DimensionScore, list[Violation]]:
        violations: list[Violation] = []
        evidence: list[str] = []

        for idx, msg in enumerate(session.messages):
            if not msg.is_business():
                continue
            for pattern, label in VIOLATION_PATTERNS:
                match = re.search(pattern, msg.text)
                if match:
                    violations.append(
                        Violation(
                            keyword=match.group(0),
                            excerpt=msg.text[:80],
                            message_index=idx,
                            severity="high",
                            rule_id=label,
                        )
                    )
            if any(m in msg.text for m in _COMPLIANCE_MARKERS):
                evidence.append(f"主动风险提示：{msg.text[:50]}")

        # 做对的部分（有风险提示）作为正向证据；无违规即满分
        score = 0.0 if violations else 1.0
        notes = (
            f"检出 {len(violations)} 处违规承诺（一票否决）"
            if violations
            else "未发现违规承诺，合规红线通过"
        )
        return (
            DimensionScore(
                dimension="合规红线",
                score=score,
                weight=DIMENSION_WEIGHTS["合规红线"],
                passed=not violations,
                evidence=evidence[:3],
                notes=notes,
            ),
            violations,
        )

    # ------------------------------------------------------------------ #
    # 维度二：需求挖掘
    # ------------------------------------------------------------------ #
    @staticmethod
    def _score_needs(session: RoleplaySession) -> DimensionScore:
        covered: list[str] = []
        missing: list[str] = []
        evidence: list[str] = []

        for slot, keys in _NEED_SLOTS.items():
            hit_msg = next((m.text for m in session.business_messages if any(k in m.text for k in keys)), None)
            if hit_msg:
                covered.append(slot)
                evidence.append(f"{slot}：{hit_msg[:40]}")
            else:
                missing.append(slot)

        score = len(covered) / len(_NEED_SLOTS)
        notes = (
            f"覆盖 {len(covered)}/4 个关键槽位"
            + (f"，缺失：{'、'.join(missing)}" if missing else "")
        )
        return DimensionScore(
            dimension="需求挖掘",
            score=round(score, 3),
            weight=DIMENSION_WEIGHTS["需求挖掘"],
            passed=score >= 0.5,
            evidence=evidence,
            notes=notes,
        )

    # ------------------------------------------------------------------ #
    # 维度三：异议处理
    # ------------------------------------------------------------------ #
    @staticmethod
    def _score_objection(session: RoleplaySession) -> DimensionScore:
        messages = session.messages
        objections: list[int] = [
            i for i, m in enumerate(messages)
            if m.role == "customer" and any(k in m.text for k in _OBJECTION_MARKERS)
        ]

        if not objections:
            # 没有异议机会：不奖励也不惩罚，给中性偏上分
            return DimensionScore(
                dimension="异议处理",
                score=0.6,
                weight=DIMENSION_WEIGHTS["异议处理"],
                passed=True,
                evidence=[],
                notes="本场未出现明显客户异议，按中性分计算",
            )

        evidence: list[str] = []
        scores: list[float] = []
        for idx in objections:
            reply = next(
                (m.text for m in messages[idx + 1 : idx + 3] if m.is_business()),
                "",
            )
            if not reply:
                scores.append(0.0)  # 客户提了异议但业务员没回应
                continue

            if any(re.search(p, reply) for p, _ in VIOLATION_PATTERNS):
                scores.append(0.0)  # 用违规承诺"糊弄"异议 → 0 分
                evidence.append(f"异议用违规承诺应对：{reply[:40]}")
                continue

            has_ack = any(m in reply for m in _EMPATHY_MARKERS)
            has_compliance = any(m in reply for m in _COMPLIANCE_MARKERS)
            has_explain = len(reply) >= 20

            value = 0.3
            if has_ack:
                value += 0.25
            if has_compliance:
                value += 0.25
            if has_explain:
                value += 0.2
            scores.append(min(1.0, value))
            evidence.append(f"异议回应：{reply[:40]}")

        score = mean(scores)
        return DimensionScore(
            dimension="异议处理",
            score=round(score, 3),
            weight=DIMENSION_WEIGHTS["异议处理"],
            passed=score >= 0.6,
            evidence=evidence[:3],
            notes=f"共 {len(objections)} 处客户异议，平均应对得分 {score:.2f}",
        )

    # ------------------------------------------------------------------ #
    # 维度四：邀约促成
    # ------------------------------------------------------------------ #
    @staticmethod
    def _score_invite(session: RoleplaySession) -> DimensionScore:
        generic = [m.text for m in session.business_messages if any(k in m.text for k in _INVITE_MARKERS)]
        specific = [t for t in generic if any(k in t for k in _INVITE_SPECIFIC_MARKERS)]

        if specific:
            score, notes = 1.0, "有明确到店邀约（含具体时间），促成动作到位"
        elif generic:
            score, notes = 0.6, "有邀约但不够具体，缺少时间锚点（如'周六下午'）"
        else:
            score, notes = 0.0, "全流程未出现到店邀约动作，客户无法被推进到店"

        return DimensionScore(
            dimension="邀约促成",
            score=score,
            weight=DIMENSION_WEIGHTS["邀约促成"],
            passed=score >= 0.6,
            evidence=(specific or generic)[:2],
            notes=notes,
        )

    # ------------------------------------------------------------------ #
    # LLM Judge 增强
    # ------------------------------------------------------------------ #
    def _score_with_llm(self, session: RoleplaySession) -> tuple[dict[str, float], bool]:
        """让模型对三个非合规维度打分（0-1）。

        返回 (维度分, degraded)。degraded=True 表示模型输出不可信，已按维度回退规则分：
        模型只做语义层的锦上添花，评分口径永远以可审计的确定性结果兜底，
        绝不能让一次乱码输出把评分卡带崩（或更糟：静默算出一个错分）。
        """
        transcript = "\n".join(
            f"{'业务员' if m.role == 'business' else '客户'}：{m.text}" for m in session.messages
        )
        schema = {
            "type": "object",
            "properties": {
                "需求挖掘": {"type": "number", "description": "0-1，是否问到额度/用途/资质/征信"},
                "异议处理": {"type": "number", "description": "0-1，面对客户异议的应对质量"},
                "邀约促成": {"type": "number", "description": "0-1，是否明确推进到店"},
                "理由": {"type": "string"},
            },
            "required": ["需求挖掘", "异议处理", "邀约促成"],
        }
        system = (
            "你是助贷销售培训的评分专家。请严格按 rubric 给业务员的表现打分（0-1）：\n"
            "需求挖掘：是否主动问到资质、征信、用途、额度四类信息（全覆盖=1）。\n"
            "异议处理：客户提出异议时，是否先共情、再解释、并给出合规的风险提示（不承诺下款）。\n"
            "邀约促成：是否明确提出到店/面签邀约，且给出具体时间锚点。\n"
            "注意：只评这三项，不要因为回答长就给高分。"
        )
        try:
            payload = self.llm.complete_json(
                [
                    Message(role="system", content=system),
                    Message(role="user", content=f"对话记录：\n{transcript}"),
                ],
                schema,
            )
        except Exception as exc:  # noqa: BLE001
            # complete_json 内部已按"去 ``` 围栏 → 截取 {...} 子串"两级容错（llm/base.py），
            # 走到这里说明输出是彻底的乱码，只能整体回退规则分
            logger.warning("LLM Judge 输出无法解析（整体回退规则分）：%s", exc)
            return {}, True

        if not isinstance(payload, dict):
            # 合法 JSON 但不是对象（比如模型吐了个数组），按整体失败处理
            logger.warning("LLM Judge 返回的不是 JSON 对象（整体回退规则分）：%r", payload)
            return {}, True

        result: dict[str, float] = {}
        degraded = False
        for key in ("需求挖掘", "异议处理", "邀约促成"):
            value = payload.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                # bool 是 int 子类，true/false 混进来会静默变成 1.0/0.0，必须按缺维度处理
                logger.warning("LLM Judge 缺少维度「%s」，该维度回退规则分", key)
                degraded = True
                continue
            if not 0.0 <= float(value) <= 1.0:
                # 越界说明模型没遵守 0-1 口径，数值不可信：丢弃回退规则分，不钳制后混入总分
                #（顺带兜住 NaN/Inf——它们与任何区间比较都是 False）
                logger.warning("LLM Judge 维度「%s」分数越界（%r），该维度回退规则分", key, value)
                degraded = True
                continue
            result[key] = float(value)
        return result, degraded

    # ------------------------------------------------------------------ #
    # 反馈生成
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_feedback(
        dimensions: list[DimensionScore], violations: list[Violation]
    ) -> tuple[list[str], list[str]]:
        weak: list[str] = []
        recs: list[str] = []

        for dim in dimensions:
            if dim.score >= 0.8:
                continue
            weak.append(f"{dim.dimension}：{dim.notes}")
            if dim.dimension == "合规红线":
                recs.append("重学《合规话术红线》：禁止出现'包过/保证下款/不看征信'，改用'以银行审批为准'")
            elif dim.dimension == "需求挖掘":
                recs.append("练习'资质-征信-用途-额度'四连问，确保方案匹配前信息完整")
            elif dim.dimension == "异议处理":
                recs.append("练习'共情 + 事实解释 + 合规提示'三段式回应，例如'我理解您的顾虑…'")
            elif dim.dimension == "邀约促成":
                recs.append("每次沟通结尾给出具体到店时间建议（如'周六上午 10 点方便吗'）")

        if violations:
            examples = "、".join(f"『{v.keyword}』" for v in violations[:3])
            weak.insert(0, f"出现违规承诺：{examples}（一票否决）")
            recs.insert(0, "违规承诺是监管红线，本场判定不通过，须重练并通过合规考核后方可上岗")

        if not weak:
            weak.append("无明显薄弱项，继续保持")
        return weak, recs

    # ------------------------------------------------------------------ #
    # 偏差校准（双盲）
    # ------------------------------------------------------------------ #
    @staticmethod
    def calibrate(
        judge_scores: list[float],
        human_scores: list[float],
        *,
        tolerance: float = 10.0,
    ) -> dict:
        """用人工专家分校准 Judge 的一致性。

        参数：
            judge_scores: Judge 打的百分制分数
            human_scores: 人工专家在**不知情**下独立打的分数（双盲）
            tolerance:    视为"一致"的容差（分）

        返回：一致率、平均偏差、偏差方向，用于迭代评分 Prompt。
        """
        if len(judge_scores) != len(human_scores) or not judge_scores:
            raise ValueError("judge_scores 与 human_scores 长度必须一致且非空")

        diffs = [j - h for j, h in zip(judge_scores, human_scores, strict=True)]
        within = sum(1 for d in diffs if abs(d) <= tolerance)
        agreement = within / len(diffs)

        return {
            "samples": len(diffs),
            "tolerance": tolerance,
            "agreement_rate": round(agreement, 4),
            "mean_bias": round(mean(diffs), 3),
            "bias_direction": "judge偏宽松" if mean(diffs) > 1 else ("judge偏严格" if mean(diffs) < -1 else "无明显偏差"),
            "max_abs_diff": round(max(abs(d) for d in diffs), 2),
        }
