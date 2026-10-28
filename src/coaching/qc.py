"""全量会话质检引擎。

对应简历项目三的"质检覆盖率从抽查 5% 到全量 100%，问题发现从'客诉后'前移到'发生时'"。

两段式设计（这是控制误报率的关键）：
    第一段：**规则引擎粗筛**（正则 + 结构化规则）—— 保证召回，宁可多报不可漏报；
    第二段：**LLM 精判**（接了真实模型时）—— 降低误报，只对粗筛命中做复核。
    质检结果分级（high/medium/low），**只有 high 才实时告警主管** —— 避免告警疲劳。

同时在输出上强调：
    质检报告给人**证据**（原文 + 位置 + 建议），而不是给人一个分数。
    AI 不做裁决，主管做最终判断（AI 提供证据的原则）。
"""

from __future__ import annotations

import re
from typing import Any

from agent_platform.llm.base import LLMProvider, Message
from agent_platform.logging_config import get_logger
from agent_platform.observability import Metrics
from coaching.domain import QCFinding, QCReport, now_str
from coaching.judge import (
    _COMPLIANCE_MARKERS,
    _INVITE_MARKERS,
    VIOLATION_PATTERNS,
)

logger = get_logger(__name__)

# ---- 规则库 ---------------------------------------------------------------- #
# 敏感信息：业务员把客户的完整手机号/身份证/卡号发出去 = 数据泄露
SENSITIVE_PATTERNS: list[tuple[str, str]] = [
    (r"(?<!\d)1[3-9]\d{9}(?!\d)", "疑似手机号未脱敏"),
    (r"(?<!\w)\d{17}[\dXx](?!\w)", "疑似身份证号未脱敏"),
    (r"(?<!\d)\d{16,19}(?!\d)", "疑似银行卡号未脱敏"),
]

# 资质问询信号（SOP 必备动作）
_QUALIFICATION_MARKERS = ("公积金", "社保", "房产", "房子", "车", "保单", "营业执照", "执照", "收入", "缴存")

# 客户正向反馈信号（用于话术挖掘）
POSITIVE_MARKERS = ("好的", "行", "可以", "明白", "对", "是的", "那我", "我去", "安排了", "有道理", "嗯嗯")

# 响应时效阈值（秒）：客户发消息后超过该时长才回复视为服务时效问题
RESPONSE_TIME_LIMIT_SECONDS = 1800.0

# 扣分规则
SEVERITY_DEDUCTION = {"high": 30.0, "medium": 15.0, "low": 5.0}


class QCEngine:
    """全量质检引擎。"""

    def __init__(
        self,
        llm: LLMProvider | None = None,
        *,
        response_time_limit: float = RESPONSE_TIME_LIMIT_SECONDS,
        metrics: Metrics | None = None,
    ) -> None:
        self.llm = llm
        self._use_llm = llm is not None and getattr(llm, "name", "echo") != "echo"
        self.response_time_limit = response_time_limit
        self.metrics = metrics or Metrics()

    @staticmethod
    def _add(findings: list[QCFinding], **fields: Any) -> None:
        """追加一条发现，并按 (规则, 消息序号, 本批次序号) 生成稳定 finding_id。

        LLM 精判按 finding_id 回传结论 —— 用 excerpt 当 key 会在模型改写/截断原文时
        全部失配，导致"复核永远等于没复核"。
        """
        fid = f"{fields['rule_id']}#{fields.get('message_index', 0)}#{len(findings)}"
        findings.append(QCFinding(finding_id=fid, **fields))

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def check(self, conversation: dict[str, Any]) -> QCReport:
        """质检一段真实业务对话。"""
        messages: list[dict] = conversation.get("messages", [])
        findings: list[QCFinding] = []
        checked_rules = 0

        # ---- 规则 1：违规承诺（high）----
        checked_rules += 1
        for idx, msg in enumerate(messages):
            if msg.get("role") != "business":
                continue
            for pattern, label in VIOLATION_PATTERNS:
                match = re.search(pattern, msg.get("text", ""))
                if match:
                    self._add(
                        findings,
                        rule_id="QC-001",
                        category="违规承诺",
                        severity="high",
                        message_index=idx,
                        excerpt=msg["text"][:80],
                        detail=f"出现违规表述『{match.group(0)}』（{label}）",
                        suggestion="删除承诺性表述，改为'预估区间 + 以银行审批为准'",
                    )

        # ---- 规则 2：敏感信息未脱敏（high）----
        checked_rules += 1
        for idx, msg in enumerate(messages):
            if msg.get("role") != "business":
                continue
            for pattern, label in SENSITIVE_PATTERNS:
                match = re.search(pattern, msg.get("text", ""))
                if match:
                    self._add(
                        findings,
                        rule_id="QC-002",
                        category="敏感信息",
                        severity="high",
                        message_index=idx,
                        excerpt=_mask(msg["text"][:80], match.group(0)),
                        detail=label,
                        suggestion="敏感信息须脱敏后再发送（如 138****5678）",
                    )

        # ---- 规则 3：响应时效（medium）----
        checked_rules += 1
        findings.extend(self._check_response_time(messages))

        # ---- 规则 4：SOP 完整性（medium/low）----
        checked_rules += 1
        findings.extend(self._check_sop(conversation))

        # ---- 第二段：LLM 精判（复核高风险的违规承诺，降低误报）----
        if self._use_llm and any(f.rule_id == "QC-001" for f in findings):
            findings = self._refine_with_llm(findings, messages)

        # ---- 计分 ----
        score = 100.0
        for f in findings:
            score -= SEVERITY_DEDUCTION.get(f.severity, 5.0)
        score = max(0.0, score)

        report = QCReport(
            conversation_id=str(conversation.get("conversation_id", "unknown")),
            agent=str(conversation.get("agent", "")),
            score=round(score, 1),
            findings=findings,
            checked_rules=checked_rules,
            stats={
                "messages": len(messages),
                "business_messages": sum(1 for m in messages if m.get("role") == "business"),
                "high": sum(1 for f in findings if f.severity == "high"),
                "medium": sum(1 for f in findings if f.severity == "medium"),
                "low": sum(1 for f in findings if f.severity == "low"),
            },
            needs_alert=any(f.severity == "high" for f in findings),
            created_at=now_str(),
        )
        logger.info(
            "质检完成 conv=%s 得分=%.0f 发现=%d 需告警=%s",
            report.conversation_id, report.score, len(findings), report.needs_alert,
        )
        return report

    # ------------------------------------------------------------------ #
    # 子规则
    # ------------------------------------------------------------------ #
    def _check_response_time(self, messages: list[dict]) -> list[QCFinding]:
        """检查是否长时间未回复客户。"""
        findings: list[QCFinding] = []
        for idx, msg in enumerate(messages):
            if msg.get("role") != "customer":
                continue
            # 找客户之后的第一条业务员消息
            nxt = next(
                ((i, m) for i, m in enumerate(messages[idx + 1 :], start=idx + 1)
                 if m.get("role") == "business"),
                None,
            )
            if nxt is None:
                # 客户发完消息就没人回了 —— 漏跟进的典型信号
                self._add(
                    findings,
                    rule_id="QC-003",
                    category="响应时效",
                    severity="medium",
                    message_index=idx,
                    excerpt=msg.get("text", "")[:60],
                    detail="客户消息后无业务员回复",
                    suggestion="建立未回复提醒，超过 2 小时未回必须跟进",
                )
                continue

            next_idx, next_msg = nxt
            gap = float(next_msg.get("ts", 0)) - float(msg.get("ts", 0))
            if gap > self.response_time_limit:
                self._add(
                    findings,
                    rule_id="QC-003",
                    category="响应时效",
                    severity="medium",
                    message_index=idx,
                    excerpt=msg.get("text", "")[:60],
                    detail=f"客户消息后 {gap / 60:.0f} 分钟才回复（阈值 {self.response_time_limit / 60:.0f} 分钟）",
                    suggestion="高峰期使用快捷话术先响应，避免客户流失",
                )
        return findings

    def _check_sop(self, conversation: dict[str, Any]) -> list[QCFinding]:
        """检查必备动作是否缺失（资质问询 / 合规提示 / 邀约）。"""
        messages: list[dict] = conversation.get("messages", [])
        business_texts = [m.get("text", "") for m in messages if m.get("role") == "business"]
        joined = " ".join(business_texts)
        outcome = str(conversation.get("outcome", ""))
        findings: list[QCFinding] = []

        if not any(k in joined for k in _QUALIFICATION_MARKERS):
            self._add(
                findings,
                rule_id="QC-004",
                category="SOP缺失",
                severity="medium" if outcome == "won" else "low",
                message_index=0,
                excerpt=business_texts[0][:60] if business_texts else "",
                detail="全程未做资质问询（公积金/社保/房产/收入等），方案匹配将缺少依据",
                suggestion="按 SOP 完成'资质-征信-用途-额度'四连问",
            )

        if not any(k in joined for k in _COMPLIANCE_MARKERS):
            self._add(
                findings,
                rule_id="QC-005",
                category="SOP缺失",
                severity="medium",
                message_index=0,
                excerpt="",
                detail="全程未做合规风险提示（如'以银行审批为准'）",
                suggestion="涉及利率、额度、通过率时必须追加合规提示",
            )

        if not any(k in joined for k in _INVITE_MARKERS):
            self._add(
                findings,
                rule_id="QC-006",
                category="SOP缺失",
                severity="medium" if outcome == "pending" else "low",
                message_index=0,
                excerpt="",
                detail="未出现到店/面签邀约动作，客户未被推进",
                suggestion="明确给出到店时间建议，推进成交漏斗",
            )

        return findings

    # ------------------------------------------------------------------ #
    # LLM 精判（降低误报）
    # ------------------------------------------------------------------ #
    def _refine_with_llm(self, findings: list[QCFinding], messages: list[dict]) -> list[QCFinding]:
        """对 QC-001（违规承诺）做语义复核：规则命中但语义合规的，降级而非删除。

        为什么要"降级而不是删除"：规则是合规底线，删除会让审计链路断掉；
        降级为 low 并保留证据，人工复核时能看到完整上下文。
        """
        targets = [f for f in findings if f.rule_id == "QC-001"]
        if not targets:
            return findings

        context = "\n".join(m.get("text", "") for m in messages)
        system = (
            "你是助贷合规质检员。下面这段对话中，系统用规则检测出若干'疑似违规承诺'片段。"
            "请判断每一处是否**真的构成违规承诺**（即：明确承诺放款结果、承诺不看征信、"
            "承诺具体利率一定实现）。注意区分'承诺'与'合规的预估表述'。"
            "必须原样回传每条的 finding_id，只输出 JSON。"
        )
        schema = {
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "finding_id": {"type": "string"},
                            "is_violation": {"type": "boolean"},
                            "reason": {"type": "string"},
                        },
                        "required": ["finding_id", "is_violation"],
                    },
                }
            },
            "required": ["results"],
        }
        try:
            payload = self.llm.complete_json(
                [
                    Message(role="system", content=system),
                    Message(
                        role="user",
                        content=f"对话：\n{context}\n\n待复核片段：\n"
                        + "\n".join(f"- [{f.finding_id}] {f.excerpt}" for f in targets),
                    ),
                ],
                schema,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("质检 LLM 复核失败，保留规则原始结论：%s", exc)
            return findings

        verdicts = {
            str(r.get("finding_id", "")): r
            for r in payload.get("results", [])
            if isinstance(r, dict)
        }
        known_ids = {f.finding_id for f in targets}
        unknown_ids = {fid for fid in verdicts if fid and fid not in known_ids}
        if unknown_ids:
            # 模型返回了不存在的 id —— 说明 prompt 与代码又漂了，必须能被观测到
            self.metrics.incr("qc_refine_unmatched_total", value=float(len(unknown_ids)))

        refined: list[QCFinding] = []
        for f in findings:
            if f.rule_id != "QC-001":
                refined.append(f)
                continue
            verdict = verdicts.get(f.finding_id)
            if verdict is None:
                # 未复核成功 = 保留原判（合规场景宁严不宽）
                self.metrics.incr("qc_refine_missing_total")
                refined.append(f)
                continue
            is_violation = bool(verdict.get("is_violation", True))
            reason = str(verdict.get("reason", ""))
            if is_violation:
                refined.append(f)
            else:
                refined.append(
                    f.model_copy(
                        update={
                            "severity": "low",
                            "detail": f.detail + f"｜LLM 复核：语义上不构成违规承诺（{reason}）",
                        }
                    )
                )
        return refined


def _mask(text: str, target: str) -> str:
    """把命中的敏感串在回显中打码，避免报告本身又泄露一次。"""
    if len(target) <= 4:
        return text
    masked = target[:3] + "*" * (len(target) - 5) + target[-2:]
    return text.replace(target, masked)


def build_qc_engine(llm: LLMProvider | None = None) -> QCEngine:
    return QCEngine(llm=llm)
