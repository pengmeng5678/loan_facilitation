"""素材生成器：**结构模板 + 渠道语感 + 生成即合规**。

生成链路固定为四步，缺一不可：
    ① 选结构模板（Blueprint）—— 决定"怎么抓人"，这是可复用资产
    ② 按渠道语感组织语言     —— 决定"像不像那个渠道的人说的话"
    ③ 合规审核（先审后发）   —— 决定"能不能发"
    ④ 质量打分               —— 决定"值不值得推给业务员用"

离线（echo）模式走**模板填充**，不靠模型"自由发挥"：
    因为素材是对外公开的，宁可产出稳定可控的合规稿，也不能产出编造事实的漂亮稿。
    接入真实模型后自动切换为 LLM 生成，但**合规与质量两道闸门不变**。
"""

from __future__ import annotations

import uuid
from typing import Any

from agent_platform.llm.base import LLMProvider, Message
from agent_platform.logging_config import get_logger
from marketing.compliance import ComplianceEngine
from marketing.domain import Blueprint, Channel, MaterialDraft, channel_name, now_str
from marketing.quality import CHANNEL_SPECS, HOOK_STYLES, QualityScorer

logger = get_logger(__name__)

# 钩子风格的固定前缀（离线模式下用它制造多样性，且不编造事实）
_STYLE_PREFIX: dict[str, str] = {
    "提问式": "很多人问：",
    "数字式": "3 点说清一件事：",
    "对比式": "同样的情况，处理方式不同，结果差很多：",
    "故事式": "上周有位客户的情况挺典型——",
}


class MaterialGenerator:
    """素材生成器。"""

    def __init__(
        self,
        llm: LLMProvider,
        compliance: ComplianceEngine,
        scorer: QualityScorer,
        blueprints: list[Blueprint],
    ) -> None:
        self.llm = llm
        self.compliance = compliance
        self.scorer = scorer
        self.blueprints = list(blueprints)
        # 只在真实模型下走 LLM 生成；echo 是离线占位实现，会产出无意义文本
        self._use_llm = getattr(llm, "name", "echo") != "echo"

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #
    def generate(self, channel: Channel, *, theme: str = "", count: int = 1) -> list[MaterialDraft]:
        """按渠道批量生成素材。

        theme 为空则在该渠道的所有模板里轮换（保证产出多样性）。
        """
        pool = [b for b in self.blueprints if b.channel == channel]
        if theme:
            matched = [b for b in pool if theme in b.theme or theme in b.name]
            pool = matched or pool
        if not pool:
            # 完全没有该渠道模板时，用其他渠道模板兜底（仍然过渠道语感与合规）
            pool = list(self.blueprints)
        if not pool:
            logger.warning("素材模板库为空，无法生成（请先运行 scripts/seed_data.py）")
            return []

        drafts: list[MaterialDraft] = []
        for i in range(max(1, count)):
            bp = pool[i % len(pool)]
            style = HOOK_STYLES[i % len(HOOK_STYLES)]
            drafts.append(self._build_one(bp, channel=channel, theme=theme or bp.theme, style=style))
        return drafts

    # ------------------------------------------------------------------ #
    # 单条生成
    # ------------------------------------------------------------------ #
    def _build_one(self, bp: Blueprint, *, channel: Channel, theme: str, style: str) -> MaterialDraft:
        spec = CHANNEL_SPECS.get(channel)

        if self._use_llm and spec is not None:
            title, body, hashtags, cta = self._from_llm(bp, spec, theme, style)
            source = "llm"
        else:
            title, body, hashtags, cta = self._from_template(bp, channel=channel, theme=theme, style=style)
            source = "template"

        return self.assemble(
            channel=channel,
            theme=theme,
            blueprint_id=bp.blueprint_id,
            title=title,
            body=body,
            hashtags=hashtags,
            cta=cta,
            source=source,
        )

    def assemble(
        self,
        *,
        channel: Channel,
        theme: str,
        blueprint_id: str,
        title: str,
        body: str,
        hashtags: list[str],
        cta: str,
        source: str,
    ) -> MaterialDraft:
        """组装成素材：**过合规 → 过质量分 → 定状态**。

        所有入口（模板/LLM/案例包装）都必须走这里，保证"先审后发"无法被绕过 ——
        这是把合规做成**流程约束**而不是"记得检查一下"。
        """
        full_text = f"{title}\n{body}\n{cta}"

        # ---- ③ 合规：生成即合规（不合格自动改写，改写后可再过则标记 sanitized）----
        report = self.compliance.check_and_fix(full_text)
        final_body = report.sanitized_text or body
        final_title = title
        final_cta = cta
        if report.sanitized and report.sanitized_text:
            # 改写后的文本是三段拼一起的，按换行还原正文与 CTA
            lines = report.sanitized_text.splitlines()
            if lines:
                final_title = lines[0]
            if cta and lines and cta in lines[-1]:
                final_cta = lines[-1]
                final_body = "\n".join(lines[1:-1]) if len(lines) > 2 else ""
            else:
                final_body = "\n".join(lines[1:]) if len(lines) > 1 else ""

        if report.passed:
            status = "sanitized" if report.sanitized else "approved"
        else:
            status = "rejected"

        # ---- ④ 质量分 ----
        q_score, q_notes = self.scorer.score(
            channel=channel,
            title=final_title,
            body=final_body,
            hashtags=hashtags,
            cta=final_cta,
            compliance_passed=report.passed,
        )

        if status == "rejected":
            q_notes.insert(0, "已拦截：合规未通过，不可发布")

        return MaterialDraft(
            draft_id=f"md_{uuid.uuid4().hex[:10]}",
            channel=channel,
            theme=theme,
            blueprint_id=blueprint_id,
            title=final_title,
            body=final_body,
            hashtags=hashtags,
            cta=final_cta,
            status=status,
            compliance=report,
            quality_score=q_score,
            quality_notes=q_notes,
            source=source,
            created_at=now_str(),
        )

    # ------------------------------------------------------------------ #
    # 离线模板路径
    # ------------------------------------------------------------------ #
    def _from_template(
        self, bp: Blueprint, *, channel: Channel, theme: str, style: str
    ) -> tuple[str, str, list[str], str]:
        spec = CHANNEL_SPECS.get(channel)
        prefix = _STYLE_PREFIX.get(style, "")
        hook = f"{prefix}{bp.hook}".strip()

        if channel == "douyin":
            body = "\n".join(
                part
                for part in (
                    f"【0-3秒】{hook}",
                    f"【痛点】{bp.pain}",
                    f"【方案】{bp.solution}",
                )
                if part.strip("【】0-3秒痛点方案")
            )
        elif channel == "xiaohongshu":
            body = "\n".join(
                part
                for part in (
                    hook,
                    f"先说结论：{bp.solution}",
                    f"我遇到的情况是这样的：{bp.pain}",
                    "很多人卡在这里，其实思路理顺了并不难。",
                )
                if part.strip()
            )
        else:  # moments：短平近，短句成行
            body = "\n".join(part for part in (hook, bp.pain, bp.solution) if part.strip())

        title = f"{theme}｜{bp.name}" if theme else bp.name
        hashtags = self._hashtags(bp, theme, spec.hashtag_count if spec else 0)
        return title, body, hashtags, bp.cta

    # ------------------------------------------------------------------ #
    # LLM 路径
    # ------------------------------------------------------------------ #
    def _from_llm(
        self, bp: Blueprint, spec: Any, theme: str, style: str
    ) -> tuple[str, str, list[str], str]:
        schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "hashtags": {"type": "array", "items": {"type": "string"}},
                "cta": {"type": "string"},
            },
            "required": ["title", "body", "cta"],
        }
        system = (
            f"{spec.persona}\n渠道：{spec.name}；语感要求：{spec.tone}\n"
            f"格式要求：{spec.format_hint}；篇幅 {spec.min_chars}-{spec.max_chars} 字。\n"
            "**合规硬约束**：不得承诺审批结果、不得承诺利率、不得提'不看征信/黑户可做'、"
            "不得使用绝对化用语（最好/第一/唯一）。所有效果表述必须留'以银行审批为准'。"
        )
        user = (
            f"主题：{theme or bp.theme}\n"
            f"钩子（{style}）：{bp.hook}\n痛点：{bp.pain}\n解决方案：{bp.solution}\n"
            f"行动指令方向：{bp.cta}\n"
            "请输出 title / body / hashtags / cta 四个字段。"
        )
        try:
            data = self.llm.complete_json(
                [Message(role="system", content=system), Message(role="user", content=user)],
                schema,
            )
            return (
                str(data.get("title") or f"{theme}｜{bp.name}"),
                str(data.get("body") or ""),
                [str(h).lstrip("#") for h in (data.get("hashtags") or [])],
                str(data.get("cta") or bp.cta),
            )
        except Exception as exc:  # noqa: BLE001 - LLM 失败必须能退回模板，不能断供
            logger.warning("LLM 素材生成失败，退回模板生成：%s", exc)
            return self._from_template(bp, channel=spec.channel, theme=theme, style=style)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _hashtags(bp: Blueprint, theme: str, count: int) -> list[str]:
        if count <= 0:
            return []
        base = [t for t in bp.tags if t]
        if theme and theme not in base:
            base.insert(0, theme)
        # 补足到目标数量（用主题词派生的通用标签，不编造具体事件）
        filler = ["负债管理", "资金规划", "征信知识", "贷款攻略", "避坑指南", "银行产品"]
        for f in filler:
            if len(base) >= count:
                break
            if f not in base:
                base.append(f)
        return base[:count]

    # ------------------------------------------------------------------ #
    def channel_hint(self, channel: str) -> dict[str, Any]:
        spec = CHANNEL_SPECS.get(channel)
        if spec is None:
            return {"channel": channel, "name": channel_name(channel)}
        return {
            "channel": spec.channel,
            "name": spec.name,
            "tone": spec.tone,
            "format_hint": spec.format_hint,
            "length": [spec.min_chars, spec.max_chars],
            "hashtag_count": spec.hashtag_count,
        }
