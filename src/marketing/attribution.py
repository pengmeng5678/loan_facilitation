"""素材效果归因 —— 从"产出素材"升级为"产出线索"。

要回答的问题很具体：
    **哪种钩子、哪个渠道、哪条素材，真的带来了留资和成单？**

数据模型是最朴素的漏斗（impression → consult → lead → deal），
但关键在于"素材带标识"：业务员发布的每条素材都有 material_id，
后续咨询/留资/成单都能关联回来。没有这层关联，"发了素材有没有用"永远说不清，
迭代就只能靠猜。

结论由**数据推导**（而不是预设话术）：只在数据支持时才输出对应结论。
"""

from __future__ import annotations

from typing import Any

from agent_platform.logging_config import get_logger
from marketing.domain import AttributionReport, TouchRecord, channel_name, now_str

logger = get_logger(__name__)

_STAGES = ("impression", "consult", "lead", "deal")


class AttributionEngine:
    """归因引擎（纯计算，无外部依赖）。"""

    def report(
        self,
        materials: list[dict[str, Any]],
        touches: list[TouchRecord],
    ) -> AttributionReport:
        """生成归因报告。

        materials: [{"material_id","title","channel","theme"}]
        touches:   触达记录列表（含 stage）
        """
        by_channel: dict[str, dict[str, float]] = {}
        by_material: dict[str, dict[str, float]] = {}

        for t in touches:
            ch = by_channel.setdefault(t.channel, {s: 0.0 for s in _STAGES})
            ch["touches"] = ch.get("touches", 0.0) + 1
            if t.stage in ch:
                ch[t.stage] += 1

            mt = by_material.setdefault(
                t.material_id, {s: 0.0 for s in _STAGES} | {"touches": 0.0}
            )
            mt["touches"] += 1
            if t.stage in mt:
                mt[t.stage] += 1

        total_leads = sum(v.get("lead", 0.0) for v in by_channel.values())
        total_deals = sum(v.get("deal", 0.0) for v in by_channel.values())

        # 线索占比：各渠道带来的留资占全部留资的比例
        lead_share = {
            ch: round(v.get("lead", 0.0) / total_leads, 4) if total_leads > 0 else 0.0
            for ch, v in by_channel.items()
        }

        # 素材排行（按留资，其次按触达）—— 这是"哪种素材值得加量"的直接依据
        meta = {m.get("material_id"): m for m in materials}
        top_materials: list[dict[str, Any]] = []
        for mid, stats in by_material.items():
            info = meta.get(mid, {})
            # 触达记录里的素材可能已从素材库删除（下架/清理），此时不能显示成
            # 空标题 —— 那会让报告出现"（）留资 8"这种没法行动的行。
            top_materials.append({
                "material_id": mid,
                "title": info.get("title") or "(素材档案缺失)",
                "channel": info.get("channel") or _channel_of(touches, mid),
                "theme": info.get("theme", ""),
                "touches": int(stats.get("touches", 0)),
                "leads": int(stats.get("lead", 0)),
                "deals": int(stats.get("deal", 0)),
            })
        top_materials.sort(key=lambda m: (m["leads"], m["touches"]), reverse=True)

        conversion = round(total_deals / total_leads, 4) if total_leads > 0 else 0.0

        return AttributionReport(
            total_materials=len(materials),
            total_touches=len(touches),
            by_channel={ch: {k: round(v, 2) for k, v in stats.items()} for ch, stats in by_channel.items()},
            top_materials=top_materials[:10],
            lead_share=lead_share,
            conversion_rate=conversion,
            conclusions=self._conclusions(by_channel, lead_share, top_materials, conversion),
            created_at=now_str(),
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _conclusions(
        by_channel: dict[str, dict[str, float]],
        lead_share: dict[str, float],
        top_materials: list[dict[str, Any]],
        conversion: float,
    ) -> list[str]:
        """由数据推导结论：只在数据支持时才说。"""
        out: list[str] = []
        if not by_channel:
            return ["暂无触达数据，无法归因"]

        best_leads = max(by_channel.items(), key=lambda kv: kv[1].get("lead", 0.0))
        if best_leads[1].get("lead", 0.0) > 0:
            out.append(
                f"{channel_name(best_leads[0])}带来的留资最多"
                f"（{best_leads[1]['lead']:.0f} 条，占 {lead_share.get(best_leads[0], 0):.0%}）"
            )

        most_touches = max(by_channel.items(), key=lambda kv: kv[1].get("touches", 0.0))
        if most_touches[0] != best_leads[0] and most_touches[1].get("lead", 0.0) <= 0:
            out.append(
                f"{channel_name(most_touches[0])}触达量最大（{most_touches[1]['touches']:.0f} 次）"
                f"但未产生留资，建议调整钩子或减少投入"
            )

        if top_materials and top_materials[0]["leads"] > 0:
            top = top_materials[0]
            # 素材档案缺失的行不作为"加量建议"的依据（找不到素材，加不了量）
            if top["title"] != "(素材档案缺失)":
                out.append(
                    f"表现最好的素材是「{top['title'][:24]}」（{channel_name(top['channel'])}，"
                    f"留资 {top['leads']} 条），建议同主题加量产出"
                )

        if conversion > 0:
            out.append(f"素材触达 → 成单转化率 {conversion:.1%}（以留资为分母）")

        return out


def _channel_of(touches: list[TouchRecord], material_id: str) -> str:
    """从触达记录里反查某素材的渠道（素材档案缺失时的兜底）。"""
    for t in touches:
        if t.material_id == material_id:
            return t.channel
    return ""
