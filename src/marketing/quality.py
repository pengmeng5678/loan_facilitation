"""渠道语感规范（Channel Spec）与素材质量分。

**"同一套文案发三个渠道"是社交获客最常见的浪费。** 三个渠道的读者心态完全不同：

    朋友圈    熟人场 → 要短、要平、要像人说话，太像广告会被划走
    抖音      陌生场 → 前 3 秒必须抓人，所以是"口播脚本"而不是"文案"
    小红书    搜索场 → 用户是来找经验的，"第一人称真实感 + 避坑"才有人看

把这三套规范**显式写进配置**（而不是塞在 prompt 里靠模型悟），
好处是可校准、可 A/B、可被评测覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelSpec:
    """单个渠道的创作规范。"""

    channel: str
    name: str
    tone: str
    min_chars: int
    max_chars: int
    hashtag_count: int
    format_hint: str
    persona: str


CHANNEL_SPECS: dict[str, ChannelSpec] = {
    "moments": ChannelSpec(
        channel="moments",
        name="朋友圈",
        tone="短、平、近。像朋友随口分享，不要像广告；可以带一点点个人感受。",
        min_chars=40,
        max_chars=220,
        hashtag_count=0,
        format_hint="3-5 行短句，不要长段落；结尾一句轻引导就够。",
        persona="你是一位金融机构从业者，在朋友圈分享行业观察。",
    ),
    "douyin": ChannelSpec(
        channel="douyin",
        name="抖音",
        tone="强钩子。前 3 秒必须让人停下来，语速快、口语化、有冲突感。",
        min_chars=120,
        max_chars=420,
        hashtag_count=3,
        format_hint="写成口播脚本：开头一句钩子，中间讲清痛点与方案，结尾给行动指令。",
        persona="你是一位短视频口播文案作者，为金融从业者写口播脚本。",
    ),
    "xiaohongshu": ChannelSpec(
        channel="xiaohongshu",
        name="小红书",
        tone="经验贴。第一人称、真实感、避坑向，像在分享自己的经历。",
        min_chars=180,
        max_chars=600,
        hashtag_count=5,
        format_hint="开头一句结论/情绪，中间分点讲清过程与坑，结尾引导评论或私信咨询。",
        persona="你是一位在小红书分享理财与负债管理经验的博主。",
    ),
}


# 钩子风格（离线模板生成时轮换使用，保证产出多样性）
HOOK_STYLES: tuple[str, ...] = ("提问式", "数字式", "对比式", "故事式")


class QualityScorer:
    """素材质量分（0-1）。

    打分维度与业务目标对齐，而不是"读起来顺不顺"：
        权重从高到低 —— 合规 > 钩子 > 痛点 > CTA > 篇幅适配 > 标签

    为什么合规权重最高：不合规的素材发出去是负资产（封号 + 监管风险），
    质量分再高也要压下去。
    """

    WEIGHTS: dict[str, float] = {
        "compliance": 0.30,
        "hook": 0.20,
        "pain": 0.18,
        "cta": 0.12,
        "length": 0.12,
        "hashtags": 0.08,
    }

    # 痛点词：客户真实处境的语言（用客户的词，不用金融术语）
    PAIN_MARKERS: tuple[str, ...] = (
        "网贷", "月供", "压力", "利息", "查询", "逾期", "征信", "被拒",
        "周转", "还款", "负债", "信用卡", "分期", "急用", "凑不出",
    )

    def score(
        self,
        *,
        channel: str,
        title: str,
        body: str,
        hashtags: list[str],
        cta: str,
        compliance_passed: bool,
    ) -> tuple[float, list[str]]:
        spec = CHANNEL_SPECS.get(channel)
        notes: list[str] = []
        parts: dict[str, float] = {}

        # ① 合规
        parts["compliance"] = 1.0 if compliance_passed else 0.0
        if not compliance_passed:
            notes.append("不合规：命中高风险话术，必须改写或转人工复核")

        # ② 钩子：第一句是否短促有力（问号/数字/短句）
        first_line = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        hook_score = 0.0
        if first_line:
            if "？" in first_line or "?" in first_line:
                hook_score += 0.5
            if any(ch.isdigit() for ch in first_line):
                hook_score += 0.3
            if len(first_line) <= 30:
                hook_score += 0.2
        parts["hook"] = min(1.0, hook_score)
        if hook_score < 0.5:
            notes.append("钩子偏弱：建议首句用提问、数字或冲突感开场")

        # ③ 痛点：是否用客户的词描述处境
        hits = [w for w in self.PAIN_MARKERS if w in body]
        parts["pain"] = min(1.0, len(hits) / 3)
        if not hits:
            notes.append("未触及客户痛点：建议写清'什么处境的人会遇到什么问题'")

        # ④ CTA
        parts["cta"] = 1.0 if cta.strip() else 0.0
        if not cta.strip():
            notes.append("缺少行动指令（CTA）：客户看完不知道该做什么")

        # ⑤ 篇幅适配
        if spec is not None:
            n = len(body)
            if spec.min_chars <= n <= spec.max_chars:
                parts["length"] = 1.0
            elif n < spec.min_chars:
                parts["length"] = max(0.0, n / spec.min_chars)
                notes.append(f"篇幅偏短（{n} 字，{spec.name}建议 {spec.min_chars}-{spec.max_chars} 字）")
            else:
                parts["length"] = max(0.0, spec.max_chars / n)
                notes.append(f"篇幅偏长（{n} 字，{spec.name}建议 {spec.min_chars}-{spec.max_chars} 字）")

            # ⑥ 标签
            if spec.hashtag_count == 0:
                parts["hashtags"] = 1.0  # 朋友圈不需要标签
            else:
                parts["hashtags"] = min(1.0, len(hashtags) / spec.hashtag_count)
                if len(hashtags) < spec.hashtag_count:
                    notes.append(f"标签偏少（{len(hashtags)}/{spec.hashtag_count}）")
        else:
            parts["length"] = 1.0
            parts["hashtags"] = 1.0

        total = sum(parts[k] * self.WEIGHTS[k] for k in self.WEIGHTS)
        return round(total, 4), notes
