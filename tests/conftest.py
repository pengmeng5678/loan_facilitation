"""pytest 公共夹具。

设计原则：**测试必须自包含**。所有测试数据在 tmp_path 里现场生成，
不依赖仓库里的 .data 目录，也不依赖网络与任何 API Key —— 任何人 clone 下来
执行 `pytest` 都应立即通过。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agent_platform.config import Settings

# =========================================================================== #
# 夹具数据（三款产品，覆盖：正常匹配 / 图谱替代 / 政策过期）
# =========================================================================== #
PRODUCTS = [
    {
        "code": "BANK-T-GJJ-001",
        "name": "测试公积金信用贷",
        "bank": "测试银行T",
        "category": "公积金贷",
        "purposes": ["债务置换", "资金周转"],
        "age_min": 22, "age_max": 58,
        "income_min": 4000,
        "qualifications": ["公积金"],
        "occupations": ["受薪"],
        "credit_query_6m_max": 8, "overdue_count_max": 2, "overdue_days_max": 30,
        "debt_ratio_max": 0.65,
        "amount_min": 50000, "amount_max": 400000,
        "rate_min": 3.20, "rate_max": 4.60,
        "term_months": [12, 24, 36],
        "quota_multiple": 40, "quota_base": "公积金基数",
        "materials": ["身份证", "公积金缴存明细", "征信报告"],
        "process": ["提交资料", "银行预审", "面签", "放款"],
        "effective_date": "2024-01-01", "expire_date": None,
        "substitutes": ["BANK-T-DBT-001"],
    },
    {
        "code": "BANK-T-DBT-001",
        "name": "测试债务置换贷",
        "bank": "测试银行T",
        "category": "信用贷",
        "purposes": ["债务置换", "负债优化"],
        "age_min": 24, "age_max": 58,
        "income_min": 6000,
        "qualifications": ["公积金", "社保"],
        "occupations": ["受薪", "自雇"],
        "credit_query_6m_max": 12, "overdue_count_max": 4, "overdue_days_max": 60,
        "debt_ratio_max": 0.80,
        "amount_min": 80000, "amount_max": 600000,
        "rate_min": 4.35, "rate_max": 7.20,
        "term_months": [12, 24, 36],
        "quota_multiple": 30, "quota_base": "月收入",
        "materials": ["身份证", "征信报告", "存量贷款清单"],
        "process": ["负债梳理", "提交资料", "置换放款"],
        "effective_date": "2024-09-01", "expire_date": None,
        "substitutes": ["BANK-T-GJJ-001"],
    },
    {
        # 已过期产品：用于验证"生效日期过滤"（助贷最要命的坑）
        "code": "BANK-T-OLD-001",
        "name": "测试已过期产品",
        "bank": "测试银行T",
        "category": "信用贷",
        "purposes": ["资金周转"],
        "age_min": 22, "age_max": 60,
        "income_min": 3000,
        "qualifications": ["社保"],
        "occupations": [],
        "credit_query_6m_max": 20, "overdue_count_max": 6, "overdue_days_max": 90,
        "debt_ratio_max": 1.0,
        "amount_min": 10000, "amount_max": 100000,
        "rate_min": 1.00, "rate_max": 2.00,   # 故意给一个"诱人但过期"的利率
        "term_months": [12],
        "quota_multiple": 0, "quota_base": "",
        "materials": ["身份证"],
        "process": ["线上申请"],
        "effective_date": "2023-01-01", "expire_date": "2024-01-01",
        "substitutes": [],
    },
]

CUSTOMERS = [
    {
        "customer_id": "CU001", "name_masked": "张*生", "age": 34, "city": "南京",
        "occupation_type": "受薪", "monthly_income": 18000,
        "qualifications": ["公积金", "社保"],
        "credit_query_6m": 3, "overdue_count_12m": 0, "overdue_days_max": 0,
        "monthly_debt": 6000, "loans_count": 2,
        "needs": ["债务置换"], "desired_amount": 300000, "note": "",
    },
    {
        "customer_id": "CU002", "name_masked": "李*女", "age": 26, "city": "南京",
        "occupation_type": "受薪", "monthly_income": 3000,
        "qualifications": [],
        "credit_query_6m": 15, "overdue_count_12m": 3, "overdue_days_max": 40,
        "monthly_debt": 2800, "loans_count": 5,
        "needs": ["资金周转"], "desired_amount": 80000, "note": "资质薄弱，应转人工",
    },
]

PERSONAS = [
    {
        "persona_id": "P-T-EASY", "name": "测试客户（配合型）", "difficulty": "easy",
        "scene": "首触", "profile": "34岁受薪，有公积金",
        "resistance": ["回答简短"], "common_objections": ["我再考虑考虑"],
        "customer_id": "CU001",
    },
    {
        "persona_id": "P-T-HARD", "name": "测试客户（比价型）", "difficulty": "hard",
        "scene": "异议处理", "profile": "26岁，资质弱，已问过别家",
        "resistance": ["反复压价", "拿别家对比"],
        "common_objections": ["别家利率更低", "你们是不是骗人的"],
        "customer_id": "CU002",
    },
]

CONVERSATIONS = [
    {
        "conversation_id": "TCONV-1", "customer_id": "CU001", "agent": "业务员甲",
        "outcome": "won", "channel": "wecom", "started_at": "2026-08-01T10:00:00",
        "messages": [
            {"role": "business", "text": "张先生您好，我是助贷顾问。我先确认一下您的资质：公积金缴存多久了？近半年征信查询几次？", "ts": 0},
            {"role": "customer", "text": "公积金交了3年，查询3次吧", "ts": 30},
            {"role": "business", "text": "好的。预估利率区间是3.2%到4.6%，以银行审批为准，我不做任何放款承诺。", "ts": 60},
            {"role": "customer", "text": "行，那我明白了", "ts": 90},
            {"role": "business", "text": "那您周六上午10点方便过来一趟吗？我当面给您算额度。", "ts": 120},
            {"role": "customer", "text": "好，我去", "ts": 150},
        ],
    },
    {
        "conversation_id": "TCONV-2", "customer_id": "CU002", "agent": "业务员乙",
        "outcome": "lost", "channel": "wecom", "started_at": "2026-08-02T11:00:00",
        "messages": [
            {"role": "business", "text": "您好，您这个情况我们包过，不看征信的，保证下款。", "ts": 0},
            {"role": "customer", "text": "真的假的？", "ts": 20},
            {"role": "business", "text": "当然是真的，联系我 13812345678", "ts": 40},
        ],
    },
]

# =========================================================================== #
# 项目七夹具：素材模板 / 沉睡客户 / 效果种子
# =========================================================================== #
MARKETING_BLUEPRINTS = [
    {
        "blueprint_id": "bp_t_mom_swap", "name": "测试置换·朋友圈", "channel": "moments",
        "theme": "债务置换",
        "hook": "月供压力大，问题出在哪？",
        "pain": "网贷和信用卡分期叠在一起，每月还款日像打仗。",
        "solution": "先把负债按利率排序，再看哪些能置换成低息长期产品。",
        "cta": "想看能不能置换，私信我帮你看一眼负债结构。",
        "tags": ["债务置换"],
    },
    {
        "blueprint_id": "bp_t_mom_fee", "name": "测试费用·朋友圈", "channel": "moments",
        "theme": "费用透明",
        "hook": "担心服务费白花？说点实在的。",
        "pain": "怕前期收钱，怕办不下来钱退不回。",
        "solution": "费用口径签约前写清楚，办不下来不收，办成了才结算。",
        "cta": "想弄清费用怎么算，私信我。",
        "tags": ["费用透明"],
    },
    {
        "blueprint_id": "bp_t_dy_credit", "name": "测试征信·抖音口播", "channel": "douyin",
        "theme": "征信解读",
        "hook": "查了十几次征信，为什么反而办不下来？",
        "pain": "硬查询次数是银行直接看的指标，短时间密集申请会被判定为资金紧张。",
        "solution": "遇到这种情况，第一件事不是继续申贷，而是停下来：看清查询结构、算清可选方向，该等的就等，别把征信再查花了。",
        "cta": "不确定自己征信能不能做的，点头像问我，先判断方向再动手。",
        "tags": ["征信知识", "贷款攻略"],
    },
    {
        "blueprint_id": "bp_t_x_case", "name": "测试案例·小红书", "channel": "xiaohongshu",
        "theme": "案例见证",
        "hook": "被拒过两次的人，后来是怎么办下来的？",
        "pain": "他一开始的情况挺糟：查询次数多、有过逾期，自己试了两家都被拒，信心基本没了，甚至觉得这辈子都办不了了。",
        "solution": "我们做的第一件事不是再递材料，而是把资质重新摊开看：名下有什么、能走哪条路。方向对了，短板就不再是硬伤，所以别急着否定自己，先让专业的人看一眼再下结论。",
        "cta": "有被拒经历的姐妹评论区聊聊，我帮你判断方向。",
        "tags": ["案例见证", "征信知识", "避坑指南"],
    },
]

MARKETING_DORMANT_CUSTOMERS = [
    {   # 应该被激活（沉睡够久、未在静默期）
        "customer_id": "CU002", "name_masked": "李*女", "dormant_days": 30,
        "objection": "担心办不下来", "concern": "征信", "intent": 0.8, "last_touch_days": 30,
    },
    {   # 未达沉睡阈值 → 不打扰
        "customer_id": "CU001", "name_masked": "张*生", "dormant_days": 2,
        "objection": "再说吧", "concern": "征信", "intent": 0.5, "last_touch_days": 30,
    },
]

MARKETING_PERFORMANCE = [
    {"channel": "moments", "theme": "债务置换",
     "funnel": {"impression": 100, "consult": 20, "lead": 8, "deal": 2}, "ts": "2026-09-20 20:30"},
    {"channel": "moments", "theme": "费用透明",
     "funnel": {"impression": 80, "consult": 10, "lead": 3, "deal": 1}, "ts": "2026-09-21 19:30"},
]


def write_fixture_data(base: Path) -> tuple[Path, Path]:
    """把夹具数据写入 base 目录，返回 (policy_dir, profile_dir)。"""
    policy_dir = base / "policies"
    profile_dir = base / "profiles"
    marketing_dir = base / "marketing"
    policy_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)
    marketing_dir.mkdir(parents=True, exist_ok=True)

    (policy_dir / "products.json").write_text(
        json.dumps(PRODUCTS, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 每个产品生成 3 条政策条款，格式与 seed_data.py 一致
    rows: list[dict] = []
    for p in PRODUCTS:
        base_meta = {
            "product_code": p["code"],
            "effective_date": p["effective_date"],
            "expire_date": p["expire_date"],
            "source_file": f"policies/{p['code']}.pdf",
        }
        rows.append({
            **base_meta, "policy_id": f"{p['code']}-RATE-01", "clause_no": "2.1",
            "title": f"{p['name']} 额度与利率",
            "text": (
                f"{p['name']}（{p['bank']}，产品编号 {p['code']}）授信额度区间为 "
                f"{p['amount_min']:,.0f} 元至 {p['amount_max']:,.0f} 元；"
                f"年化利率区间为 {p['rate_min']:.2f}% 至 {p['rate_max']:.2f}%。"
                f"最终额度与利率以银行审批结果为准。"
            ),
        })
        rows.append({
            **base_meta, "policy_id": f"{p['code']}-ELIG-01", "clause_no": "3.1",
            "title": f"{p['name']} 准入条件",
            "text": (
                f"申请 {p['name']} 需满足：年龄 {p['age_min']} 至 {p['age_max']} 周岁；"
                f"月收入不低于 {p['income_min']:,.0f} 元；"
                f"具备任一资质：{'、'.join(p['qualifications'])}；"
                f"近 6 个月征信查询不超过 {p['credit_query_6m_max']} 次；"
                f"近 12 个月逾期不超过 {p['overdue_count_max']} 次；"
                f"负债收入比不超过 {p['debt_ratio_max']:.2f}。"
            ),
        })
        rows.append({
            **base_meta, "policy_id": f"{p['code']}-DOC-01", "clause_no": "4.2",
            "title": f"{p['name']} 所需资料",
            "text": f"办理 {p['name']} 需提供：{'、'.join(p['materials'])}。",
        })
        # 办理流程条款必须与 scripts/seed_data.py 保持一致：
        # 语音评测集里的 voice_flow 用例就是打这条，缺了它会整条失真
        rows.append({
            **base_meta, "policy_id": f"{p['code']}-FLOW-01", "clause_no": "5.1",
            "title": f"{p['name']} 办理流程",
            "text": (
                f"{p['name']} 办理流程：{' → '.join(p['process'])}。"
                f"资料齐全后一般 1 至 3 个工作日出审批结果，具体时效以银行审批为准。"
            ),
        })

    (policy_dir / "policies.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    (policy_dir / "conversations.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in CONVERSATIONS) + "\n", encoding="utf-8"
    )
    (profile_dir / "customers.json").write_text(
        json.dumps(CUSTOMERS, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (profile_dir / "personas.json").write_text(
        json.dumps(PERSONAS, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ---- 项目七夹具 ----
    (marketing_dir / "blueprints.json").write_text(
        json.dumps(MARKETING_BLUEPRINTS, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (marketing_dir / "dormant_customers.json").write_text(
        json.dumps(MARKETING_DORMANT_CUSTOMERS, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (marketing_dir / "material_performance.json").write_text(
        json.dumps(MARKETING_PERFORMANCE, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return policy_dir, profile_dir


# =========================================================================== #
# 夹具
# =========================================================================== #
@pytest.fixture(scope="session")
def fixture_dirs(tmp_path_factory) -> tuple[Path, Path]:
    """会话级夹具数据目录（生成一次，全部测试复用）。"""
    return write_fixture_data(tmp_path_factory.mktemp("zhudai_fixtures"))


@pytest.fixture
def settings(fixture_dirs, tmp_path) -> Settings:
    """指向夹具数据的 Settings（离线 LLM + 内存 Checkpointer）。"""
    policy_dir, profile_dir = fixture_dirs
    base = policy_dir.parent
    return Settings(
        app_env="local",
        log_level="WARNING",
        llm_provider="echo",
        embedding_provider="local",
        embedding_dim=256,                # 测试用小维度，跑得快
        checkpointer_backend="memory",
        vector_backend="local",
        policy_data_dir=str(policy_dir),
        profile_data_dir=str(profile_dir),
        # 项目七 / 项目六 / 项目一的数据一律落在 tmp_path：
        # 测试绝对不能写进仓库的 .data 目录（否则会污染演示数据、且测试之间互相干扰）
        marketing_data_dir=str(base / "marketing"),
        gateway_sqlite_path=str(tmp_path / "gateway.sqlite"),
        voice_data_dir=str(tmp_path / "voice"),
        voice_sqlite_path=str(tmp_path / "voice" / "voice.sqlite"),
        # 项目八的流程库同样落 tmp_path（理由同 voice）
        flow_data_dir=str(tmp_path / "flow"),
        flow_sqlite_path=str(tmp_path / "flow" / "flow.sqlite"),
        # 项目四数据中台：库/源/导出全部落 tmp_path；
        # data_auto_ingest=False —— 测试要显式调用 ingest，不能让装配阶段偷偷跑批
        data_platform_data_dir=str(tmp_path / "data_platform"),
        data_platform_sqlite_path=str(tmp_path / "data_platform" / "data.sqlite"),
        data_source_path=str(tmp_path / "data_platform" / "wechat_source.sqlite"),
        data_export_dir=str(tmp_path / "data_platform" / "exports"),
        data_auto_ingest=False,
        sqlite_business_path=str(tmp_path / "business.sqlite"),
        checkpointer_sqlite_path=str(tmp_path / "cp.sqlite"),
        approvals_sqlite_path=str(tmp_path / "platform" / "approvals.sqlite"),
        max_agent_steps=6,
        max_agent_tokens=50_000,
        approval_timeout_seconds=60,
    )


@pytest.fixture
def checkpointer():
    return InMemorySaver()


@pytest.fixture
def product_service(settings):
    """已 bootstrap 的产品知识助手服务。"""
    from product_assistant.service import ProductAssistantService

    svc = ProductAssistantService(settings=settings)
    svc.bootstrap()
    return svc


@pytest.fixture
def coaching_service(settings, product_service):
    """已 bootstrap 的陪练服务（客户档案来自产品服务的仓储）。"""
    from coaching.service import CoachingService

    svc = CoachingService(
        settings=settings, profile_provider=product_service.profiles.get
    )
    svc.bootstrap()
    yield svc
    svc.close()


@pytest.fixture
def marketing_service(settings, product_service, coaching_service):
    """已 bootstrap 的素材推广服务（依赖注入客户档案与对话语料）。"""
    from marketing.service import MarketingService

    svc = MarketingService(
        settings=settings,
        name_resolver=lambda cid: (
            getattr(product_service.profiles.get(cid), "name_masked", "") or ""
        ),
        conversation_provider=lambda: coaching_service.conversations,
    )
    svc.bootstrap()
    yield svc
    svc.close()


@pytest.fixture
def gateway_service(settings):
    """已装配的网关服务（评测目标注入最小业务闭环）。"""
    from gateway.service import GatewayService

    svc = GatewayService(settings=settings)
    yield svc
    svc.close()


@pytest.fixture
def voice_service(settings, product_service):
    """已 bootstrap 的语音客服服务。

    检索能力直接复用项目二的混合检索器 —— 这正是生产装配方式：
    语音层不重复建索引，只是把同一个知识库的召回结果压缩成"能念出来的一段话"。
    """
    from voice_service.service import VoiceService

    settings.ensure_dirs()
    svc = VoiceService(
        settings=settings,
        llm=product_service.llm,
        retrieve_fn=product_service.retriever.retrieve,
    )
    svc.bootstrap()
    yield svc
    svc.close()


@pytest.fixture
def flow_ops_service(settings, product_service):
    """已 bootstrap 的流程数字员工服务。

    plan_provider / profile_provider 直接转调项目二 —— 与生产装配
    （app_context.py）完全一致，保证测试验的就是上线路径。
    """
    from app_context import _make_plan_provider, _make_profile_provider
    from flow_ops.service import FlowOpsService

    settings.ensure_dirs()
    svc = FlowOpsService(
        settings=settings,
        llm=product_service.llm,
        plan_provider=_make_plan_provider(product_service),
        profile_provider=_make_profile_provider(product_service),
    )
    svc.bootstrap()
    yield svc
    svc.close()


@pytest.fixture
def data_platform_service(settings):
    """已 bootstrap 的数据中台服务（源库由用例自己生成）。"""
    from data_platform.service import DataPlatformService

    svc = DataPlatformService(settings=settings)
    svc.bootstrap()
    yield svc
    svc.close()


@pytest.fixture
def app_context_fixture(settings, checkpointer, monkeypatch):
    """完整的应用上下文，并把 app_context 单例替换掉（供 API 测试用）。"""
    import app_context as ac

    ctx = ac.build_app_context(settings=settings, checkpointer=checkpointer)
    monkeypatch.setattr(ac, "get_app_context", lambda: ctx)
    return ctx
