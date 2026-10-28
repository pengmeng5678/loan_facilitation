"""脱敏器 —— 素材合规的第一道也是最硬的一道门槛。

为什么在助贷场景脱敏必须"做在生成之前"：
    一旦身份证/手机号/银行卡进了对外素材，就是**泄露客户金融信息**，
    且素材会被业务员发到公开平台（抖音/小红书），传播不可控、不可撤回。
    所以脱敏不是"发布前检查一下"，而是**素材进入生成流程的入口就被剥掉**。

处理对象与策略：
    手机号     138****5678      （保留号段与尾号，便于业务内部对账，但无法拨通）
    身份证     3201**********1234
    银行卡     **** **** **** 1234
    微信/QQ    ****
    金额       30万 → 10-50万     （转区间：既保护隐私，又保留"额度感"这一说服力要素）
    姓名       张*生 → 张*        （需要外部传入已知姓名列表，规则无法凭空识别中文名）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 注意执行顺序：身份证（18 位）必须在银行卡（16-19 位）之前，
# 否则 18 位身份证会被银行卡规则先吃掉，脱敏结果就不对了。
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_BANK_CARD = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_WECHAT = re.compile(
    r"(?:微信|weixin|wx|WX|QQ|qq)\s*(?:号|号码)?\s*[:：]?\s*([A-Za-z][A-Za-z0-9_\-]{5,19})"
)
_AMOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*(万元|万|元|块)")


@dataclass
class DesensitizeFinding:
    """一条脱敏记录（审计留痕：脱了什么、变成了什么）。"""

    kind: str
    original: str
    masked: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "original": self.original, "masked": self.masked}


@dataclass
class DesensitizeResult:
    """脱敏结果。"""

    text: str
    findings: list[DesensitizeFinding] = field(default_factory=list)

    @property
    def hits(self) -> int:
        return len(self.findings)

    @property
    def kinds(self) -> list[str]:
        return sorted({f.kind for f in self.findings})


def amount_bucket(value: float) -> str:
    """把精确金额转成区间。

    设计意图：客户证言里"我贷到了 30 万"是最有说服力的部分，直接抹掉会让素材失去价值；
    但精确金额可被反查到具体客户。折中为"区间" —— 保护隐私的同时保留量级感。
    """
    if value >= 100:
        return "百万级"
    if value >= 50:
        return "50万以上"
    if value >= 10:
        return "10-50万"
    if value >= 1:
        return "10万以内"
    return "万元以内"


class Desensitizer:
    """文本脱敏器。

    参数：
        known_names: 已知客户姓名列表（来自客户档案）。中文姓名无法纯规则识别，
                     所以由外部把"可能出现的名字"传进来做精确替换。
        full_mask:   True 时对手机号/身份证做**完全移除**（对外公开素材建议开启）；
                     False 时使用行业通行的部分掩码形式（内部案例卡适用）。
    """

    def __init__(self, known_names: list[str] | None = None, *, full_mask: bool = False) -> None:
        self.known_names = sorted({n for n in (known_names or []) if n and len(n) >= 2}, key=len, reverse=True)
        self.full_mask = full_mask

    def mask(self, text: str) -> DesensitizeResult:
        """执行脱敏，返回脱敏后的文本与留痕。"""
        findings: list[DesensitizeFinding] = []
        out = text or ""

        out = self._sub(out, _PHONE, self._mask_phone, "手机号", findings)
        out = self._sub(out, _ID_CARD, self._mask_id, "身份证", findings)
        out = self._sub(out, _BANK_CARD, self._mask_bank, "银行卡", findings)
        out = self._sub(out, _WECHAT, self._mask_wechat, "微信/QQ", findings)
        out = self._sub(out, _AMOUNT, self._mask_amount, "金额", findings)
        out = self._mask_names(out, findings)

        return DesensitizeResult(text=out, findings=findings)

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _sub(text: str, pattern: re.Pattern[str], repl, kind: str, findings: list) -> str:
        def _do(match: re.Match[str]) -> str:
            original = match.group(0)
            masked = repl(match)
            findings.append(DesensitizeFinding(kind=kind, original=original, masked=masked))
            return masked

        return pattern.sub(_do, text)

    def _mask_phone(self, match: re.Match[str]) -> str:
        raw = match.group(0)
        if self.full_mask:
            return "【手机号已脱敏】"
        return f"{raw[:3]}****{raw[-4:]}"

    def _mask_id(self, match: re.Match[str]) -> str:
        raw = match.group(0)
        if self.full_mask:
            return "【身份证已脱敏】"
        return f"{raw[:4]}{'*' * 10}{raw[-4:]}"

    def _mask_bank(self, match: re.Match[str]) -> str:
        raw = match.group(0)
        return f"{'*' * max(0, len(raw) - 4)}{raw[-4:]}"

    def _mask_wechat(self, match: re.Match[str]) -> str:
        return "****"

    def _mask_amount(self, match: re.Match[str]) -> str:
        value = float(match.group(1))
        unit = match.group(2)
        # "元/块"换算成"万"，让区间口径统一
        if unit in ("元", "块"):
            value = value / 10000
        if value < 1:
            # 小额（几百块）不构成隐私问题，但也不该出现在素材里，统一抹掉
            return "【金额已脱敏】"
        return amount_bucket(value)

    def _mask_names(self, text: str, findings: list) -> str:
        for name in self.known_names:
            if name in text:
                masked = self.mask_name(name)
                # 用 replace 而非正则：姓名可能含百家姓外的字，纯字符串匹配更稳
                text = text.replace(name, masked)
                findings.append(DesensitizeFinding(kind="姓名", original=name, masked=masked))
        return text

    @staticmethod
    def mask_name(name: str) -> str:
        """把**已知就是姓名**的字符串掩码化（首字保留 + 其余星号）。

        与 `_mask_names` 的区别很重要：`_mask_names` 靠"姓名词表 + 全文扫描"，
        词表没覆盖到就漏；而案例卡上的"客户称呼"是我们**自己取出来的**，
        本来就知道它是姓名，所以直接按姓名规则掩码，不依赖词表 ——
        否则客户档案里姓名没同步过来，真名就会被写进对外素材。
        """
        name = (name or "").strip()
        if not name:
            return ""
        if len(name) == 1:
            return "*"
        return name[0] + "*" * (len(name) - 1)
