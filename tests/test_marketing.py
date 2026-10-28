"""项目七（销售素材推广与客户激活 AI）测试。

覆盖五条业务主线，每条都测"业务判据"而不是"函数能跑"：
    ① 素材生成：渠道语感（字数/标签）与"生成即合规"
    ② 合规审核：规则召回、自动改写、改不动就拦截并转人工（先审后发）
    ③ 案例包装：只有成交对话可入库、姓名/电话/金额必须脱敏
    ④ 客户激活：对症匹配 + 防骚扰（沉睡阈值 / 静默期 / 触达上限）
    ⑤ 效果归因：漏斗计算与"由数据推导结论"（无数据时不许编结论）
外加 Agent 三件事：意图路由、作用域裁剪（**未要求发送时看不到发送工具**）、
以及"发布必须走人工审批"的 HITL 闭环。

所有测试离线运行，不依赖网络与 API Key。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from marketing.activation import ActivationEngine, DormantCustomer
from marketing.cases import CaseBuilder
from marketing.compliance import ComplianceEngine
from marketing.desensitize import Desensitizer, amount_bucket
from marketing.generator import MaterialGenerator
from marketing.quality import CHANNEL_SPECS, QualityScorer
from marketing.service import MarketingService


# =========================================================================== #
# 一、脱敏（素材对外发布前的第一道硬门槛）
# =========================================================================== #
def test_desensitize_masks_phone_id_and_amount():
    d = Desensitizer()
    result = d.mask("客户电话 13812345678，身份证 320102199001011234，额度 30万")
    assert "13812345678" not in result.text
    assert "320102199001011234" not in result.text
    assert "30万" not in result.text
    assert "10-50万" in result.text          # 金额转区间，保留"额度感"
    assert result.hits == 3


def test_amount_bucket_boundaries():
    assert amount_bucket(0.5) == "万元以内"
    assert amount_bucket(5) == "10万以内"
    assert amount_bucket(30) == "10-50万"
    assert amount_bucket(60) == "50万以上"
    assert amount_bucket(120) == "百万级"


def test_desensitize_id_card_not_eaten_by_bank_card_rule():
    """18 位身份证必须命中"身份证"而不是"银行卡"（正则顺序踩过坑）。"""
    d = Desensitizer()
    result = d.mask("身份证 320102199001011234")
    kinds = [f.kind for f in result.findings]
    assert kinds == ["身份证"]                       # 只命中身份证，没被银行卡规则吃掉
    assert "3201**********1234" in result.text      # 保留前 4 位与后 4 位，中间打码


def test_mask_name_does_not_depend_on_name_dictionary():
    """已知就是姓名的字符串必须直接掩码 —— 不能靠姓名词表（词表没覆盖就漏真名）。"""
    assert Desensitizer.mask_name("张伟") == "张*"
    assert Desensitizer.mask_name("李小明") == "李**"
    assert Desensitizer.mask_name("") == ""
    # 已经是掩码形式再走一次应保持稳定（幂等）
    assert Desensitizer.mask_name("张*生") == "张**"


def test_desensitize_known_names_from_provider():
    d = Desensitizer(["张伟", "王丽娜"])
    result = d.mask("这位客户张伟和王丽娜都是这种情况")
    assert "张伟" not in result.text
    assert "王丽娜" not in result.text


# =========================================================================== #
# 二、合规审核（规则保召回 + 自动改写 + 拦截转人工）
# =========================================================================== #
@pytest.fixture
def compliance() -> ComplianceEngine:
    return ComplianceEngine(llm=None)          # 纯规则模式：确定性、可复现


@pytest.mark.parametrize(
    "text,rule_id",
    [
        ("这个客户我们包过，保证下款", "PROMISE-01"),
        ("我们零利息，免息贷款", "PROMISE-02"),
        ("黑户也能做，不看征信", "BYPASS-01"),
        ("客户电话 13812345678", "PRIVACY-01"),
    ],
)
def test_high_severity_rules_block_publishing(compliance, text, rule_id):
    """高风险项 = 一票否决：不能发布，必须转人工复核。"""
    report = compliance.check(text)
    assert not report.passed
    assert report.needs_human
    assert report.max_severity == "high"
    assert rule_id in {f.rule_id for f in report.findings}


@pytest.mark.parametrize(
    "text,rule_id,rewritten_from",
    [
        ("我们是本地最专业的机构，唯一一家", "ADLAW-01", "最专业"),
        ("额度马上到，秒批秒下款", "INDUCE-01", "秒批"),
    ],
)
def test_medium_severity_rules_are_recalled_and_rewritten(compliance, text, rule_id, rewritten_from):
    """中风险项（广告法绝对化用语/诱导性表述）要被召回，并被自动改写成合规表达。

    为什么也要求改写：这类表述在广告法下同样违规，只是不到"拦住不发"的级别；
    既然有确定的替代表达，就该顺手改掉（人不会记得去改）。
    """
    report = compliance.check(text)
    assert rule_id in {f.rule_id for f in report.findings}
    assert report.max_severity == "medium"

    fixed = compliance.check_and_fix(text)
    assert fixed.sanitized
    assert rewritten_from not in fixed.sanitized_text
    assert fixed.passed          # 中风险不拦发布


def test_compliance_passes_clean_text(compliance):
    report = compliance.check("先看负债结构，再决定怎么置换，具体以银行审批结果为准。")
    assert report.passed
    assert not report.needs_human
    assert report.findings == []


def test_compliance_does_not_mangle_ordinal_words(compliance):
    """回归测试：裸词"第一"曾被当成绝对化用语，把"第一件事"改写成"优质件事"。

    过宽匹配在内容系统里是"静默损坏"——不报错、不告警，直接把对外文案改坏。
    所以这里锁两件事：正常序数表达不判违规、不被改写；真正的排序性宣称仍要召回。
    """
    safe = "遇到这种情况，第一件事不是继续申贷；第一步先看清查询结构，第一天就把资料备齐。"
    report = compliance.check(safe)
    assert report.findings == [], f"正常序数表达被误判：{[f.matched for f in report.findings]}"
    assert report.passed
    assert compliance.sanitize(safe) == safe, "正常序数表达被改写了"

    risky = "我们是行业第一，全国销量第一，你的唯一选择。"
    findings = compliance.check(risky).findings
    assert findings, "排序性宣称必须被绝对化用语规则召回"
    fixed = compliance.sanitize(risky)
    assert fixed != risky
    assert "第一" not in fixed
    assert "唯一" not in fixed


def test_compliance_check_and_fix_rewrites_and_keeps_evidence(compliance):
    """改写成功 → 可发布，但**审核报告必须保留"拦下了什么"**（审计要求）。"""
    report = compliance.check_and_fix("黑户也能做，百分百下款，月供压力大")
    assert report.passed                      # 改写后可以发
    assert report.sanitized
    assert "黑户也能" not in report.sanitized_text
    assert "百分百下款" not in report.sanitized_text
    # 证据链不能因为是"已改写"就丢掉
    matched = {f.matched for f in report.findings}
    assert {"黑户也能", "百分百"} <= matched
    assert report.max_severity == "high"


def test_compliance_blocks_unfixable_text(compliance):
    """改不动的（敏感信息）必须拦截，且要转人工 —— 不许"改写一下就放行"。"""
    report = compliance.check_and_fix("客户电话 13812345678，已经办下来了")
    assert not report.passed
    assert report.needs_human
    assert "PRIVACY-01" in {f.rule_id for f in report.findings}


# =========================================================================== #
# 三、素材生成（渠道语感 + 生成即合规 + 质量分）
# =========================================================================== #
@pytest.fixture
def generator(settings, marketing_service) -> MaterialGenerator:
    return marketing_service.generator


@pytest.mark.parametrize("channel", ["moments", "douyin", "xiaohongshu"])
def test_generate_material_matches_channel_spec(generator, channel):
    drafts = generator.generate(channel, count=2)          # type: ignore[arg-type]
    assert len(drafts) == 2
    spec = CHANNEL_SPECS[channel]
    for d in drafts:
        assert d.channel == channel
        assert d.status in ("approved", "sanitized")
        assert d.compliance is not None and d.compliance.passed
        # 篇幅必须在渠道规范内（模板库是照着规范写的）
        assert spec.min_chars <= len(d.body) <= spec.max_chars, (
            f"{channel} 正文 {len(d.body)} 字，超出 {spec.min_chars}-{spec.max_chars}"
        )
        assert len(d.hashtags) == spec.hashtag_count
        assert d.cta.strip(), "素材必须有 CTA（没有行动指令等于白发）"


def test_generate_rejects_material_with_hard_redline(generator):
    """红线话术如果没有对应改写词，必须落到 rejected，绝不能发布。"""
    draft = generator.assemble(
        channel="moments", theme="测试", blueprint_id="bp_x", title="测试",
        body="客户电话 13812345678 已经办下来了", hashtags=[], cta="私信我",
        source="template",
    )
    assert draft.status == "rejected"
    assert "已拦截" in " ".join(draft.quality_notes)


def test_quality_score_zero_when_compliance_fails():
    scorer = QualityScorer()
    good, _ = scorer.score(
        channel="moments", title="t", body="月供压力大，网贷太多，先看负债结构。",
        hashtags=[], cta="私信我", compliance_passed=True,
    )
    bad, notes = scorer.score(
        channel="moments", title="t", body="月供压力大，网贷太多，先看负债结构。",
        hashtags=[], cta="私信我", compliance_passed=False,
    )
    assert bad < good
    assert any("不合规" in n for n in notes)


def test_quality_scorer_reports_actionable_notes():
    scorer = QualityScorer()
    _, notes = scorer.score(
        channel="douyin", title="t", body="太短了", hashtags=[], cta="",
        compliance_passed=True,
    )
    joined = " ".join(notes)
    assert "篇幅偏短" in joined
    assert "缺少行动指令" in joined
    assert "标签偏少" in joined


# =========================================================================== #
# 四、案例包装（只有成交对话可入库 + 强制脱敏）
# =========================================================================== #
def _case_builder(marketing_service) -> CaseBuilder:
    return marketing_service.case_builder


def test_case_builder_only_accepts_won_conversations(marketing_service):
    builder = _case_builder(marketing_service)
    assert builder.build_from_conversation({"outcome": "lost", "messages": []}) is None
    assert builder.build_from_conversation({"outcome": "pending", "messages": []}) is None
    assert builder.build_from_conversation({"outcome": "", "messages": []}) is None

    won = {
        "conversation_id": "T-WON", "customer_id": "CU001", "outcome": "won",
        "messages": [
            {"role": "customer", "text": "我有三笔网贷，月供压力大，手机号 13900001111"},
            {"role": "business", "text": "可以用公积金置换，具体以银行审批为准。"},
            {"role": "customer", "text": "行，那我明白了"},
        ],
    }
    case = builder.build_from_conversation(won)
    assert case is not None
    assert case.source_conversation == "T-WON"
    assert "网贷" in case.scene or "月供" in case.scene
    assert "公积金" in case.path
    # 证言里的手机号必须被脱敏
    assert "13900001111" not in case.quote
    assert case.desensitize_hits >= 1


def test_case_builder_masks_customer_name(marketing_service):
    builder = _case_builder(marketing_service)
    case = builder.build_from_conversation({
        "conversation_id": "T-WON2", "customer_id": "CU001", "outcome": "won",
        "messages": [
            {"role": "customer", "text": "我想做债务置换"},
            {"role": "business", "text": "可以做，先看负债结构。"},
            {"role": "customer", "text": "行"},
        ],
    })
    assert case is not None
    # 档案里的 name_masked 是 "张*生"，再过一次按姓名掩码 → "张**"
    assert case.customer_masked.startswith("张")
    assert "*" in case.customer_masked


def test_pack_cases_produces_publishable_material(marketing_service):
    drafts = marketing_service.pack_cases(channel="moments")
    assert drafts, "夹具里有一段成交对话，应该能包装出素材"
    for d in drafts:
        assert d.source == "case"
        assert d.compliance is not None and d.compliance.passed
        assert "真实案例" in d.title or "客户案例" in d.title


# =========================================================================== #
# 五、客户激活（对症 + 防骚扰）
# =========================================================================== #
def _engine(settings) -> ActivationEngine:
    return ActivationEngine(settings, default_channel="moments")


def _material(marketing_service, channel: str, theme: str):
    drafts = marketing_service.generate_materials(channel, theme=theme, count=1)
    assert drafts
    return drafts[0]


def test_activation_skips_customers_below_dormant_threshold(settings, marketing_service):
    engine = _engine(settings)
    material = _material(marketing_service, "moments", "债务置换")
    plan = engine.plan(
        [DormantCustomer(customer_id="C-A", dormant_days=1, intent=0.9, last_touch_days=99)],
        [material],
    )
    assert plan.generated == 0
    assert "未达沉睡阈值" in plan.skipped[0]["reason"]


def test_activation_skips_customers_in_cooldown(settings, marketing_service):
    engine = _engine(settings)
    material = _material(marketing_service, "moments", "债务置换")
    plan = engine.plan(
        [DormantCustomer(customer_id="C-B", dormant_days=30, intent=0.9, last_touch_days=0)],
        [material],
    )
    assert plan.generated == 0
    assert "静默期" in plan.skipped[0]["reason"]


def test_activation_respects_per_customer_touch_cap(settings, marketing_service):
    """触达上限用的是**真实触达流水**，不是客户档案里的离线快照。"""
    engine = _engine(settings)
    material = _material(marketing_service, "moments", "债务置换")
    plan = engine.plan(
        [DormantCustomer(customer_id="C-C", dormant_days=40, intent=0.9, last_touch_days=30)],
        [material],
        activated_counts={"C-C": settings.marketing_max_activation_per_customer},
    )
    assert plan.generated == 0
    assert "已达触达上限" in plan.skipped[0]["reason"]


def test_activation_matches_material_by_objection(settings, marketing_service):
    """犹豫点是"利率贵" → 必须给"费用透明"主题的素材，而不是随便给一条。"""
    engine = _engine(settings)
    fee = _material(marketing_service, "moments", "费用透明")
    swap = _material(marketing_service, "moments", "债务置换")
    plan = engine.plan(
        [DormantCustomer(
            customer_id="C-D", dormant_days=20, intent=0.8, last_touch_days=30,
            objection="利率太高了，服务费也贵", concern="费用",
        )],
        [swap, fee],
    )
    assert plan.generated == 1
    task = plan.tasks[0]
    assert fee.title == task.material_title
    assert "费用透明" in task.reason


def test_activation_task_channel_follows_the_material(settings, marketing_service):
    """找不到"同渠道+同主题"时以对症优先，任务渠道必须跟随素材实际渠道。"""
    engine = _engine(settings)
    xhs_case = _material(marketing_service, "xiaohongshu", "案例见证")
    plan = engine.plan(
        [DormantCustomer(
            customer_id="C-E", dormant_days=25, intent=0.6, last_touch_days=30,
            objection="一直在考虑，想再看看", concern="利率",
        )],
        [xhs_case],
    )
    assert plan.generated == 1
    assert plan.tasks[0].channel == "xiaohongshu"     # 跟着素材走，不是默认朋友圈
    assert "小红书" in plan.tasks[0].reason


def test_activation_priority_ranks_high_intent_long_dormant_first(settings, marketing_service):
    engine = _engine(settings)
    material = _material(marketing_service, "moments", "案例见证")
    plan = engine.plan(
        [
            DormantCustomer(customer_id="C-LOW", dormant_days=10, intent=0.2, last_touch_days=30),
            DormantCustomer(customer_id="C-HIGH", dormant_days=45, intent=0.9, last_touch_days=30),
        ],
        [material],
    )
    assert [t.customer_id for t in plan.tasks] == ["C-HIGH", "C-LOW"]


def test_activation_suggested_time_avoids_night_and_weekend(settings, marketing_service):
    engine = _engine(settings)
    material = _material(marketing_service, "moments", "债务置换")
    plan = engine.plan(
        [DormantCustomer(customer_id="C-F", dormant_days=20, intent=0.7, last_touch_days=30)],
        [material],
        now=datetime(2026, 9, 25, 23, 30),      # 周五深夜
    )
    send_at = datetime.strptime(plan.tasks[0].suggested_send_at, "%Y-%m-%d %H:%M")
    assert send_at.weekday() < 5               # 不能落在周末
    assert send_at.hour in (10, 19)            # 只发两个黄金时段
    assert send_at > datetime(2026, 9, 25, 23, 30)


def test_activation_empty_when_no_publishable_material(settings):
    engine = _engine(settings)
    plan = engine.plan(
        [DormantCustomer(customer_id="C-G", dormant_days=30, intent=0.9, last_touch_days=30)],
        [],
    )
    assert plan.generated == 0
    assert any("无可用素材" in str(s.get("reason")) for s in plan.skipped)


# =========================================================================== #
# 六、效果归因（结论必须由数据推导）
# =========================================================================== #
def test_attribution_without_data_says_so(marketing_service):
    report = marketing_service.attribution.report([], [])
    assert report.total_touches == 0
    assert report.conclusions == ["暂无触达数据，无法归因"]


def test_attribution_funnel_and_conclusions(marketing_service):
    from marketing.domain import TouchRecord

    materials = [
        {"material_id": "mat_a", "title": "A 素材", "channel": "moments", "theme": "债务置换"},
        {"material_id": "mat_b", "title": "B 素材", "channel": "douyin", "theme": "征信解读"},
    ]
    touches: list[TouchRecord] = []
    for stage, n in (("impression", 100), ("consult", 20), ("lead", 8), ("deal", 2)):
        touches += [
            TouchRecord(touch_id=f"a_{stage}_{i}", material_id="mat_a",
                        channel="moments", stage=stage)          # type: ignore[arg-type]
            for i in range(n)
        ]
    for stage, n in (("impression", 500), ("consult", 30), ("lead", 0), ("deal", 0)):
        touches += [
            TouchRecord(touch_id=f"b_{stage}_{i}", material_id="mat_b",
                        channel="douyin", stage=stage)           # type: ignore[arg-type]
            for i in range(n)
        ]

    report = marketing_service.attribution.report(materials, touches)
    assert report.total_materials == 2
    assert report.total_touches == 660
    assert report.by_channel["moments"]["lead"] == 8.0
    assert report.lead_share["moments"] == 1.0
    assert report.conversion_rate == pytest.approx(0.25)
    joined = " ".join(report.conclusions)
    assert "朋友圈带来的留资最多" in joined
    assert "表现最好的素材是「A 素材」" in joined
    # 抖音触达量大但没有留资 → 必须给"调整钩子或减少投入"的建议
    assert "未产生留资" in joined


def test_attribution_labels_missing_material_instead_of_blank(marketing_service):
    """素材被清理过时，报告不能出现空标题的行（那样没法行动）。"""
    from marketing.domain import TouchRecord

    report = marketing_service.attribution.report([], [
        TouchRecord(touch_id="t1", material_id="mat_gone", channel="moments",
                    stage="lead"),  # type: ignore[arg-type]
    ])
    assert report.top_materials[0]["title"] == "(素材档案缺失)"
    assert report.top_materials[0]["channel"] == "moments"


# =========================================================================== #
# 七、服务门面
# =========================================================================== #
def test_service_bootstrap_loads_everything(marketing_service):
    stats = marketing_service.stats()
    assert stats["blueprints"] == 4
    assert stats["dormant_customers"] == 2
    assert stats["cases"] == 1                        # 夹具里只有 TCONV-1 是 won
    assert stats["materials"] >= 4                    # 冷启动素材包
    assert stats["seed_performance"] == 2
    assert stats["channels"] == ["douyin", "moments", "xiaohongshu"]


def test_service_generate_persists_and_audits(marketing_service):
    before = marketing_service.store.material_count()
    drafts = marketing_service.generate_materials("douyin", count=2)
    assert len(drafts) == 2
    assert marketing_service.store.material_count() == before + 2
    assert all(d.material_id for d in drafts)          # 入库后必须回写 material_id
    # 合规流水要能查到（监管审计的入口）
    audits = marketing_service.store.audits(limit=100)
    assert audits
    assert marketing_service.stats()["audit"]["total"] > 0


def test_service_plan_activation_uses_store_touch_counts(marketing_service):
    plan = marketing_service.plan_activation(limit=10)
    assert plan.generated == 1
    assert plan.tasks[0].customer_id == "CU002"
    # 计划落库，业务员端能按优先级取待办
    tasks = marketing_service.store.list_activation_tasks(limit=10)
    assert len(tasks) == 1
    assert tasks[0].customer_id == "CU002"


def test_service_activate_customer_returns_sendable_package(marketing_service):
    marketing_service.plan_activation(limit=10)
    registry_tools = marketing_service.store.list_activation_tasks(limit=10)
    assert registry_tools

    # 通过工具调用取触达包（与 Agent 走同一条路径）
    from agent_platform.tools.registry import ToolRegistry

    registry = ToolRegistry()
    marketing_service.register_tools(registry)
    result = registry.get("marketing.activate_customer").handler(customer_id="CU002")
    assert result["found"] is True
    assert result["message_body"], "触达包必须带可直接发送的正文"
    assert "*" in result["name_masked"]
    # 发给客户的内容里不能带内部标注（状态/质量分）
    assert "状态 approved" not in result["message_body"]


def test_service_attribute_uses_seed_performance_when_no_live_touches(marketing_service):
    report = marketing_service.attribute()
    assert report.total_touches > 0            # 种子漏斗生效（演示/冷启动）
    assert "暂无触达数据" not in report.conclusions


def test_service_audit_records_manual_review(marketing_service):
    report = marketing_service.audit("这个是百分百下款的", ref_id="manual-1")
    assert report.findings
    rows = marketing_service.store.audits(ref_id="manual-1")
    assert len(rows) == 1
    assert rows[0]["stage"] == "manual_audit"


# =========================================================================== #
# 八、工具注册 + Agent 行为
# =========================================================================== #
def test_register_tools_exposes_six_marketing_tools(marketing_service):
    from agent_platform.tools.registry import ToolRegistry

    registry = ToolRegistry()
    names = marketing_service.register_tools(registry)
    assert names == [
        "marketing.generate_materials",
        "marketing.pack_cases",
        "marketing.plan_activation",
        "marketing.audit_text",
        "marketing.attribute",
        "marketing.activate_customer",
    ]
    # 每个工具都要有中文描述（Agent 靠它选择工具，人靠它理解边界）
    for name in names:
        assert len(registry.get(name).description) > 20


@pytest.fixture
def marketing_runtime(settings, checkpointer, marketing_service):
    from agent_platform.runtime import AgentRuntime
    from marketing.agent import build_marketing_agent

    runtime = AgentRuntime(settings=settings, checkpointer=checkpointer)
    marketing_service.register_tools(runtime.tools)
    runtime.register(build_marketing_agent(marketing_service, runtime.tools))
    return runtime


@pytest.fixture
def marketing_agent(settings, marketing_service):
    from agent_platform.runtime import AgentRuntime
    from marketing.agent import build_marketing_agent

    runtime = AgentRuntime(settings=settings)
    marketing_service.register_tools(runtime.tools)
    caps = build_marketing_agent(marketing_service, runtime.tools)
    return caps


@pytest.mark.parametrize(
    "text,expected",
    [
        ("帮我生成 2 条朋友圈素材", "generate"),
        ("把成交对话包装成客户证言素材", "case"),
        ("有哪些沉睡客户需要激活", "activation"),
        ("帮我审一下这段：我们是本地最专业的机构", "audit"),
        ("看看哪条素材带来的留资最多", "attribution"),
    ],
)
def test_agent_route_by_intent(marketing_agent, text, expected):
    assert marketing_agent.route({"user_input": text}) == expected


@pytest.mark.parametrize(
    "route_name,expected_tools",
    [
        ("generate", ["marketing.generate_materials"]),
        ("case", ["marketing.pack_cases"]),
        ("activation", ["marketing.activate_customer", "marketing.plan_activation"]),
        ("audit", ["marketing.audit_text"]),
        ("attribution", ["marketing.attribute"]),
    ],
)
def test_agent_scopes_tools_by_route(marketing_agent, route_name, expected_tools):
    scoped = {t.name for t in marketing_agent.resolve_scoped_tools({"route": route_name})}
    for name in expected_tools:
        assert name in scoped
    # 逃生舱永远在（转人工不能因为裁剪而不可用）
    assert "platform.handoff_to_human" in scoped
    # 作用域必须真的更窄：绝不能是全量
    assert len(scoped) < len(marketing_agent.tools.all())


def test_agent_hides_send_tool_unless_user_asks_to_send(marketing_agent):
    """最小权限的铁证：不提"发出去"，Agent 手里根本没有对外发送工具。"""
    quiet = {t.name for t in marketing_agent.resolve_scoped_tools(
        {"route": "activation", "user_input": "有哪些沉睡客户需要激活"}
    )}
    assert "platform.send_external_message" not in quiet

    loud = {t.name for t in marketing_agent.resolve_scoped_tools(
        {"route": "activation", "user_input": "把激活素材发给客户 CU002"}
    )}
    assert "platform.send_external_message" in loud


def test_agent_generate_route_persists_materials(marketing_runtime, marketing_service):
    before = marketing_service.store.material_count()
    result = marketing_runtime.invoke("marketing", "帮我生成 2 条抖音素材")
    assert result.status == "completed"
    assert result.route == "generate"
    assert result.output["route"] == "generate"
    assert result.output["count"] == 2
    assert result.output["rejected"] == 0
    assert marketing_service.store.material_count() == before + 2
    # 作用域裁剪真的省了 token
    assert result.tokens_used < 50_000


def test_agent_audit_route_handoffs_when_high_risk_cannot_be_fixed(
    marketing_runtime, marketing_service
):
    """改不动的红线 → 必须转人工，而不是"改写一下就放行"。"""
    result = marketing_runtime.invoke(
        "marketing", "帮我审一下这段：客户电话 13812345678 已经办下来了"
    )
    assert result.route == "audit"
    report = result.output["report"]
    assert report["passed"] is False
    assert report["needs_human"] is True
    assert result.output["handoff"], "高风险文本必须发起转人工"
    # 转人工是 medium 风险工具，不挂 HITL，直接执行
    assert result.status == "completed"


def test_agent_audit_route_rewrites_fixable_text(marketing_runtime):
    result = marketing_runtime.invoke("marketing", "帮我审一下这段：我们是本地最专业的机构")
    report = result.output["report"]
    assert report["sanitized"] is True
    assert "最专业" not in report["sanitized_text"]
    assert "专业" in report["sanitized_text"]


def test_agent_activation_send_interrupts_for_approval(marketing_runtime, marketing_service):
    """发布是对外不可撤回的动作 → 必须挂起等人工审批。"""
    result = marketing_runtime.invoke(
        "marketing", "把激活素材发给客户 CU002", context={"customer_id": "CU002"}
    )
    assert result.status == "interrupted"
    assert result.interrupts
    interrupt = result.interrupts[0]["value"]
    assert interrupt["type"] == "approval_required"
    assert interrupt["tool"] == "platform.send_external_message"
    assert interrupt["risk_level"] == "high"
    # 待审内容必须是真实素材正文，不能是空消息
    assert len(interrupt["arguments"]["content"]) > 20
    assert interrupt["arguments"]["target"] == "CU002"


def test_agent_activation_send_succeeds_after_approval(marketing_runtime):
    first = marketing_runtime.invoke(
        "marketing", "把激活素材发给客户 CU002", context={"customer_id": "CU002"}
    )
    assert first.status == "interrupted"

    resumed = marketing_runtime.resume(
        first.thread_id, {"approved": True, "decided_by": "张主管", "comment": "素材已核对"}
    )
    assert resumed.status == "completed"
    assert resumed.output["sent"] is True
    assert resumed.approvals[0]["decided_by"] == "张主管"


def test_agent_activation_send_aborts_when_rejected(marketing_runtime):
    first = marketing_runtime.invoke(
        "marketing", "把激活素材发给客户 CU002", context={"customer_id": "CU002"}
    )
    assert first.status == "interrupted"
    resumed = marketing_runtime.resume(
        first.thread_id, {"approved": False, "decided_by": "张主管", "comment": "话术需再改"}
    )
    assert resumed.output["sent"] is False


def test_agent_activation_without_materials_is_safe(marketing_service, settings):
    """素材库空时不允许编造素材发出去，只给出计划与跳过原因。"""
    from agent_platform.runtime import AgentRuntime
    from marketing.agent import build_marketing_agent

    marketing_service.dormant_customers = []
    runtime = AgentRuntime(settings=settings)
    marketing_service.register_tools(runtime.tools)
    runtime.register(build_marketing_agent(marketing_service, runtime.tools))

    result = runtime.invoke("marketing", "有哪些客户需要激活")
    assert result.status == "completed"
    assert result.output["generated"] == 0
    assert result.output["sent"] is False


def test_service_close_is_idempotent(settings):
    svc = MarketingService(settings=settings)
    svc.close()
