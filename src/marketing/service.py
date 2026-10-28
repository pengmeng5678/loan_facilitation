"""素材推广服务门面（项目七）。

对外五件事：
    generate_materials  素材批量生成 + 渠道适配（含合规与质量分）
    pack_cases          成单案例脱敏包装（客户证言式素材）
    plan_activation     未成单客户激活计划（对症 + 防骚扰）
    audit               对任意文本做合规审核（业务员自己写的内容也能先审）
    attribute           素材 → 触达 → 留资 → 成单 的归因

**边界**：AI 只产出素材与建议，**发布/发送动作由业务员执行**
（且对外发送走平台 HITL 审批，见 agent.py）。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from agent_platform.config import Settings, get_settings
from agent_platform.llm.base import LLMProvider
from agent_platform.llm.providers import build_llm
from agent_platform.logging_config import get_logger
from agent_platform.tools.registry import ToolRegistry
from agent_platform.tools.spec import ToolSpec
from marketing.activation import ActivationEngine, DormantCustomer
from marketing.attribution import AttributionEngine
from marketing.cases import CaseBuilder
from marketing.compliance import ComplianceEngine
from marketing.desensitize import Desensitizer
from marketing.domain import (
    ActivationPlan,
    AttributionReport,
    Blueprint,
    CaseStudy,
    Channel,
    ComplianceReport,
    MarketingStats,
    MaterialDraft,
    TouchRecord,
    TouchStage,
    channel_name,
)
from marketing.generator import MaterialGenerator
from marketing.quality import CHANNEL_SPECS, QualityScorer
from marketing.store import MarketingStore

logger = get_logger(__name__)


class MarketingService:
    """销售素材推广与客户激活服务。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        llm: LLMProvider | None = None,
        name_resolver: Callable[[str], str] | None = None,
        conversation_provider: Callable[[], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.llm = llm or build_llm(self.settings)

        # 数据来源全部通过 provider 注入 —— 本项目不直接 import 项目二/四，保持解耦
        self.name_resolver = name_resolver
        self.conversation_provider = conversation_provider

        # ---- 数据层 ----
        self.store = MarketingStore(self.settings.marketing_abs_dir / "marketing.sqlite")
        self.blueprints: list[Blueprint] = []
        self.dormant_customers: list[DormantCustomer] = []
        self._seed_performance: list[dict[str, Any]] = []

        # ---- 能力组件 ----
        self.desensitizer = Desensitizer(full_mask=False)
        self.compliance = ComplianceEngine(llm=self.llm)
        self.scorer = QualityScorer()
        self.generator = MaterialGenerator(self.llm, self.compliance, self.scorer, self.blueprints)
        self.case_builder = CaseBuilder(
            desensitizer=self.desensitizer,
            compliance=self.compliance,
            generator=self.generator,
            scorer=self.scorer,
            llm=self.llm,
            name_resolver=self.name_resolver,
        )
        self.activation = ActivationEngine(
            self.settings, default_channel=self.settings.marketing_default_channel
        )
        self.attribution = AttributionEngine()

        self.cases: list[CaseStudy] = []
        self._last_plan: ActivationPlan | None = None

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #
    def bootstrap(self) -> dict[str, Any]:
        """加载模板库 / 沉睡客户 / 素材效果数据，并把成交对话打包成案例。"""
        self._load_blueprints()
        self._load_dormant_customers()
        self._load_seed_performance()

        # 素材生成器持有的是同一份模板列表，加载后需要重新绑定
        self.generator.blueprints = self.blueprints

        # 已知姓名 → 供脱敏使用（从客户档案注入的 provider 取不到全量，故额外从文件读）
        self._load_known_names()

        # 案例：仅成交对话可入库
        conversations = self._conversations()
        self.cases = self.case_builder.build_all(conversations)
        for case in self.cases:
            self.store.save_case(case)

        # 冷启动素材包：素材库为空时，按模板各生成一条并入库。
        # 为什么要有这一步：激活计划与归因都以"已有可发布素材"为前提，
        # 否则第一次上线（或清库重启后）业务员打开就是空的，功能等于没有。
        if self.store.material_count() == 0 and self.blueprints:
            seeded = 0
            for bp in self.blueprints:
                drafts = self.generate_materials(
                    bp.channel, theme=bp.theme, count=1, persist=True
                )
                seeded += len(drafts)
            logger.info("冷启动素材包：按 %d 个模板生成 %d 条素材", len(self.blueprints), seeded)

        stats = {
            "blueprints": len(self.blueprints),
            "dormant_customers": len(self.dormant_customers),
            "cases": len(self.cases),
            "seed_performance": len(self._seed_performance),
            "materials_in_db": self.store.material_count(),
            "channels": sorted({b.channel for b in self.blueprints}),
        }
        logger.info("素材推广服务初始化完成：%s", stats)
        return stats

    def _load_blueprints(self) -> None:
        path = self.settings.marketing_abs_dir / "blueprints.json"
        if not path.exists():
            logger.warning("未找到素材模板库：%s（请先运行 scripts/seed_data.py）", path)
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.blueprints = [Blueprint(**item) for item in raw]
        logger.info("加载素材模板：%d 条", len(self.blueprints))

    def _load_dormant_customers(self) -> None:
        path = self.settings.marketing_abs_dir / "dormant_customers.json"
        if not path.exists():
            logger.warning("未找到沉睡客户数据：%s", path)
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.dormant_customers = [DormantCustomer(**item) for item in raw]
        logger.info("加载待激活客户：%d 位", len(self.dormant_customers))

    def _load_seed_performance(self) -> None:
        """加载素材效果种子数据（用于演示归因；真实环境由埋点实时写入）。

        注意种子数据按 **渠道 + 主题** 标注，而不是按 material_id：
        素材 id 是运行时生成的（`mat_md_xxxx`），写死在种子文件里必然对不上，
        归因报告就会满屏"素材档案缺失"。真正算归因时再解析到当前素材库里的
        同渠道同主题素材上（见 `_seed_touches`）。
        """
        self._seed_performance = []
        path = self.settings.marketing_abs_dir / "material_performance.json"
        if not path.exists():
            return
        raw = json.loads(path.read_text(encoding="utf-8"))
        self._seed_performance = [item for item in raw if isinstance(item, dict)]
        logger.info("加载素材效果种子：%d 组（渠道×主题）", len(self._seed_performance))

    def _seed_touches(self) -> list[TouchRecord]:
        """把"渠道×主题"的效果种子解析成触达记录（挂到当前素材库里的实际素材上）。"""
        materials = self.store.list_materials(limit=500)
        touches: list[TouchRecord] = []
        for item in self._seed_performance:
            channel = str(item.get("channel") or "")
            theme = str(item.get("theme") or "")
            material = next(
                (m for m in materials if m.channel == channel and m.theme == theme), None
            ) or next((m for m in materials if theme and m.theme == theme), None)
            if material is None:
                continue
            mid = material.material_id or f"mat_{material.draft_id}"
            for stage, count in (item.get("funnel") or {}).items():
                if stage not in ("impression", "consult", "lead", "deal"):
                    continue
                for i in range(int(count)):
                    touches.append(TouchRecord(
                        touch_id=f"tch_{uuid.uuid4().hex[:10]}",
                        material_id=mid,
                        channel=material.channel,
                        customer_id=f"seed_{mid}_{stage}_{i}",
                        stage=stage,  # type: ignore[arg-type]
                        ts=str(item.get("ts") or ""),
                    ))
        return touches

    def _load_known_names(self) -> None:
        """从客户档案里取出姓名（已脱敏形式），供证言脱敏使用。"""
        path = self.settings.profile_abs_dir / "customers.json"
        if not path.exists():
            return
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        names: list[str] = []
        for row in rows:
            for key in ("name", "name_masked", "customer_name"):
                value = str(row.get(key) or "").strip()
                if value and "*" not in value:
                    names.append(value)
        if names:
            self.desensitizer.known_names = sorted(set(names), key=len, reverse=True)

    def _conversations(self) -> list[dict[str, Any]]:
        if self.conversation_provider is not None:
            try:
                return list(self.conversation_provider() or [])
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取对话语料失败：%s", exc)
        # 兜底：直接读演示数据文件
        path = self.settings.policy_abs_dir / "conversations.jsonl"
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows

    # ------------------------------------------------------------------ #
    # 能力一：素材生成
    # ------------------------------------------------------------------ #
    def generate_materials(
        self, channel: Channel | str, *, theme: str = "", count: int = 1, persist: bool = True
    ) -> list[MaterialDraft]:
        """批量生成素材（自动过合规与质量分）。"""
        drafts = self.generator.generate(channel, theme=theme, count=count)  # type: ignore[arg-type]
        if persist:
            for d in drafts:
                material_id = self.store.save_material(d)
                d.material_id = material_id
                if d.compliance is not None:
                    self.store.save_audit(
                        d.compliance, ref_id=material_id, stage="material_generate",
                        text=f"{d.title}\n{d.body}",
                    )
                self.store.save_material(d)  # 回写 material_id
        return drafts

    # ------------------------------------------------------------------ #
    # 能力二：案例包装
    # ------------------------------------------------------------------ #
    def pack_cases(self, *, channel: Channel = "moments") -> list[MaterialDraft]:
        """把所有已提炼案例包装成证言式素材。"""
        if not self.cases:
            logger.warning("暂无可用案例（只有成交对话才能入库）")
            return []
        drafts = [self.case_builder.to_material(case, channel) for case in self.cases]
        for d in drafts:
            material_id = self.store.save_material(d)
            d.material_id = material_id
            if d.compliance is not None:
                self.store.save_audit(
                    d.compliance, ref_id=material_id, stage="case_material", text=f"{d.title}\n{d.body}"
                )
            self.store.save_material(d)
        return drafts

    # ------------------------------------------------------------------ #
    # 能力三：客户激活
    # ------------------------------------------------------------------ #
    def plan_activation(self, *, limit: int = 20, persist: bool = True) -> ActivationPlan:
        """生成未成单客户激活计划。"""
        materials = self.store.list_materials(status="approved") + self.store.list_materials(status="sanitized")
        # 已触达次数来自触达流水 —— 防骚扰的"触达上限"判定必须用真实数据，
        # 不能只看客户档案里的 last_touch_days（那是离线快照，会漂移）。
        plan = self.activation.plan(
            self.dormant_customers,
            materials,
            limit=limit,
            activated_counts=self.store.touch_counts_by_customer(),
        )
        self._last_plan = plan
        if persist:
            self.store.save_plan(plan)
        return plan

    # ------------------------------------------------------------------ #
    # 能力四：合规审核
    # ------------------------------------------------------------------ #
    def audit(self, text: str, *, ref_id: str = "") -> ComplianceReport:
        """对任意文本做合规审核（先审后发）。"""
        report = self.compliance.check_and_fix(text)
        self.store.save_audit(report, ref_id=ref_id or "adhoc", stage="manual_audit", text=text)
        return report

    # ------------------------------------------------------------------ #
    # 能力五：归因
    # ------------------------------------------------------------------ #
    def record_touch(
        self, material_id: str, stage: TouchStage, *, channel: Channel | str = "moments", customer_id: str = ""
    ) -> TouchRecord:
        """记录一次素材触达（曝光/咨询/留资/成单）。"""
        touch = TouchRecord(
            touch_id=f"tch_{uuid.uuid4().hex[:10]}",
            material_id=material_id,
            channel=channel,  # type: ignore[arg-type]
            customer_id=customer_id,
            stage=stage,
        )
        self.store.save_touch(touch)
        return touch

    def attribute(self, *, material_id: str = "") -> AttributionReport:
        """生成归因报告（有指定素材时只看该素材）。"""
        materials = [
            {"material_id": m.material_id, "title": m.title, "channel": m.channel, "theme": m.theme}
            for m in self.store.list_materials(limit=500)
        ]
        touches = self.store.list_touches(material_id=material_id, limit=5000)
        if not touches and not material_id:
            # 库里没有实时触达数据时，用种子效果数据兜底（演示/冷启动用）
            touches = self._seed_touches()
        return self.attribution.report(materials, touches)

    # ------------------------------------------------------------------ #
    # 工具注册
    # ------------------------------------------------------------------ #
    def register_tools(self, registry: ToolRegistry) -> list[str]:
        registered: list[str] = []

        def _generate(channel: str = "moments", theme: str = "", count: int = 1) -> dict[str, Any]:
            drafts = self.generate_materials(channel, theme=theme, count=count)  # type: ignore[arg-type]
            return {
                "count": len(drafts),
                "channel": channel,
                "channel_name": channel_name(channel),
                "materials": [d.model_dump(mode="json") for d in drafts],
                "texts": [d.to_text() for d in drafts],
                "rejected": sum(1 for d in drafts if d.status == "rejected"),
                "needs_human": sum(1 for d in drafts if d.compliance and d.compliance.needs_human),
            }

        registry.register(ToolSpec(
            name="marketing.generate_materials",
            description=(
                "按渠道批量生成营销素材：朋友圈/抖音/小红书自动适配语感，"
                "结构为 钩子→痛点→方案→CTA；**生成即过合规与质量分**。"
                "status=rejected 的素材不可发布，需人工处理。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "channel": {"type": "string", "description": "moments | douyin | xiaohongshu"},
                    "theme": {"type": "string", "description": "主题，如 债务置换 / 案例见证"},
                    "count": {"type": "integer", "description": "生成条数，默认 1"},
                },
                "required": ["channel"],
            },
            handler=_generate,
            tags=("marketing", "generate", "write"),
        ))
        registered.append("marketing.generate_materials")

        def _cases(channel: str = "moments") -> dict[str, Any]:
            drafts = self.pack_cases(channel=channel)  # type: ignore[arg-type]
            return {
                "count": len(drafts),
                "cases": [c.model_dump(mode="json") for c in self.cases],
                "materials": [d.model_dump(mode="json") for d in drafts],
                "texts": [d.to_text() for d in drafts],
            }

        registry.register(ToolSpec(
            name="marketing.pack_cases",
            description=(
                "把成交对话提炼成客户证言式素材（姓名/电话/金额全部脱敏，先审后发）。"
                "只有成交(won)对话可入库 —— 未成单对话照搬成'成功案例'属违规。"
            ),
            parameters={
                "type": "object",
                "properties": {"channel": {"type": "string", "description": "目标渠道"}},
                "required": [],
            },
            handler=_cases,
            tags=("marketing", "case", "write"),
        ))
        registered.append("marketing.pack_cases")

        def _activation(limit: int = 10) -> dict[str, Any]:
            plan = self.plan_activation(limit=limit)
            payload = plan.model_dump(mode="json")
            payload["plan_text"] = plan.to_text()
            return payload

        registry.register(ToolSpec(
            name="marketing.plan_activation",
            description=(
                "为未成单客户生成激活计划：按犹豫原因匹配对症素材，给出建议发送时间与优先级。"
                "内置防骚扰（沉睡阈值/触达上限/静默期）。"
            ),
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "最多生成多少条任务"}},
                "required": [],
            },
            handler=_activation,
            tags=("marketing", "activation", "write"),
        ))
        registered.append("marketing.plan_activation")

        def _audit(text: str) -> dict[str, Any]:
            report = self.audit(text)
            payload = report.model_dump(mode="json")
            payload["report_text"] = report.to_text()
            return payload

        registry.register(ToolSpec(
            name="marketing.audit_text",
            description=(
                "对任意文本做金融广告合规审核（规则保召回 + LLM 保精度），"
                "命中承诺性话术/规避征信/绝对化用语等高风险项会 needs_human=true。"
            ),
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "待审文本"}},
                "required": ["text"],
            },
            handler=_audit,
            tags=("marketing", "audit", "read"),
        ))
        registered.append("marketing.audit_text")

        def _attribute(material_id: str = "") -> dict[str, Any]:
            report = self.attribute(material_id=material_id)
            payload = report.model_dump(mode="json")
            payload["report_text"] = report.to_text()
            return payload

        registry.register(ToolSpec(
            name="marketing.attribute",
            description=(
                "素材效果归因：各渠道触达/留资/成单与线索占比、表现最好的素材、"
                "以及下一步加量建议。"
            ),
            parameters={
                "type": "object",
                "properties": {"material_id": {"type": "string", "description": "只看某条素材（可空）"}},
                "required": [],
            },
            handler=_attribute,
            tags=("marketing", "attribution", "read"),
        ))
        registered.append("marketing.attribute")

        def _activate_customer(customer_id: str) -> dict[str, Any]:
            """把一条激活任务整理成可直接发送的触达包（发送动作本身走 HITL）。"""
            tasks = self.store.list_activation_tasks(limit=200)
            task = next((t for t in tasks if t.customer_id == customer_id), None)
            if task is None:
                return {"found": False, "customer_id": customer_id}
            # 任务里存的是素材**草稿号**（md_xxx），而素材库里主键是 mat_md_xxx，
            # 两个都要试一下 —— 否则取不到正文，触达包会是空消息。
            material = next(
                (
                    m
                    for m in self.store.list_materials(limit=500)
                    if m.draft_id == task.material_draft_id or m.material_id == task.material_draft_id
                ),
                None,
            )
            return {
                "found": True,
                "customer_id": customer_id,
                "name_masked": task.name_masked,
                "channel": task.channel,
                "channel_name": channel_name(task.channel),
                "material_id": material.material_id if material else "",
                "material_title": task.material_title,
                "material_text": material.to_text() if material else "",
                "suggested_send_at": task.suggested_send_at,
                "reason": task.reason,
                # 发给客户的只有素材正文，不带内部标注（状态/质量分是给业务员看的）
                "message_body": (
                    f"{material.body}\n{material.cta}" if material and material.cta else
                    (material.body if material else "")
                ),
            }

        registry.register(ToolSpec(
            name="marketing.activate_customer",
            description=(
                "取出某位客户的激活触达包（渠道 + 素材全文 + 建议发送时间）。"
                "注意：**实际发送必须经过人工审批**，此工具只做准备工作。"
            ),
            parameters={
                "type": "object",
                "properties": {"customer_id": {"type": "string"}},
                "required": ["customer_id"],
            },
            handler=_activate_customer,
            tags=("marketing", "activation", "read"),
        ))
        registered.append("marketing.activate_customer")

        logger.info("素材推广服务注册工具 %d 个：%s", len(registered), registered)
        return registered

    # ------------------------------------------------------------------ #
    # 观测
    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, Any]:
        audit = self.store.audit_stats()
        materials_total = self.store.material_count()
        st = MarketingStats(
            blueprints=len(self.blueprints),
            materials=materials_total,
            approved=self.store.material_count("approved"),
            sanitized=self.store.material_count("sanitized"),
            rejected=self.store.material_count("rejected"),
            cases=len(self.cases),
            dormant_customers=len(self.dormant_customers),
            activation_tasks=len(self.store.list_activation_tasks(limit=1000)),
            blocked_materials=audit["blocked"],
            touches=len(self.store.list_touches(limit=100000)),
            channels=sorted(CHANNEL_SPECS),
        )
        return {
            **st.model_dump(mode="json"),
            "audit": audit,
            "seed_performance": len(self._seed_performance),
        }

    def channel_specs(self) -> list[dict[str, Any]]:
        return [self.generator.channel_hint(c) for c in CHANNEL_SPECS]

    def close(self) -> None:
        self.store.close()
