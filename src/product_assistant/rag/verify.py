"""RAG 答案后校验：把"有引用"升级为"引用真的支撑答案"。

只做检索、不做校验的系统有一个隐蔽失败：
    检索到了正确条款，模型却把 3.20% 写成 2.80%，引用列表还看起来完美。
    业务员看到"有依据"就敢发给客户，但依据其实对不上。

校验三条（确定性，不依赖模型自评）：
    1. 答案里的金额/利率/期限/次数，必须在某条引用原文中出现同值；
    2. 答案里的产品编号/政策条款号，必须出自引用；
    3. 输出"答案实际用到的引用"，未用到的条款不放进 citations（避免堆引用凑数）。

刻意不做的：用 LLM 判断"语义是否等价"。那会把"听起来对"当成"对"，
而这套校验存在的意义恰恰是给 LLM 的产出兜底。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from product_assistant.domain.models import RetrievedChunk

# 带业务单位的数值：利率/金额/期限/次数/年龄
_NUMBER_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*(%|％|万元|万|元|个月|年|次|天|周岁)",
)
# 产品编号与条款号
_PRODUCT_CODE_RE = re.compile(r"\bBANK-[A-Z0-9-]+\b")
_CLAUSE_RE = re.compile(r"第\s*([0-9]+(?:\.[0-9]+)?)\s*条")


@dataclass
class VerifyResult:
    """答案校验结论。"""

    ok: bool
    mismatched_numbers: list[tuple[str, str]] = field(default_factory=list)
    """(答案中的值, 引用中的值)；引用中找不到同值时为 ("2.8%", "未找到")"""
    unsupported_claims: list[str] = field(default_factory=list)
    used_hits: list[RetrievedChunk] = field(default_factory=list)
    usage_uncertain: bool = False
    """无法从答案反推实际用了哪些引用（生成式回答常见），此时调用方应保守处理。"""

    def issues(self) -> list[str]:
        out = [f"数值与政策原文不一致：{a}（引用为 {b}）" for a, b in self.mismatched_numbers]
        out.extend(f"引用未覆盖的表述：{item}" for item in self.unsupported_claims)
        return out


def _numbers(text: str) -> list[str]:
    return [f"{m.group(1)}{m.group(2)}" for m in _NUMBER_RE.finditer(text or "")]


def _norm(value: str) -> tuple[float, str]:
    match = re.match(r"(\d+(?:\.\d+)?)(.*)", value)
    if not match:
        return 0.0, value
    unit = "％" if match.group(2) == "%" else match.group(2)
    return float(match.group(1)), unit


def _supported_by(value: str, reference_numbers: set[tuple[float, str]]) -> bool:
    return _norm(value) in reference_numbers


def _hit_is_used(answer: str, hit: RetrievedChunk) -> bool:
    probe = (hit.parent_text or hit.text or "").strip()
    if probe and probe[:24] in answer:
        return True
    citation = hit.citation
    if citation.policy_id and citation.policy_id in answer:
        return True
    if citation.product_code and citation.product_code in answer:
        return True
    if citation.clause_no and f"第 {citation.clause_no} 条" in answer:
        return True
    return False


def verify_answer(answer: str, hits: list[RetrievedChunk]) -> VerifyResult:
    """核对答案是否被引用原文支撑。"""
    references = "\n".join((h.parent_text or h.text or "") for h in hits)
    reference_numbers = {_norm(item) for item in _numbers(references)}

    mismatched: list[tuple[str, str]] = []
    for value in _numbers(answer):
        if not _supported_by(value, reference_numbers):
            mismatched.append((value, "未找到"))

    reference_codes = {h.citation.product_code for h in hits if h.citation.product_code}
    reference_codes |= set(_PRODUCT_CODE_RE.findall(references))
    unsupported = [code for code in _PRODUCT_CODE_RE.findall(answer) if code not in reference_codes]

    reference_clauses = {h.citation.clause_no for h in hits if h.citation.clause_no}
    reference_clauses |= set(_CLAUSE_RE.findall(references))
    unsupported += [
        f"第 {num} 条" for num in _CLAUSE_RE.findall(answer) if num not in reference_clauses
    ]

    used = [h for h in hits if _hit_is_used(answer, h)]
    usage_uncertain = bool(hits) and not used
    return VerifyResult(
        ok=not mismatched and not unsupported,
        mismatched_numbers=mismatched,
        unsupported_claims=unsupported,
        used_hits=used or ([] if usage_uncertain else hits),
        usage_uncertain=usage_uncertain,
    )
