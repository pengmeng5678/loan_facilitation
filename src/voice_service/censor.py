"""内容安全（Censor）：语音客服的"嘴巴闸门"。

和项目七（素材先审后发）最大的区别：
    素材发出去还能删，**语音说出去收不回来**。所以这里不是"审完再决定要不要发"，
    而是"边说边审、命中立刻换成兜底话术" —— 宁可这一句答得笨一点，
    也不能让"我保证您能下款"从 AI 嘴里念出来。

助贷客服的五条红线（都有真实监管依据，面试时能讲清"为什么"）：
    1. 承诺性表述   保证下款 / 百分百通过 / 包过 / 无视征信
                    → 放款与否由银行审批决定，任何承诺都是虚假宣传；
    2. 诱导造假     包装资料 / 假流水 / 美化征信
                    → 这是骗贷，不是"服务"，参与方都可能担刑责；
    3. 违规收费     放款前先交钱 / 保证金 / 押金
                    → 助贷机构的红线，收前期费用本身就是违规；
    4. 债务链诱导   以贷养贷 / 借新还旧 / 多头借贷
                    → 会把客户推进债务螺旋，监管明确禁止诱导；
    5. 隐私与敏感   索要验证码/密码/完整身份证号，或把客户手机号念出来
                    → 语音场景下"念出来"等于向同房间所有人广播。

命中 1-4 的处理统一是：**整轮替换成兜底话术 + 标记转人工**。
原因：这类内容不能靠"改几个词"来降级 —— "保证下款"改成"大概率能下"同样是违规。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from voice_service.domain import CensorFinding, CensorVerdict

# =========================================================================== #
# 兜底话术（按类别准备，避免所有违规都用同一句"抱歉"）
# =========================================================================== #
SAFE_REPLIES: dict[str, str] = {
    "承诺性表述": "能不能批、批多少，都以银行最终审批为准，我不能给您任何承诺。您先把基本情况跟我说下，我帮您判断方向。",
    "诱导造假": "资料必须真实，这个我给不了变通的办法。您如果有难处，我们可以看看有没有别的合规方案。",
    "违规收费": "我们的费用都是放款后按约定结算，前期不收取任何费用。您要是遇到要求预付的，一定要小心。",
    "债务链诱导": "同时申请多家、或者借新的还旧的，风险其实更大。我建议先把现有负债理清楚再说。",
    "隐私与敏感": "密码和验证码您千万别告诉任何人，包括我。需要核对的资料我们线下当面看。",
    "绝对化用语": "我们是正规机构，具体方案得看您的实际情况，我跟您说下大概怎么走。",
}
GENERIC_SAFE_REPLY = "这个问题我请专员跟您详细确认一下，我先把您的需求记下来。"
HANDOFF_LINE = "（已记录，稍后由人工专员跟进）"

# 客户话语里需要"立刻拒绝 + 打标"的类别（不含"承诺性表述"：客户要承诺是正常提问）
_USER_RISK_CATEGORIES: frozenset[str] = frozenset({"诱导造假", "违规收费", "债务链诱导"})


# =========================================================================== #
# 规则表
# =========================================================================== #
@dataclass(frozen=True)
class CensorRule:
    rule_id: str
    category: str
    severity: str
    keywords: tuple[str, ...]
    suggestion: str
    """命中后的处理建议（写给运营/审核看的，不进对话）。"""
    blocking: bool = True
    """True = 不可改写，整轮替换 + 转人工；False = 可自动改写后继续。"""
    patterns: tuple[re.Pattern[str], ...] = field(default_factory=tuple)
    """关键词覆盖不到的形状（如"一定能批下来"中间夹字），用正则兜底。"""


_BLOCKING_RULES: tuple[CensorRule, ...] = (
    CensorRule(
        rule_id="PROMISE-01",
        category="承诺性表述",
        severity="high",
        keywords=(
            "保证下款", "保证批", "保证贷", "百分百", "100%", "一定批", "肯定批", "必批",
            "包过", "包批", "包下款", "稳下", "必下", "肯定能下", "无条件放款",
            "无视征信", "不看征信", "黑户也能", "黑户也可以", "当天到账",
        ),
        suggestion="删掉一切结果承诺，改为'以银行最终审批为准'。",
        patterns=(
            re.compile(r"保证.{0,6}(下款|批|放款|通过)"),
            re.compile(r"一定.{0,4}(批|下|过)"),
        ),
    ),
    CensorRule(
        rule_id="FRAUD-01",
        category="诱导造假",
        severity="high",
        keywords=(
            "包装资料", "包装一下", "伪造", "假流水", "做假", "假的流水", "假收入",
            "假社保", "美化征信", "把征信处理一下", "改征信", "ps流水",
        ),
        suggestion="明确拒绝并说明必须真实资料；必要时上报风控。",
    ),
    CensorRule(
        rule_id="FEE-01",
        category="违规收费",
        severity="high",
        keywords=("前期收费", "先交钱", "先交费", "保证金", "押金", "先付", "服务费先"),
        suggestion="放款前不得收取任何费用，纠正表述并说明结算方式。",
        # 两个 pattern 都刻意避开"否定句"：
        # "放款前**不**收任何费用"是我们自己的合规话术，不能被自己拦下来。
        # 用 [^不] 而不是 . 的理由：否定词经常就夹在"放款前"和动词之间
        # （"放款前不收取…"），用 . 会把整句吃掉然后再也排除不掉。
        patterns=(
            re.compile(r"放款前[^不]{0,6}(交|付|收)(?!任何)"),
            re.compile(r"(先|提前)[^不]{0,4}(交|付|收)[^不]{0,4}(费|钱|保证金|押金)"),
        ),
    ),
    CensorRule(
        rule_id="CHAIN-01",
        category="债务链诱导",
        severity="high",
        keywords=("以贷养贷", "借新还旧", "多头借贷", "多申请几家", "拆东墙", "再贷一笔还"),
        suggestion="不得诱导以贷养贷/多头借贷，改为先梳理负债。",
    ),
    CensorRule(
        rule_id="PRIVACY-01",
        category="隐私与敏感",
        severity="high",
        keywords=("验证码发我", "密码告诉我", "把验证码", "报一下银行卡号", "身份证号发我"),
        suggestion="不得索要验证码/密码/完整卡号；改为线下核验或官方渠道。",
        # 必须带上"给我/发我"这个动作方向：
        # "验证码您千万别告诉任何人"是我们自己的合规话术，不该被自己拦下来。
        patterns=(re.compile(r"(验证码|密码).{0,6}(发|报|告诉)(给)?我"),),
    ),
)

# 可自动改写的中风险规则（不阻断，只降级表达）
_REWRITE_RULES: tuple[CensorRule, ...] = (
    CensorRule(
        rule_id="ADLAW-01",
        category="绝对化用语",
        severity="medium",
        keywords=("最好", "最专业", "最权威", "最强", "唯一", "顶级", "国家级", "万能"),
        suggestion="广告法禁用绝对化用语，改为中性表述。",
        blocking=False,
    ),
)

# 改写表：长词优先（避免"最专业"被"最专"类短词抢先替换）
REWRITE_MAP: tuple[tuple[str, str], ...] = (
    ("最专业", "比较专业"),
    ("最权威", "相对权威"),
    ("最强", "较强"),
    ("最好", "比较合适"),
    ("国家级", "正规"),
    ("顶级", "优质"),
    ("万能", "多种"),
    ("唯一", "少数"),
)

# =========================================================================== #
# 归一化：把"故意写成各种形状"的话还原成可匹配的文本
#
# 输入是 ASR 转写结果，同一个词到我们手里可能是：
#     "保 证 下 款"（同音/断字躲避）、"包\u200b过"（零宽字符）、
#     "１００％"（全角）、"包·过"（分隔符插入）
# 直接拿原文做 substring 匹配会全部漏掉 —— 这就是语音场景最容易被绕过的地方。
# 所以扫描前统一做三步：NFKC 全半角归一 → 去掉空白/零宽/分隔符 → 转小写。
# 注意：归一化只用于**扫描**，改词和播报仍用原文（不能把客户看到的字改掉）。
# =========================================================================== #
_ZW_RE = re.compile(r"[\s\u200b-\u200d\ufeff\u3000]+")
_SEP_RE = re.compile(r"[·•‧・|｜/／\\,，、。;；:：~～\-—_]+")


def normalize_for_scan(text: str) -> str:
    """归一化文本用于规则扫描（全角→半角、去空白/零宽/分隔符、小写）。"""
    normalized = unicodedata.normalize("NFKC", text or "")
    return _SEP_RE.sub("", _ZW_RE.sub("", normalized)).lower()


# 拼音黑名单：中文转拼音后"包过"变成 baoguo，关键词匹配完全失效。
# 项目不引入 pypinyin（它会误伤多音字、还会拖慢 100ms 级链路），
# 改为对**红线短语**维护小规模拼音别名，用穷举换确定性。
# 拼音串可能带空格（bao guo）或数字声调，归一化已去掉空格；数字声调暂不覆盖。
_PINYIN_ALIASES: tuple[tuple[str, str, str], ...] = (
    ("baoguo", "包过", "PROMISE-01"),
    ("baozhengxiakuan", "保证下款", "PROMISE-01"),
    ("wushizhengxin", "无视征信", "PROMISE-01"),
    ("buakanzhengxin", "不看征信", "PROMISE-01"),
    ("baozhuangziliao", "包装资料", "FRAUD-01"),
    ("jialiushui", "假流水", "FRAUD-01"),
    ("meihuazhengxin", "美化征信", "FRAUD-01"),
    ("baozhengjin", "保证金", "FEE-01"),
    ("yidaiyangdai", "以贷养贷", "CHAIN-01"),
    ("jiexinhuangjiu", "借新还旧", "CHAIN-01"),
    ("duotoujiedai", "多头借贷", "CHAIN-01"),
)


# ---- PII 脱敏（语音场景是"输出前屏蔽"，不是"入库前脱敏"）---- #
_PII_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)"), r"\1****\2"),   # 手机号
    (re.compile(r"(?<!\d)(\d{4})\d{10}(\d{3}[\dXx])(?!\d)"), r"\1**********\2"),  # 身份证
    (re.compile(r"(?<!\d)(\d{4})\d{8,11}(\d{4})(?!\d)"), r"\1********\2"),  # 银行卡
)


def mask_pii(text: str) -> tuple[str, list[str]]:
    """把文本里的手机号/身份证/银行卡打码，返回 (脱敏后文本, 命中的原文列表)。

    注意顺序：身份证规则必须在银行卡之前跑，否则 18 位身份证会被
    16-19 位的银行卡规则先吃掉（项目七踩过这个坑）。
    """
    hits: list[str] = []
    result = text
    for pattern, replacement in _PII_PATTERNS:
        for match in pattern.finditer(result):
            hits.append(match.group(0))
        result = pattern.sub(replacement, result)
    return result, hits


# =========================================================================== #
# 检查器
# =========================================================================== #
class Censor:
    """内容安全检查器（确定性规则，不依赖模型）。

    为什么不全交给 LLM 判：语音链路要求 100ms 级判定，且"承诺放款"这种
    红线必须**零漏检** —— 规则引擎的召回率是确定的，模型不是。
    模型只适合做规则的补充（本项目在评测里用 LLM 做复核，不在主链路上）。
    """

    def __init__(
        self,
        *,
        rules: tuple[CensorRule, ...] | None = None,
        rewrite_rules: tuple[CensorRule, ...] | None = None,
    ) -> None:
        self.rules = rules if rules is not None else _BLOCKING_RULES
        self.rewrite_rules = rewrite_rules if rewrite_rules is not None else _REWRITE_RULES

    # ------------------------------------------------------------------ #
    def _scan(self, text: str, rules: tuple[CensorRule, ...]) -> list[CensorFinding]:
        findings: list[CensorFinding] = []
        lowered = text.lower()
        normalized = normalize_for_scan(text)
        for rule in rules:
            matched: set[str] = set()
            for kw in rule.keywords:
                # 关键词同时扫原文与归一化文本：后者覆盖"保 证 下 款"、
                # 零宽字符、全角数字等刻意变体。
                if kw.lower() in lowered or normalize_for_scan(kw) in normalized:
                    matched.add(kw)
            # 正则原文 + 归一化各跑一遍：原文保留跨标点语义，归一化覆盖
            # "保 证 您能下 款"这类词组被拆开的变体（关键词层已覆盖相邻形态）。
            for pattern in rule.patterns:
                for m in pattern.finditer(text):
                    matched.add(m.group(0))
                for m in pattern.finditer(normalized):
                    matched.add(m.group(0))
            # 拼音变体只对当前规则声明过的红线短语生效
            for pinyin, phrase, rule_id in _PINYIN_ALIASES:
                if rule_id == rule.rule_id and pinyin in normalized:
                    matched.add(f"{phrase}（拼音:{pinyin}）")
            for hit in matched:
                findings.append(
                    CensorFinding(
                        rule_id=rule.rule_id,
                        category=rule.category,
                        severity=rule.severity,  # type: ignore[arg-type]
                        matched=hit,
                        suggestion=rule.suggestion,
                    )
                )
        return findings

    # ------------------------------------------------------------------ #
    def check(self, text: str, *, mask_pii_enabled: bool = True) -> CensorVerdict:
        """检查一段 AI 准备说出口的话。"""
        text = text or ""
        findings = self._scan(text, self.rules)
        rewrite_findings = self._scan(text, self.rewrite_rules)
        findings.extend(rewrite_findings)

        # 1) PII：能打码就打码（既不泄露，也不至于整轮作废）
        sanitized = text
        pii_hits: list[str] = []
        if mask_pii_enabled:
            sanitized, pii_hits = mask_pii(text)
            for hit in pii_hits:
                findings.append(
                    CensorFinding(
                        rule_id="PII-01",
                        category="隐私与敏感",
                        severity="high",
                        matched=hit,
                        suggestion="对外播报前必须打码，避免向同房间广播客户隐私。",
                    )
                )

        # 2) 可改写的中风险：直接换词（"最好"→"比较合适"）
        for old, new in REWRITE_MAP:
            if old in sanitized:
                sanitized = sanitized.replace(old, new)

        # 3) 不可改写的高风险：整轮替换 + 转人工
        blocking = [f for f in findings if f.severity == "high"]
        if blocking:
            category = blocking[0].category
            return CensorVerdict(
                passed=False,
                needs_human=True,
                blocked=True,
                findings=findings,
                sanitized_text=sanitized,
                safe_reply=SAFE_REPLIES.get(category, GENERIC_SAFE_REPLY),
            )

        # 4) 只命中可改写项：放行，但留痕（审的是"改了什么"）
        return CensorVerdict(
            passed=True,
            needs_human=False,
            blocked=False,
            findings=findings,
            sanitized_text=sanitized,
            safe_reply="",
        )

    # ------------------------------------------------------------------ #
    def check_user_text(self, text: str) -> CensorVerdict:
        """检查**客户说的话**（识别"客户要求我们做违规的事"）。

        只针对"客户要求我们违规"的三类：诱导造假、违规收费、以贷养贷。
        刻意**不包含**"承诺性表述"—— 客户问"你能保证下款吗"是正常提问，
        正确反应是回答"以银行审批为准"，而不是把客户标成高风险转人工。
        （判断"AI 有没有承诺"是出口检查的事，见 check()。）
        """
        rules = tuple(r for r in self.rules if r.category in _USER_RISK_CATEGORIES)
        findings = self._scan(text or "", rules)
        blocking = [f for f in findings if f.severity == "high"]
        return CensorVerdict(
            passed=not blocking,
            needs_human=bool(blocking),
            blocked=bool(blocking),
            findings=findings,
            sanitized_text=text or "",
            safe_reply=SAFE_REPLIES.get(blocking[0].category, GENERIC_SAFE_REPLY) if blocking else "",
        )
