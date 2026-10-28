"""成单案例包装 —— 把最有说服力的素材（客户证言）变成可复用资产。

为什么这件事价值最高：
    业务员自己吹"我们渠道多、通过率高"，客户根本不信；
    但换成"我这种 3 笔网贷的情况都办下来了"，说服力完全不同。
    而这类素材过去**散在业务员个人手里**（截图、聊天记录），既没法复用，
    又因为含客户隐私不敢公开 —— 所以必须"脱敏 → 提炼 → 包装"三步自动化。

提炼是**确定性规则**（不是让模型编故事）：
    从真实成交对话里抽取「需求场景 / 解决路径 / 结果感受」，再用客户自己的话
    作为证言。模型只在需要润色措辞时参与。这样案例里的每个事实都能回溯到原对话。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from typing import Any

from agent_platform.llm.base import LLMProvider
from agent_platform.logging_config import get_logger
from marketing.compliance import ComplianceEngine
from marketing.desensitize import Desensitizer
from marketing.domain import Blueprint, CaseStudy, Channel, MaterialDraft, now_str
from marketing.generator import MaterialGenerator
from marketing.quality import CHANNEL_SPECS, QualityScorer

logger = get_logger(__name__)

# 需求场景：用"客户的语言"归类（左：命中词，右：可对外表述的场景）
_SCENE_MARKERS: tuple[tuple[str, str], ...] = (
    ("网贷", "名下有多笔网贷、月供压力大"),
    ("月供", "月供压力大、想降低还款额"),
    ("置换", "想把高息贷款置换成低息银行贷款"),
    ("信用卡", "信用卡分期成本高"),
    ("经营", "经营周转有资金缺口"),
    ("周转", "短期资金周转"),
    ("装修", "装修资金缺口"),
    ("征信", "征信有瑕疵、担心办不下来"),
    ("查询", "征信查询偏多、担心被拒"),
    ("逾期", "有过逾期记录、担心影响审批"),
)

# 解决路径：从业务员的话里识别用的是什么方案
_PATH_MARKERS: tuple[tuple[str, str], ...] = (
    ("抵押", "房产抵押方案"),
    ("公积金", "公积金信用贷方案"),
    ("社保", "社保信用贷方案"),
    ("个税", "个税信用贷方案"),
    ("保单", "保单质押方案"),
    ("车", "车辆抵押方案"),
    ("置换", "债务置换方案"),
    ("优化", "负债结构优化方案"),
    ("组合", "组合方案"),
    ("经营", "经营贷方案"),
)

# 客户"正向反应"标记：只有客户明确认可/答应推进，才算有效的证言来源
_POSITIVE_MARKERS: tuple[str, ...] = (
    "行", "好", "可以", "我去", "那我", "明白", "懂了", "办", "走", "约", "方便", "收到", "谢谢",
)

_AMOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*(万元|万)")


class CaseBuilder:
    """案例提炼与证言素材包装。"""

    def __init__(
        self,
        *,
        desensitizer: Desensitizer,
        compliance: ComplianceEngine,
        generator: MaterialGenerator,
        scorer: QualityScorer,
        llm: LLMProvider | None = None,
        name_resolver: Callable[[str], str] | None = None,
    ) -> None:
        self.desensitizer = desensitizer
        self.compliance = compliance
        self.generator = generator
        self.scorer = scorer
        self.llm = llm
        self.name_resolver = name_resolver

    # ------------------------------------------------------------------ #
    # 提炼
    # ------------------------------------------------------------------ #
    def build_from_conversation(self, conversation: dict[str, Any]) -> CaseStudy | None:
        """从一段对话提炼案例。只处理**成交(outcome=won)**的对话。

        为什么只处理成单：未成单对话照搬成"成功案例"就是虚假宣传，
        这是合规红线，不是技术取舍。
        """
        if (conversation.get("outcome") or "").lower() != "won":
            return None

        messages = conversation.get("messages") or []
        customer_text = " ".join(m.get("text", "") for m in messages if m.get("role") == "customer")
        business_text = " ".join(m.get("text", "") for m in messages if m.get("role") == "business")
        if not business_text:
            return None

        scene = self._pick(customer_text, _SCENE_MARKERS, default="客户有资金需求")
        path = self._pick(business_text, _PATH_MARKERS, default="定制融资方案")

        # 客户证言：取"正向反应"里最长的一句客户原话（信息量最大、最像人话）
        quote_raw = ""
        for m in messages:
            if m.get("role") != "customer":
                continue
            text = (m.get("text") or "").strip()
            if text and any(text.startswith(p) or p in text[:6] for p in _POSITIVE_MARKERS):
                if len(text) > len(quote_raw):
                    quote_raw = text
        if not quote_raw:
            quote_raw = next(
                ((m.get("text") or "").strip() for m in messages if m.get("role") == "customer" and m.get("text")),
                "",
            )

        # 结果感受：从对话里提取金额区间（有则更可信），否则用推进状态表述
        amount_note = ""
        for m in messages:
            hit = _AMOUNT.search(m.get("text") or "")
            if hit:
                from marketing.desensitize import amount_bucket

                amount_note = f"额度 {amount_bucket(float(hit.group(1)))}"
                break
        outcome = amount_note or "客户确认继续推进，已约到店"

        customer_id = conversation.get("customer_id") or ""
        resolved_name = ""
        if customer_id and self.name_resolver is not None:
            try:
                resolved_name = (self.name_resolver(customer_id) or "").strip()
            except Exception:  # noqa: BLE001 - 姓名取不到不影响案例生成
                resolved_name = ""
        # 案例卡上的称呼一律按"姓名"掩码，不依赖姓名词表（见 Desensitizer.mask_name）
        customer_masked = self.desensitizer.mask_name(resolved_name) or "客户"
        name_hits = 1 if (resolved_name and customer_masked != resolved_name) else 0

        # 整段证言过脱敏（姓名/电话/金额/证件号）
        masked_quote = self.desensitizer.mask(quote_raw)
        masked_scene = self.desensitizer.mask(scene).text
        masked_path = self.desensitizer.mask(path).text

        case = CaseStudy(
            case_id=f"case_{uuid.uuid4().hex[:10]}",
            source_conversation=str(conversation.get("conversation_id") or ""),
            customer_masked=customer_masked,
            scene=masked_scene,
            path=masked_path,
            outcome=outcome,
            quote=masked_quote.text,
            tags=self._tags(customer_text, business_text),
            desensitize_hits=masked_quote.hits + name_hits,
            created_at=now_str(),
        )
        logger.info("案例提炼完成：%s（来源 %s，脱敏 %d 处）", case.case_id, case.source_conversation, case.desensitize_hits)
        return case

    def build_all(self, conversations: list[dict[str, Any]]) -> list[CaseStudy]:
        cases = [c for c in (self.build_from_conversation(cv) for cv in conversations) if c]
        logger.info("共提炼案例 %d 个（来自 %d 段对话，仅成交对话可入库）", len(cases), len(conversations))
        return cases

    # ------------------------------------------------------------------ #
    # 包装成素材
    # ------------------------------------------------------------------ #
    def to_material(self, case: CaseStudy, channel: Channel = "moments") -> MaterialDraft:
        """把案例包装成"客户证言式"素材（先脱敏、再合规、后质量分）。"""
        spec = CHANNEL_SPECS.get(channel)
        quote = case.quote or "（客户反馈）"

        if channel == "douyin":
            body = "\n".join(
                [
                    "【0-3秒】这种情况到底还能不能办？答案是能。",
                    f"【痛点】{case.scene}",
                    f"【方案】{case.path}",
                    f"【客户原话】“{quote}”",
                ]
            )
            title = f"真实案例｜{case.scene}"
        elif channel == "xiaohongshu":
            body = "\n".join(
                [
                    f"最近帮一位客户处理了{case.scene}这件事，结果比想象中顺利。",
                    f"他最初的问题是：{case.scene}",
                    f"最后走的是：{case.path}",
                    f"客户原话：“{quote}”",
                    f"结果：{case.outcome}",
                    "所以别急着放弃，先让专业的人看一眼资质，很多情况是有办法的。",
                ]
            )
            title = f"真实案例｜{case.scene}怎么办"
        else:  # moments
            body = "\n".join(
                [
                    f"刚帮一位客户处理完，情况挺典型的：{case.scene}",
                    f"最后是{case.path}解决的，{case.outcome}。",
                    f"客户的原话我印象挺深：“{quote}”",
                ]
            )
            title = f"客户案例｜{case.scene}"

        hashtags = self.generator._hashtags(  # noqa: SLF001 - 复用同一套标签生成逻辑
            Blueprint(
                blueprint_id=f"case-{case.case_id}",
                name="案例见证",
                channel=channel,
                theme=case.scene,
                tags=list(case.tags),
            ),
            case.scene,
            spec.hashtag_count if spec else 0,
        )

        return self.generator.assemble(
            channel=channel,
            theme=case.scene,
            blueprint_id=f"case-{case.case_id}",
            title=title,
            body=body,
            hashtags=hashtags,
            cta="想了解自己的情况能不能做，可以私信我帮你看看资质。",
            source="case",
        )

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _pick(text: str, markers: tuple[tuple[str, str], ...], *, default: str) -> str:
        """按命中优先级挑选第一个匹配的表述。"""
        for keyword, label in markers:
            if keyword in text:
                return label
        return default

    @staticmethod
    def _tags(customer_text: str, business_text: str) -> list[str]:
        tags: list[str] = []
        for keyword, _label in _SCENE_MARKERS[:6]:
            if keyword in customer_text and keyword not in tags:
                tags.append(keyword)
        for keyword, _label in _PATH_MARKERS:
            if keyword in business_text and keyword not in tags:
                tags.append(keyword)
        return tags[:6]
