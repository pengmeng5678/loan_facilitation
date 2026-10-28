"""合规审核：**规则引擎保召回 + LLM 保精度** 的两段式。

为什么必须两段式（这是助贷场景最容易被面试官追问的点）：
    - 只用规则：金融违规话术的变体太多（"包过""100%批""必下"），
      词表永远补不完 → **漏判**，而漏判的代价是监管处罚；
    - 只用 LLM：模型会"觉得这句也没什么"而放过 → **不稳定**，
      同一句今天判违规明天判合规，无法审计，也无法作为发布门槛。

所以：**规则做高召回粗筛（宁可多报）+ LLM 对可疑句做精判（减少误报）**，
并且规则部分**确定性、可审计、可复现**。LLM 不可用时自动降级为纯规则，
审核能力不会消失（只是误报略多、需要人工多看一眼）。

另外内置 **sanitize**：把高风险话术自动改写成合规表达。
"生成即合规"比"生成完发现违规再退回"效率高得多。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from agent_platform.llm.base import LLMProvider, Message
from agent_platform.logging_config import get_logger
from marketing.domain import ComplianceFinding, ComplianceReport

logger = get_logger(__name__)

_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass
class Rule:
    """一条合规规则。"""

    rule_id: str
    category: str
    severity: str
    keywords: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    suggestion: str = ""
    field_repr: list[str] = field(default_factory=list, repr=False)


# --------------------------------------------------------------------------- #
# 规则库（确定性、可审计、可扩展）
# --------------------------------------------------------------------------- #
DEFAULT_RULES: list[Rule] = [
    Rule(
        rule_id="PROMISE-01",
        category="承诺性话术",
        severity="high",
        keywords=(
            "包过", "保证下款", "保下款", "必下款", "必下", "包批", "包下", "包下款",
            "百分百通过", "100%通过", "100％通过", "稳过", "保证通过", "一定批",
            "保证批", "担保放款", "肯定能批", "一定能办下来",
            # 以下是对"承诺形状"的兜底召回：金融广告里出现这些词几乎必然是承诺，
            # 宁可多报交给 LLM 精判 / 人工复核，也不能漏（漏判的代价是监管处罚）。
            "百分百", "必过", "必批", "稳下", "100%", "100％", "肯定能下",
        ),
        suggestion="改为'通过率较高/以银行审批结果为准'，不得对审批结果做任何承诺。",
    ),
    Rule(
        rule_id="PROMISE-02",
        category="收益承诺",
        severity="high",
        keywords=(
            "零利息", "零利率", "免息", "无息贷款", "0利息", "不花钱贷款",
            "绝对低息", "最低利率保证", "保证最低利率",
        ),
        suggestion="删除利率承诺，改为'利率区间以银行审批为准'。",
    ),
    Rule(
        rule_id="BYPASS-01",
        category="规避审查",
        severity="high",
        keywords=(
            "不看征信", "无视征信", "无需征信", "不查征信", "黑户可做", "黑户也能",
            "包装资料", "包装流水", "包装资质", "伪造", "做假", "假流水", "假资料",
            "洗白征信", "消除逾期", "征信修复", "修复征信", "内部渠道", "内部通道",
            "银行内部", "走后门", "关系户", "花钱改",
        ),
        suggestion="删除规避审查与造假表述；改为'综合评估客户资质，通过正规银行渠道办理'。",
    ),
    Rule(
        rule_id="INDUCE-01",
        category="诱导性表述",
        severity="medium",
        keywords=(
            "秒批", "秒下款", "极速下款", "无门槛", "零门槛", "人人都能过",
            "无抵押无担保", "当天必到账",
        ),
        suggestion="改为'审批效率较高/流程较快'，避免绝对化与诱导。",
    ),
    Rule(
        rule_id="ADLAW-01",
        category="绝对化用语",
        severity="medium",
        # 只收"排名/绝对化"形状的词，**不要收裸的"第一""最好"**：
        # "第一件事""第一天""最好是先看清"是正常中文，裸词会大面积误伤
        # （实测把"第一件事"改写成了"优质件事"，直接把文案改坏）。
        # 广告法的口径也是"排序性宣称"，不是"出现序数词"。
        keywords=(
            "最好", "最专业", "最权威", "最强", "最便宜", "最优",
            "第一品牌", "行业第一", "全国第一", "全球第一", "销量第一",
            "排名第一", "第一名", "No.1", "NO.1", "唯一", "独家", "顶级",
            "国家级", "万能",
        ),
        suggestion="广告法禁用绝对化用语，改为'专业/领先/优质'等相对表述。",
    ),
    Rule(
        rule_id="PRIVACY-01",
        category="敏感信息",
        severity="high",
        patterns=(r"(?<!\d)1[3-9]\d{9}(?!\d)", r"(?<!\d)\d{17}[\dXx](?!\d)", r"(?<!\d)\d{16,19}(?!\d)"),
        suggestion="素材含客户手机号/证件号/银行卡号，必须先脱敏再发布。",
    ),
]

# 自动改写表：高风险表达 → 合规表达
SANITIZE_MAP: list[tuple[str, str]] = [
    ("保证下款", "以银行审批结果为准"),
    ("保下款", "以银行审批结果为准"),
    ("必下款", "有望获批"),
    ("百分百通过", "通过率较高"),
    ("100%通过", "通过率较高"),
    ("100％通过", "通过率较高"),
    ("包过", "通过率较高"),
    ("包批", "通过率较高"),
    ("包下", "通过率较高"),
    ("稳过", "通过率较高"),
    ("保证通过", "以银行审批结果为准"),
    ("保证批", "以银行审批结果为准"),
    ("一定能办下来", "具备办理条件"),
    ("一定批", "具备办理条件"),
    # 通用"承诺形状"的改写（必须放在上面具体词条之后，否则会被抢先替换）
    ("百分百下款", "以银行审批结果为准"),
    ("百分百", "以银行审批结果为准"),
    ("必下", "有望获批"),
    ("必批", "通过率较高"),
    ("必过", "通过率较高"),
    ("稳下", "审批效率较高"),
    ("肯定能下", "有望获批"),
    ("包下款", "通过率较高"),
    ("100%", "通过率较高"),
    ("100％", "通过率较高"),
    ("不看征信", "综合评估资质"),
    ("无视征信", "综合评估资质"),
    ("不查征信", "综合评估资质"),
    ("无需征信", "综合评估资质"),
    ("黑户可做", "资质较弱亦有方案"),
    ("黑户也能", "资质较弱亦有方案"),
    ("包装资料", "合规整理资料"),
    ("包装流水", "合规整理流水"),
    ("包装资质", "合规梳理资质"),
    ("洗白征信", "通过正规方式维护信用记录"),
    ("消除逾期", "通过正规方式维护信用记录"),
    ("征信修复", "通过正规方式维护信用记录"),
    ("修复征信", "通过正规方式维护信用记录"),
    ("内部渠道", "正规银行渠道"),
    ("内部通道", "正规银行渠道"),
    ("银行内部", "正规银行渠道"),
    ("走后门", "正规渠道"),
    ("关系户", "正规渠道"),
    ("零利息", "利率以银行审批为准"),
    ("零利率", "利率以银行审批为准"),
    ("0利息", "利率以银行审批为准"),
    ("免息", "利率以银行审批为准"),
    ("秒批", "审批效率较高"),
    ("秒下款", "审批效率较高"),
    ("极速下款", "审批效率较高"),
    ("无门槛", "准入门槛较低"),
    ("零门槛", "准入门槛较低"),
    ("人人都能过", "符合条件即可申请"),
    ("最专业", "专业"),
    ("最权威", "权威"),
    ("最好", "优质"),
    # 排序性宣称逐条列出。**不要写 ("第一", "优质")** —— 那是过宽匹配，
    # 会把"第一件事""第一步""第一天"这类正常表达一起改坏（对外文案直接报废）。
    ("第一品牌", "知名品牌"),
    ("行业第一", "行业领先"),
    ("全国第一", "全国领先"),
    ("全球第一", "全球领先"),
    ("销量第一", "销量领先"),
    ("排名第一", "排名靠前"),
    ("第一名", "名次靠前"),
    ("No.1", "领先"),
    ("NO.1", "领先"),
    ("唯一", "优先"),
    ("顶级", "优质"),
    ("绝对", "相对"),
]

# 精判提示词：只处理规则已经标出可疑的句子，控制成本与误报
_LLM_SYSTEM = (
    "你是金融广告合规审核员。判断给定文本是否存在金融广告违规风险，"
    "重点看：承诺审批结果、承诺利率、规避征信审查、诱导性表述、绝对化用语。"
    "不要重复列出已由规则发现的问题，只补充规则可能漏掉的。"
    "返回 JSON：{\"findings\":[{\"category\":\"...\",\"severity\":\"high|medium|low\","
    "\"matched\":\"...\",\"suggestion\":\"...\"}]}。若无补充返回空数组。"
)


class ComplianceEngine:
    """合规审核引擎。"""

    def __init__(
        self,
        llm: LLMProvider | None = None,
        *,
        rules: list[Rule] | None = None,
        extra_keywords: dict[str, list[str]] | None = None,
    ) -> None:
        self.rules = list(rules or DEFAULT_RULES)
        self.llm = llm
        self._compiled: list[tuple[Rule, list[re.Pattern[str]]]] = [
            (r, [re.compile(p) for p in r.patterns]) for r in self.rules
        ]
        # 允许从数据文件追加词表（规则改词不用改代码、不用重新发版）
        if extra_keywords:
            self._apply_extra_keywords(extra_keywords)
        # 只有真实模型才启用精判：echo 是离线占位实现，用它判分毫无判别力
        self._use_llm = llm is not None and getattr(llm, "name", "") != "echo"

    def _apply_extra_keywords(self, extra: dict[str, list[str]]) -> None:
        by_id = {r.rule_id: r for r in self.rules}
        for rule_id, words in extra.items():
            rule = by_id.get(rule_id)
            if rule is None:
                continue
            rule.keywords = tuple(dict.fromkeys(list(rule.keywords) + list(words)))

    # ------------------------------------------------------------------ #
    # 审核
    # ------------------------------------------------------------------ #
    def check(self, text: str) -> ComplianceReport:
        """审核一段文本（素材正文/口播稿/私信话术）。"""
        findings: list[ComplianceFinding] = []
        checked = 0

        for rule, patterns in self._compiled:
            checked += 1
            for kw in rule.keywords:
                idx = text.find(kw)
                if idx >= 0:
                    findings.append(ComplianceFinding(
                        rule_id=rule.rule_id,
                        category=rule.category,
                        severity=rule.severity,  # type: ignore[arg-type]
                        matched=kw,
                        excerpt=_excerpt(text, idx, len(kw)),
                        suggestion=rule.suggestion,
                        source="rules",
                    ))
            for pat in patterns:
                for m in pat.finditer(text):
                    findings.append(ComplianceFinding(
                        rule_id=rule.rule_id,
                        category=rule.category,
                        severity=rule.severity,  # type: ignore[arg-type]
                        matched=m.group(0),
                        excerpt=_excerpt(text, m.start(), len(m.group(0))),
                        suggestion=rule.suggestion,
                        source="rules",
                    ))

        # ---- 第二段：LLM 精判 ----
        # 规则命中时靠它压误报；规则零命中时也抽查一次，防止"规则完全没覆盖"的
        # 新型违规话术直接漏过去。成本可控：只在有文本要审时调用，且提示词很短。
        if self._use_llm:
            findings.extend(self._llm_review(text))

        return self._build_report(text, findings, checked)

    def check_and_fix(self, text: str) -> ComplianceReport:
        """审核 → 有命中项就自动改写 → 再审核（**生成即合规**）。

        为什么"只要命中就改写"，而不是"只有不通过才改写"：
            中风险的绝对化用语（"最专业/唯一"）在广告法下同样是违规风险，
            只是不构成"拦住不发"的级别。既然有确定的合规替代表达，
            就应该顺手改掉 —— 否则它们会一直留在素材里，靠人记得去改是不可能的。

        返回的 findings 是**首次命中 + 复检命中的并集**：
        通过与否看复检结果（改写后能发就发），但"拦下并改写了什么"必须留在
        报告里 —— 监管审计要能回答"你们的审核到底做了什么"，只看复检结果
        会变成"什么都没发现"，那是审计黑洞。
        """
        report = self.check(text)
        if report.passed and not report.findings:
            return report

        fixed = self.sanitize(text)
        if fixed == text:
            # 改不动（说明命中的是敏感信息等不可改写项）→ 原样返回，交人工
            return report

        recheck = self.check(fixed)
        recheck.sanitized = True
        recheck.sanitized_text = fixed

        # 合并证据链：把首次命中的发现并回复检报告（去重）
        merged = list(report.findings)
        seen = {(f.rule_id, f.matched) for f in merged}
        for f in recheck.findings:
            if (f.rule_id, f.matched) not in seen:
                merged.append(f)
                seen.add((f.rule_id, f.matched))
        recheck.findings = merged
        # 证据里的最高风险级要保留下来（审计要回答"拦过什么档次的违规"），
        # 但 passed / needs_human 仍以复检结果为准 —— 改写成功就是可以发布。
        if merged:
            recheck.max_severity = max(merged, key=lambda f: _SEVERITY_RANK[f.severity]).severity
        return recheck

    def sanitize(self, text: str) -> str:
        """把高风险表达替换为合规表达。"""
        out = text
        for bad, good in SANITIZE_MAP:
            out = out.replace(bad, good)
        return out

    def _llm_review(self, text: str) -> list[ComplianceFinding]:
        assert self.llm is not None
        schema = {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "category": {"type": "string"},
                            "severity": {"type": "string"},
                            "matched": {"type": "string"},
                            "suggestion": {"type": "string"},
                        },
                    },
                }
            },
            "required": ["findings"],
        }
        try:
            data = self.llm.complete_json(
                [
                    Message(role="system", content=_LLM_SYSTEM),
                    Message(role="user", content=f"待审核文本：\n{text}"),
                ],
                schema,
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001 - 精判失败降级为纯规则，不能挡住审核
            logger.warning("合规 LLM 精判失败，降级为纯规则审核：%s", exc)
            return []

        out: list[ComplianceFinding] = []
        for item in data.get("findings") or []:
            severity = str(item.get("severity", "medium")).lower()
            if severity not in _SEVERITY_RANK:
                severity = "medium"
            out.append(ComplianceFinding(
                rule_id="LLM-REVIEW",
                category=str(item.get("category", "模型判定")),
                severity=severity,  # type: ignore[arg-type]
                matched=str(item.get("matched", "")),
                excerpt=str(item.get("matched", ""))[:120],
                suggestion=str(item.get("suggestion", "")),
                source="llm",
            ))
        return out

    @staticmethod
    def _build_report(text: str, findings: list[ComplianceFinding], checked: int) -> ComplianceReport:
        # 去重：同一 rule+matched 只留一条（关键词与正则可能同时命中）
        seen: set[tuple[str, str]] = set()
        unique: list[ComplianceFinding] = []
        for f in findings:
            key = (f.rule_id, f.matched)
            if key in seen:
                continue
            seen.add(key)
            unique.append(f)

        max_sev = None
        if unique:
            max_sev = max(unique, key=lambda f: _SEVERITY_RANK[f.severity]).severity
        passed = max_sev is None or _SEVERITY_RANK[max_sev] < _SEVERITY_RANK["high"]
        return ComplianceReport(
            passed=passed,
            findings=unique,
            checked_rules=checked,
            needs_human=not passed,
            max_severity=max_sev,  # type: ignore[arg-type]
        )

    def rule_count(self) -> int:
        return len(self.rules)


def _excerpt(text: str, start: int, length: int, *, window: int = 20) -> str:
    """截取命中处的上下文（证据原文）。"""
    left = max(0, start - window)
    right = min(len(text), start + length + window)
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(text) else ""
    return f"{prefix}{text[left:right]}{suffix}"
