"""未成单客户激活 —— 把"还在考虑"的客户重新拉回对话。

前提：客户的犹豫是**有原因的**，所以激活不能群发，必须"对症"：
    嫌利率高 → 给利率科普与费用透明说明
    担心办不下来 → 给同资质客户的案例见证
    一直拖延 → 给流程科普（降低心理门槛）+ 案例
    在比价 → 给服务对比与合规价值（而不是跟着降价）

同时必须有**防骚扰机制**（这是这个模块能不能上线的关键）：
    ① 达沉睡阈值才激活（几天没互动才算沉睡，刚聊完就催是骚扰）；
    ② 同一客户有触达上限与静默期（冷却天数）；
    ③ 建议发送时间避开休息时段与周末 —— 晚上 10 点发消息的转化是负的。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from agent_platform.config import Settings
from agent_platform.logging_config import get_logger
from marketing.domain import ActivationPlan, ActivationTask, Channel, MaterialDraft, channel_name

logger = get_logger(__name__)


class DormantCustomer(BaseModel):
    """一位待激活的未成单客户（由智能客服/数据中台沉淀）。"""

    customer_id: str
    name_masked: str = ""
    dormant_days: int = Field(0, description="距最后一次互动天数")
    objection: str = Field("", description="犹豫原因，如 利率高 / 担心办不下来 / 再考虑")
    concern: str = Field("", description="关注点，如 利率 / 征信 / 放款速度")
    intent: float = Field(0.5, ge=0, le=1, description="意向分（来自智能客服的意向评级）")
    last_touch_days: int = Field(999, description="距上次被激活触达的天数")


# 犹豫原因 → 素材主题（对症下药的核心映射，可人工维护、可 A/B）
_OBJECTION_TO_THEME: tuple[tuple[tuple[str, ...], str], ...] = (
    (("利率", "利息", "利率高", "贵", "费用", "服务费", "手续费"), "费用透明"),
    (("办不下来", "通过率", "能不能办", "征信", "查询", "逾期", "资质"), "征信解读"),
    (("考虑", "商量", "没时间", "再说", "等等", "想一下"), "案例见证"),
    (("别家", "对比", "便宜", "更低"), "服务对比"),
    (("流程", "多久", "麻烦", "资料"), "流程科普"),
)

# 关注点 → 渠道偏好：征信/利率这类需要解释的放朋友圈（熟人信任场），
# 简单触达放抖音（陌生场拉新）。这条映射是可调的业务规则，不是技术约束。
_CONCERN_TO_CHANNEL: tuple[tuple[tuple[str, ...], Channel], ...] = (
    (("流程", "介绍", "科普"), "douyin"),
)


class ActivationEngine:
    """激活计划引擎。"""

    def __init__(self, settings: Settings, *, default_channel: str = "moments") -> None:
        self.settings = settings
        self.default_channel: Channel = default_channel  # type: ignore[assignment]

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def plan(
        self,
        customers: list[DormantCustomer],
        materials: list[MaterialDraft],
        *,
        now: datetime | None = None,
        limit: int = 20,
        activated_counts: dict[str, int] | None = None,
    ) -> ActivationPlan:
        """生成激活计划（每个客户一条任务，含渠道、素材、建议时间、理由）。

        activated_counts: 各客户**历史上被触达的次数**（来自触达流水）。
        用来兜住"触达上限"这条防骚扰硬规则 —— 光靠客户档案里的
        last_touch_days 不够：那只是离线快照，会被反复写、会漂移。
        """
        now = now or datetime.now()
        counts = activated_counts or {}
        available = [m for m in materials if m.status in ("approved", "sanitized")]

        tasks: list[ActivationTask] = []
        skipped: list[dict[str, object]] = []

        if not available:
            logger.warning("无可发布素材，激活计划为空（请先生成素材）")

        for c in customers:
            # ① 未达沉睡阈值 → 不打扰
            if c.dormant_days < self.settings.marketing_dormant_days:
                skipped.append({
                    "customer_id": c.customer_id, "name_masked": c.name_masked,
                    "reason": f"未达沉睡阈值（{c.dormant_days}<{self.settings.marketing_dormant_days} 天）",
                })
                continue

            # ② 静默期：距上次触达太近 → 不打扰
            if c.last_touch_days < self.settings.marketing_activation_cooldown_days:
                skipped.append({
                    "customer_id": c.customer_id, "name_masked": c.name_masked,
                    "reason": f"处于静默期（{c.last_touch_days}<"
                              f"{self.settings.marketing_activation_cooldown_days} 天）",
                })
                continue

            # ③ 触达总数上限：同一位客户累计被触达太多次 → 不打扰（防骚扰硬上限）
            already = counts.get(c.customer_id, 0)
            if already >= self.settings.marketing_max_activation_per_customer:
                skipped.append({
                    "customer_id": c.customer_id, "name_masked": c.name_masked,
                    "reason": f"已达触达上限（{already}>="
                              f"{self.settings.marketing_max_activation_per_customer} 次）",
                })
                continue

            if not available:
                skipped.append({
                    "customer_id": c.customer_id, "name_masked": c.name_masked,
                    "reason": "无可用素材",
                })
                continue

            theme = self._theme_for(c)
            preferred = self._channel_for(c, theme)
            material = self._pick_material(available, channel=preferred, theme=theme)

            tasks.append(ActivationTask(
                task_id=f"act_{uuid.uuid4().hex[:10]}",
                customer_id=c.customer_id,
                name_masked=c.name_masked or c.customer_id,
                dormant_days=c.dormant_days,
                trigger=c.objection or c.concern or "长期未互动",
                concern=c.concern,
                # 渠道以**素材的实际渠道**为准：
                # 选素材时优先"同渠道+同主题"，但找不到时以"对症"优先，
                # 这时若仍标成偏好渠道，就会把小红书稿件发到朋友圈 —— 语感当场穿帮。
                channel=material.channel,
                material_draft_id=material.draft_id,
                material_title=material.title,
                suggested_send_at=self._suggest_send_at(now).strftime("%Y-%m-%d %H:%M"),
                priority=self._priority(c),
                reason=self._reason(c, theme, material, preferred=preferred),
                status="pending",
            ))

        tasks.sort(key=lambda t: t.priority, reverse=True)
        tasks = tasks[:limit]
        plan = ActivationPlan(
            plan_id=f"plan_{uuid.uuid4().hex[:10]}",
            generated=len(tasks),
            tasks=tasks,
            skipped=skipped,
            stats={
                "customers": len(customers),
                "generated": len(tasks),
                "skipped": len(skipped),
                "channels": len({t.channel for t in tasks}),
            },
        )
        logger.info("激活计划 %s：生成 %d 条，跳过 %d 位（防骚扰）", plan.plan_id, plan.generated, len(skipped))
        return plan

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _theme_for(c: DormantCustomer) -> str:
        text = f"{c.objection} {c.concern}"
        for keywords, theme in _OBJECTION_TO_THEME:
            if any(k in text for k in keywords):
                return theme
        return "案例见证"  # 兜底用案例（最通用、最有说服力）

    def _channel_for(self, c: DormantCustomer, theme: str) -> Channel:
        text = f"{c.objection} {c.concern}"
        for keywords, channel in _CONCERN_TO_CHANNEL:
            if any(k in text for k in keywords):
                return channel
        return self.default_channel

    @staticmethod
    def _pick_material(
        materials: list[MaterialDraft], *, channel: Channel, theme: str
    ) -> MaterialDraft:
        """优先选"同渠道 + 同主题"的素材；退而求其次同主题；再退同渠道；最后任意。

        为什么"同主题"排在"同渠道"前面：激活的成败取决于**是否对症**
        （客户嫌贵就给费用透明的说明），渠道只是承载方式，可以随素材走。
        """
        for predicate in (
            lambda m: m.channel == channel and m.theme == theme,
            lambda m: m.theme == theme,
            lambda m: m.channel == channel,
            lambda m: True,
        ):
            for m in materials:
                if predicate(m):
                    return m
        return materials[0]

    @staticmethod
    def _priority(c: DormantCustomer) -> float:
        """优先级 = 意向 × 沉睡程度。

        为什么乘而不是加：高意向但刚联系过的客户不该排在前面（会显得很急），
        低意向但沉了很久的客户也可以缓一缓。两者都高才最值得先触达。
        """
        dormant_factor = min(1.0, c.dormant_days / 30.0)
        return round(c.intent * dormant_factor, 4)

    @staticmethod
    def _reason(c: DormantCustomer, theme: str, material: MaterialDraft, *, preferred: Channel) -> str:
        note = "" if material.channel == preferred else "（同渠道无对症素材，按素材原渠道发布）"
        return (
            f"沉睡 {c.dormant_days} 天；犹豫点「{c.objection or '未明确'}」"
            f"→ 用「{theme}」主题素材对症触达"
            f"（{channel_name(material.channel)}｜{material.title}）{note}"
        )

    @staticmethod
    def _suggest_send_at(now: datetime) -> datetime:
        """建议发送时间：工作日的 10:00 或 19:30。

        这两段是触达效果最好的时间窗（上午通勤后/晚饭后），
        深夜与周末发送会显著拉低回复率，甚至引起反感。
        """
        if now.hour < 10:
            candidate = now.replace(hour=10, minute=0, second=0, microsecond=0)
        elif now.hour < 19:
            candidate = now.replace(hour=19, minute=30, second=0, microsecond=0)
        else:
            candidate = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        while candidate.weekday() >= 5:  # 跳过周六周日
            candidate += timedelta(days=1)
        return candidate
