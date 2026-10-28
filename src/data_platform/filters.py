"""内容过滤与预标注（9 级分类 + 质量分 + QA 抽取）。

**为什么要先分类再入库**：真实微信记录里，大部分消息对训练毫无价值
（系统提示、表情、单字应答、推销），直接灌进向量库只会让召回变差。
把"什么算有效数据"写成可解释的规则，才能回答"你们的数据质量怎么保证"。

**分类顺序（一条消息的旅程）**：
    1 系统消息 → 2 媒体消息 → 3 敏感对话 → 4 垃圾广告
    → 5 过短 → 6 闲聊 → 7 重复 → 8 普通有效 → 9 高价值

**一个刻意偏离直觉的决定：敏感优先于过短。**
    "你们骗人"只有四个字，按长度该被丢；但它可能是投诉/监管风险的苗头。
    合规风险不能因为"字少"被漏掉，所以风险关键词先于长度阈值判定。
    这类对话会进暂存区（主管必须看到），但 `dataset_eligible=False`，
    永远不进训练集 —— "要不要人看"和"能不能拿去训练"是两个问题。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from data_platform.domain import (
    CATEGORY_PRIORITY,
    BlockStats,
    ConversationBlock,
    FilterDecision,
    QAPair,
    RawMessage,
)

# --------------------------------------------------------------------------- #
# 词表（可配置，默认值来自助贷业务语料）
# --------------------------------------------------------------------------- #
SYSTEM_MARKERS = (
    "撤回了一条消息", "加入了群聊", "邀请", "拍了拍", "领取了", "红包",
    "以上是打招呼的内容", "该消息类型暂不支持", "开启了朋友验证",
    "你已添加了", "现在可以开始聊天了", "消息已发出，但被对方拒收",
)

MEDIA_PATTERN = re.compile(r"^\[(图片|视频|语音|文件|表情|动画表情|链接|位置|名片|小程序|转账|聊天记录|音乐)\]$")

RISK_KEYWORDS = (
    "投诉", "举报", "报警", "律师", "起诉", "维权", "监管", "银保监", "12378",
    "骗人", "诈骗", "曝光", "差评", "退款", "退费", "乱收费", "高利贷",
    "骚扰", "威胁", "暴力催收",
)

SPAM_KEYWORDS = (
    "秒批", "无需审核", "不看征信包过", "刷单", "返利", "日赚", "代理加盟",
    "点击链接", "免费办理", "代开", "包下款", "贷款秒到",
)

BUSINESS_KEYWORDS = (
    "额度", "利率", "公积金", "社保", "征信", "负债", "置换", "网贷",
    "保单", "房产", "营业执照", "流水", "月供", "期限", "先息后本",
    "到店", "面签", "放款", "审批", "资料", "服务费", "佣金",
)

OUTCOME_KEYWORDS = ("签约", "签了", "签合同", "放款", "到账", "办成了", "通过了", "成交")

CHITCHAT_TOKENS = ("嗯", "哦", "好的", "好", "行", "在吗", "在的", "谢谢", "收到", "ok", "OK", "？", "?", "哈哈")

#: 意图场景（知识产物的 scene 字段；规则判定，不靠模型）
SCENE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("债务置换", ("置换", "网贷", "负债优化", "以贷养贷")),
    ("征信解读", ("征信", "查询", "逾期", "负债比")),
    ("利率费用", ("利率", "利息", "服务费", "佣金", "费用", "点位")),
    ("资质准入", ("公积金", "社保", "房产", "保单", "营业执照", "资质")),
    ("邀约到店", ("到店", "面签", "约个时间", "明天下午", "过来一趟")),
    ("流程资料", ("资料", "材料", "流程", "多久", "几天", "放款时间")),
)


class ConversationFilter:
    """确定性分类器 + 质量打分 + QA 抽取。"""

    def __init__(
        self,
        *,
        min_chars: int = 6,
        high_value_threshold: float = 0.70,
        business_keywords: Sequence[str] = BUSINESS_KEYWORDS,
        risk_keywords: Sequence[str] = RISK_KEYWORDS,
        spam_keywords: Sequence[str] = SPAM_KEYWORDS,
    ) -> None:
        self.min_chars = min_chars
        self.high_value_threshold = high_value_threshold
        self.business_keywords = tuple(business_keywords)
        self.risk_keywords = tuple(risk_keywords)
        self.spam_keywords = tuple(spam_keywords)

    # ------------------------------------------------------------------ #
    # 分类
    # ------------------------------------------------------------------ #
    def classify(
        self,
        messages: Sequence[RawMessage],
        *,
        duplicate: bool = False,
        price_hits: int = 0,
    ) -> FilterDecision:
        """返回分类结论。`duplicate` 由切块器跨会话判定后传入。"""
        texts = [m.content.strip() for m in messages if m.content.strip()]
        joined = "\n".join(texts)
        total_chars = sum(len(t) for t in texts)

        # 1 系统消息 / 2 媒体消息：整块都是这类内容才丢（混着业务内容的块要保留）
        if texts and all(self._is_system(t) for t in texts):
            return FilterDecision(category="system", reason="整块为系统提示", keep=False,
                                  dataset_eligible=False)
        if texts and all(MEDIA_PATTERN.match(t) for t in texts):
            return FilterDecision(category="media", reason="整块为图片/语音/文件等媒体占位",
                                  keep=False, dataset_eligible=False)

        # 3 敏感（优先于"过短"：风险不能因为字少被漏掉）
        risk_hits = self._hits(joined, self.risk_keywords)
        if risk_hits:
            return FilterDecision(
                category="sensitive",
                reason=f"命中风险词：{'、'.join(risk_hits[:3])}",
                keep=True,
                dataset_eligible=False,
                risk=True,
            )

        # 4 垃圾广告
        spam_hits = self._hits(joined, self.spam_keywords)
        has_link = "http://" in joined or "https://" in joined
        if len(spam_hits) >= 2 or (spam_hits and has_link) or (has_link and total_chars < 80):
            return FilterDecision(
                category="spam",
                reason=f"命中垃圾广告特征：{'、'.join(spam_hits[:2]) or '外链'}",
                keep=False,
                dataset_eligible=False,
            )

        # 5 过短
        if total_chars < self.min_chars or len(messages) < 2:
            return FilterDecision(
                category="too_short",
                reason=f"有效字符 {total_chars} < {self.min_chars} 或轮次 < 2",
                keep=False,
                dataset_eligible=False,
            )

        # 6 闲聊（没有业务关键词，且都是短应答）
        kw_hits = self._hits(joined, self.business_keywords)
        if not kw_hits and self._is_chitchat(texts):
            return FilterDecision(category="chitchat", reason="无业务信息的寒暄/应答",
                                  keep=False, dataset_eligible=False)

        # 7 重复
        if duplicate:
            return FilterDecision(category="duplicate", reason="与已有对话块内容重复",
                                  keep=False, dataset_eligible=False)

        # 9 高价值（8 普通有效是兜底）
        score = self.quality(messages, risk=False, price_hits=price_hits, keyword_hits=kw_hits)
        has_outcome = bool(self._hits(joined, OUTCOME_KEYWORDS))
        if (score >= self.high_value_threshold) or (has_outcome and score >= 0.45):
            return FilterDecision(
                category="high_value",
                reason=f"质量分 {score:.2f}，业务关键词 {len(kw_hits)} 个"
                       + ("，含成交信号" if has_outcome else ""),
                keep=True,
            )
        return FilterDecision(
            category="normal",
            reason=f"有效业务对话（质量分 {score:.2f}）",
            keep=True,
        )

    # ------------------------------------------------------------------ #
    # 质量分
    # ------------------------------------------------------------------ #
    def quality(
        self,
        messages: Sequence[RawMessage],
        *,
        risk: bool = False,
        price_hits: int = 0,
        keyword_hits: Sequence[str] | None = None,
    ) -> float:
        """0-1 的确定性质量分。

        权重是**显式可调**的（面试被问"0.25 怎么来的"时，
        答"业务上认为轮次最重要，且能在配置里改"比"拍脑袋"好）：
            轮次       0.25   谈得越深信息越多
            业务词覆盖 0.25   真的在聊业务，而不是寒暄
            QA 对数量  0.20   能直接变成问答/陪练素材
            业务侧字数 0.20   业务员说的内容才是"话术"
            结构完整   0.10   双方都出现，不是单方独白
        """
        stats = self.block_stats(messages)
        joined = "\n".join(m.content for m in messages)
        kw = keyword_hits if keyword_hits is not None else self._hits(joined, self.business_keywords)
        qa_count = len(self.extract_qa(messages))

        score = 0.0
        score += min(stats.turns / 8.0, 1.0) * 0.25
        score += min(len(kw) / 6.0, 1.0) * 0.25
        score += min(qa_count / 3.0, 1.0) * 0.20
        score += min(stats.business_chars / 200.0, 1.0) * 0.20
        roles = {m.role for m in messages}
        if {"customer", "business"} <= roles:
            score += 0.10
        if risk:
            score -= 0.35
        if price_hits > 0:
            score -= 0.10  # 谈价格可以，但含价格内容的样本训练价值偏低（已脱敏）
        return round(max(0.0, min(1.0, score)), 3)

    # ------------------------------------------------------------------ #
    # QA 抽取
    # ------------------------------------------------------------------ #
    def extract_qa(self, messages: Sequence[RawMessage], *, limit: int = 5) -> list[QAPair]:
        """抽出问答对（双向）。

        只认"问句 + 下一条同块消息"这种最朴素的结构 ——
        不用模型判断"这句是不是问题"，因为问号本身就是最可靠的信号，
        而模型误判会让问答库混入陈述句，下游检索质量反而下降。
        """
        pairs: list[QAPair] = []
        for i, msg in enumerate(messages):
            text = msg.content.strip()
            if not text or not text.endswith(("？", "?")):
                continue
            for nxt in messages[i + 1:i + 3]:  # 允许中间隔一条（比如对方先发个"嗯"）
                answer = nxt.content.strip()
                if not answer or answer.endswith(("？", "?")):
                    continue
                if nxt.role == "system":
                    continue
                if nxt.role == msg.role:
                    continue  # 自己问自己答不是问答对
                pairs.append(QAPair(question=text, answer=answer, asker=msg.role, ts=msg.ts))
                break
            if len(pairs) >= limit:
                break
        return pairs

    def scene_of(self, text: str) -> str:
        """按关键词判场景（规则的，不依赖模型）。"""
        for scene, keywords in SCENE_RULES:
            if any(k in text for k in keywords):
                return scene
        return "其他"

    # ------------------------------------------------------------------ #
    # 工具方法
    # ------------------------------------------------------------------ #
    def block_stats(self, messages: Iterable[RawMessage]) -> BlockStats:
        msgs = list(messages)
        return BlockStats(
            turns=len(msgs),
            chars=sum(len(m.content) for m in msgs),
            customer_chars=sum(len(m.content) for m in msgs if m.role == "customer"),
            business_chars=sum(len(m.content) for m in msgs if m.role == "business"),
        )

    def build_block(
        self,
        *,
        block_id: str,
        session_id: str,
        customer_id: str,
        customer_name: str,
        messages: list[RawMessage],
        started_at: float,
        ended_at: float,
        duplicate: bool = False,
        price_hits: int = 0,
        findings: list | None = None,
    ) -> ConversationBlock:
        """把一串消息装成对话块（含分类、质量分、QA、统计）。"""
        decision = self.classify(messages, duplicate=duplicate, price_hits=price_hits)
        score = self.quality(messages, risk=decision.risk, price_hits=price_hits)
        return ConversationBlock(
            block_id=block_id,
            session_id=session_id,
            customer_id=customer_id,
            customer_name=customer_name,
            started_at=started_at,
            ended_at=ended_at,
            messages=messages,
            category=decision.category,
            category_reason=decision.reason,
            quality_score=score,
            dataset_eligible=decision.dataset_eligible,
            qa_pairs=self.extract_qa(messages),
            price_hits=price_hits,
            findings=findings or [],
            stats=self.block_stats(messages),
        )

    @staticmethod
    def priority_of(category: str) -> int:
        return CATEGORY_PRIORITY.get(category, 99)

    # ---- 内部判定 ---- #
    @staticmethod
    def _is_system(text: str) -> bool:
        return any(marker in text for marker in SYSTEM_MARKERS)

    @staticmethod
    def _is_chitchat(texts: Sequence[str]) -> bool:
        if not texts:
            return True
        short = sum(1 for t in texts if len(t) <= 6)
        return short / len(texts) >= 0.8 and all(
            len(t) <= 8 or t.strip() in CHITCHAT_TOKENS for t in texts
        )

    @staticmethod
    def _hits(text: str, keywords: Sequence[str]) -> list[str]:
        return [k for k in keywords if k in text]
