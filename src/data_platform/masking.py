"""脱敏引擎（合规的第一道硬约束）。

三条设计原则，都是踩过坑才定下来的：

**① 规则有优先级，必须"身份证先于银行卡"**
    18 位身份证号同时满足"16-19 位连续数字"，如果先跑银行卡规则，
    身份证会被打成"1101****...1234"这种四不像，而且审计里记录的类型是错的。
    所以候选命中按 `priority` 排序，重叠时高优先级胜出 —— 不是靠写正则的运气。

**② 脱敏必须幂等（同一段文本跑两次，第二次零命中）**
    这是三层冗余脱敏（清洗 / 写入 / 导出）能成立的前提：
    如果第二次还会命中，说明替换串本身又被规则吃掉了，
    那"三层"不仅没兜底，还会把文本越改越乱。
    `verify()` 就是这条不变量的可执行形式，导出层每次都会调用它。

**③ 价格是硬红线，但它是商业红线不是 PII**
    真实报价进了微调语料，模型会把公司底价复述给客户 —— 议价能力直接归零。
    所以金额/利率/点位一律打码，且单独计数（`price_hits`），
    便于回答"你们怎么证明价格没泄露"。

**④ 变体先归一化再匹配（防绕过），但归一化只发生在"检测层"**
    全角数字/字母 + 全角分隔符是用户"看着像打了码"的典型写法
    （１３８－１２３４－５６７８），不处理就是训练数据泄露。
    归一化表是 **1:1 字符映射（等长）**：正则在归一化文本上找命中区间，
    替换按相同坐标落回原文 —— 命中片段之外的原文原样保留（不破坏展示），
    替换串统一用归一化后的半角构造 → 第二次匹配零命中，②的幂等不被破坏。
    为什么不用 unicodedata.normalize("NFKC")：NFKC 会改长度（①→1、ﬁ→fi），
    区间就对不回原文坐标了。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from data_platform.domain import MaskedText, MaskingFinding, MaskKind, MaskLayer, RawMessage

# --------------------------------------------------------------------------- #
# 全角 → 半角 归一化（只用于匹配层，见文件头原则④）
# --------------------------------------------------------------------------- #
#: 1:1 字符映射（等长），因此归一化文本与原文的字符区间一一对应。
_WIDTH_MAP: dict[int, str] = {
    **{0xFF10 + i: chr(0x30 + i) for i in range(10)},  # ０-９ → 0-9
    **{0xFF21 + i: chr(0x41 + i) for i in range(26)},  # Ａ-Ｚ → A-Z
    **{0xFF41 + i: chr(0x61 + i) for i in range(26)},  # ａ-ｚ → a-z
    # 号码/邮箱内部的混淆分隔符变体：归一成半角，让正则能"跨过去"匹配
    0x3000: " ",  # 全角空格
    0xFF0D: "-",  # － 全角横线
    0x2010: "-",  # ‐ 短横（中文输入常见变体）
    0x2013: "-",  # – en dash
    0x2014: "-",  # — em dash（破折号单字）
    0xFF0E: ".",  # ．全角点号
    0xFF0C: ",",  # ，全角逗号（金额千分位混淆）
    0xFF20: "@",  # ＠ 全角艾特（配合全角字母堵住邮箱绕过）
}
_WIDTH_TABLE = str.maketrans(_WIDTH_MAP)

# 金额：支持千分位与小数（50,000 / 3.2）
_AMOUNT = r"\d[\d,]*(?:\.\d+)?"
# 身份证：18 位（末位可能是 X）；15 位老身份证兜底
_ID18 = re.compile(r"(?<!\d)(\d{6})(\d{8})(\d{3}[\dXx])(?!\d)")
_ID15 = re.compile(r"(?<!\d)(\d{6})(\d{6})(\d{3})(?!\d)")
# 银行卡：16-19 位连续数字（必须在身份证之后跑）
_BANK = re.compile(r"(?<!\d)(\d{4})(\d{8,11})(\d{4})(?!\d)")
# 手机号：1[3-9] 开头 11 位；组间允许夹分隔符（138-1234-5678 / 全角变体归一化后
# 也是半角）。分隔符版本若不认，用户随手加个横线就绕过脱敏 —— 漏报的代价远高于误报
_PHONE = re.compile(r"(?<!\d)(1[3-9]\d)[-.\s]*(\d{4})[-.\s]*(\d{4})(?!\d)")
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]{1,3})[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_API_KEY = re.compile(
    r"(?i)(sk-[A-Za-z0-9]{12,}|AKID[A-Za-z0-9]{10,}|Bearer\s+[A-Za-z0-9._\-]{12,})"
)
# 价格三组：金额 / 利率百分比 / 点位
_PRICE_AMOUNT = re.compile(rf"({_AMOUNT})\s*(万元|万|元|块钱|块)")
_PRICE_RATE = re.compile(rf"({_AMOUNT})\s*%")
# 点位只认"N 个点"：裸的"N 点"通常是时间（"周六上午 10 点"），
# 早期版本允许省略"个"，结果把预约时间全打成了[点位已脱敏] —— 假阳性比漏报更伤可用性。
_PRICE_POINT = re.compile(rf"({_AMOUNT})\s*个点")
_PRICE_FEE = re.compile(r"(服务费|佣金|手续费|点位|返点|费率)\s*(?:为|是|[:：])?\s*\d[\d,]*(?:\.\d+)?\s*(?:%|个点|点)?")


@dataclass(frozen=True)
class _Candidate:
    """一个候选命中（在原始文本上的区间）。"""

    start: int
    end: int
    priority: int
    kind: MaskKind
    rule: str
    replacement: str


class MaskingEngine:
    """正则 + 词典的脱敏引擎（确定性、无外部依赖）。"""

    def __init__(
        self,
        *,
        known_names: Sequence[str] = (),
        mask_names: bool = True,
        mask_prices: bool = True,
    ) -> None:
        self.mask_names = mask_names
        self.mask_prices = mask_prices
        # 长名优先，避免"张三"先把"张三丰"吃掉一半
        self.known_names = sorted({n for n in known_names if n and len(n) >= 2}, key=len, reverse=True)
        self._name_pattern = (
            re.compile("|".join(re.escape(n) for n in self.known_names)) if self.known_names else None
        )

    # ------------------------------------------------------------------ #
    # 对外
    # ------------------------------------------------------------------ #
    def mask(self, text: str, *, layer: MaskLayer = "clean") -> MaskedText:
        """对一段文本做全规则脱敏。

        `layer` 只用于审计（记录"第几层拦住的"），不改变规则集 ——
        三层用同一套规则，才谈得上"冗余"。
        """
        if not text:
            return MaskedText(text="", layers=[layer])

        # 归一化只在匹配层（原则④）：等长映射保证区间能对回原文，
        # 命中片段之外的原文原样保留，替换串由归一化分组构造（半角）→ 幂等可 verify。
        norm = text.translate(_WIDTH_TABLE)
        candidates: list[_Candidate] = []
        candidates.extend(self._pii_candidates(norm))
        if self.mask_prices:
            candidates.extend(self._price_candidates(norm))

        findings: list[MaskingFinding] = []
        price_hits = 0
        out: list[str] = []
        cursor = 0
        # 重叠时高优先级（数字小）胜出；同优先级按出现顺序
        for cand in sorted(candidates, key=lambda c: (c.start, c.priority)):
            if cand.start < cursor:
                continue  # 与已接受的命中重叠 → 丢弃（高优先级已在前面）
            out.append(text[cursor:cand.start])
            out.append(cand.replacement)
            cursor = cand.end
            findings.append(MaskingFinding(
                rule=cand.rule,
                kind=cand.kind,
                start=cand.start,
                end=cand.end,
                preview=cand.replacement,
            ))
            if cand.kind == "price":
                price_hits += 1
        out.append(text[cursor:])
        return MaskedText(
            text="".join(out), findings=findings, price_hits=price_hits, layers=[layer]
        )

    def verify(self, text: str) -> list[MaskingFinding]:
        """校验一段（声称已脱敏的）文本是否还有残留。

        导出层每次都会调它：`verify()` 非空 → 拒绝导出。
        这条断言比"我们做了三层脱敏"这句口号可靠得多。
        """
        return self.mask(text, layer="export").findings

    def mask_messages(
        self, messages: Iterable[RawMessage], *, layer: MaskLayer = "clean"
    ) -> tuple[list[RawMessage], list[MaskingFinding], int]:
        """批量脱敏消息，返回 (新消息列表, 命中明细, 价格命中数)。

        注意：**内容变了就要生成新的 msg_id** —— 否则下游去重会把
        "已脱敏"和"未脱敏"当成同一条，导出时可能漏掉脱敏版本。
        """
        result: list[RawMessage] = []
        findings: list[MaskingFinding] = []
        price_hits = 0
        for msg in messages:
            masked = self.mask(msg.content, layer=layer)
            findings.extend(masked.findings)
            price_hits += masked.price_hits
            if masked.text == msg.content:
                result.append(msg)
                continue
            result.append(msg.model_copy(update={
                "content": masked.text,
                "msg_id": f"{msg.msg_id}#{layer}",
            }))
        return result, findings, price_hits

    def rules(self) -> list[dict[str, object]]:
        """规则清单（给 `/data/rules` 与合规说明用，不是给模型看）。"""
        rows: list[dict[str, object]] = [
            {"rule": "id_card", "priority": 30, "kind": "id_card",
             "note": "18/15 位身份证：保留前 6 位与后 4 位；必须先于银行卡"},
            {"rule": "bank_card", "priority": 40, "kind": "bank_card",
             "note": "16-19 位银行卡：保留前 4 位与后 4 位"},
            {"rule": "phone", "priority": 50, "kind": "phone",
             "note": "手机号：138****5678"},
            {"rule": "email", "priority": 20, "kind": "email",
             "note": "邮箱：首字母 + *** + 域名"},
            {"rule": "api_key", "priority": 10, "kind": "api_key",
             "note": "sk-*** / AKID*** / Bearer ***"},
        ]
        if self.mask_names:
            rows.append({"rule": "name", "priority": 60, "kind": "name",
                         "note": f"客户姓名词典（{len(self.known_names)} 个）：张*"})
        if self.mask_prices:
            rows.extend([
                {"rule": "price_amount", "priority": 70, "kind": "price",
                 "note": "金额（元/万/块）→ [金额已脱敏]；价格硬红线"},
                {"rule": "price_rate", "priority": 70, "kind": "price",
                 "note": "百分比（利率）→ [利率已脱敏]"},
                {"rule": "price_fee", "priority": 70, "kind": "price",
                 "note": "服务费/佣金/点位 → [已脱敏]"},
            ])
        return sorted(rows, key=lambda r: int(r["priority"]))  # type: ignore[arg-type]

    # ------------------------------------------------------------------ #
    # 候选生成
    # ------------------------------------------------------------------ #
    def _pii_candidates(self, text: str) -> list[_Candidate]:
        cands: list[_Candidate] = []

        for m in _API_KEY.finditer(text):
            cands.append(_Candidate(
                m.start(), m.end(), 10, "api_key", "api_key", "[密钥已脱敏]"
            ))

        for m in _EMAIL.finditer(text):
            head, domain = m.group(1), m.group(2)
            cands.append(_Candidate(
                m.start(), m.end(), 20, "email", "email", f"{head}***@{domain}"
            ))

        for m in _ID18.finditer(text):
            cands.append(_Candidate(
                m.start(), m.end(), 30, "id_card", "id_card",
                f"{m.group(1)}********{m.group(3)}",
            ))
        for m in _ID15.finditer(text):
            # 15 位只在"不像银行卡"时按身份证处理：15 位不满足 16-19，
            # 所以不会和银行卡冲突；但为避免误伤长数字串，只在 15 位整体命中时接受
            cands.append(_Candidate(
                m.start(), m.end(), 30, "id_card", "id_card",
                f"{m.group(1)}******{m.group(3)}",
            ))

        for m in _BANK.finditer(text):
            cands.append(_Candidate(
                m.start(), m.end(), 40, "bank_card", "bank_card",
                f"{m.group(1)}{'*' * len(m.group(2))}{m.group(3)}",
            ))

        for m in _PHONE.finditer(text):
            cands.append(_Candidate(
                m.start(), m.end(), 50, "phone", "phone",
                f"{m.group(1)}****{m.group(3)}",
            ))

        if self._name_pattern is not None:
            for m in self._name_pattern.finditer(text):
                name = m.group(0)
                masked = name[0] + "*" * (len(name) - 1)
                cands.append(_Candidate(
                    m.start(), m.end(), 60, "name", "name", masked
                ))
        return cands

    def _price_candidates(self, text: str) -> list[_Candidate]:
        cands: list[_Candidate] = []
        for m in _PRICE_AMOUNT.finditer(text):
            cands.append(_Candidate(
                m.start(), m.end(), 70, "price", "price_amount", "[金额已脱敏]"
            ))
        for m in _PRICE_RATE.finditer(text):
            cands.append(_Candidate(
                m.start(), m.end(), 70, "price", "price_rate", "[利率已脱敏]"
            ))
        for m in _PRICE_POINT.finditer(text):
            cands.append(_Candidate(
                m.start(), m.end(), 70, "price", "price_point", "[点位已脱敏]"
            ))
        for m in _PRICE_FEE.finditer(text):
            cands.append(_Candidate(
                m.start(), m.end(), 70, "price", "price_fee", f"{m.group(1)}[已脱敏]"
            ))
        return cands


def collect_known_names(rows: Iterable[dict]) -> list[str]:
    """从客户档案里收集真实姓名（供词典型脱敏使用）。

    只收**未打码**的姓名：已经带 `*` 的值再脱敏没有意义，
    还会让词典里混进"张*生"这种伪姓名，误伤正常文本。
    """
    names: list[str] = []
    for row in rows:
        for key in ("name", "customer_name", "real_name"):
            value = str(row.get(key) or "").strip()
            if value and "*" not in value and len(value) >= 2:
                names.append(value)
    return sorted(set(names), key=len, reverse=True)
