"""项目二（产品知识助手与方案匹配）测试。

覆盖：父子块切分、混合检索与引用溯源、拒答闸门、政策时效过滤、
结构化准入筛选与排序、无匹配转人工、方案书生成、以及 Agent 三条路由。
"""

from __future__ import annotations

from datetime import date

import pytest

from product_assistant.domain.matching import MatchingEngine
from product_assistant.domain.models import CustomerProfile
from product_assistant.rag.chunking import PolicyDocument, chunk_document
from product_assistant.rag.tokenize import tokenize


# =========================================================================== #
# 一、切分与分词
# =========================================================================== #
def test_chunk_document_produces_child_chunks_sharing_one_parent():
    doc = PolicyDocument(
        policy_id="P1", text="公" * 300, product_code="X", clause_no="1.1", title="标题"
    )
    chunks = chunk_document(doc)

    assert len(chunks) > 1
    assert all(c.parent_text == doc.text for c in chunks)   # 父块一致
    assert all(len(c.text) <= 120 for c in chunks)          # 子块受控
    assert len({c.chunk_id for c in chunks}) == len(chunks)  # 子块 ID 唯一
    # 引用信息随子块携带（用于溯源）
    assert chunks[0].citation.policy_id == "P1"
    assert chunks[0].citation.clause_no == "1.1"


def test_chunk_searchable_text_includes_product_code():
    """产品编号必须进入检索文本，否则 BM25 无法精确命中。"""
    doc = PolicyDocument(policy_id="P1", text="利率区间说明", product_code="BANK-X-001", title="额度")
    chunk = chunk_document(doc)[0]
    assert "BANK-X-001" in chunk.searchable_text


def test_tokenize_handles_chinese_and_ascii():
    tokens = tokenize("公积金贷 3.2% BANK-A-001")
    assert "公积" in tokens            # 中文产出 2-gram
    assert "公积金" in tokens          # 以及 3-gram
    assert "bank" in tokens            # 英文按词切
    assert "001" in tokens             # 编号里的数字保留


def test_content_tokens_drops_single_chinese_chars():
    """相关性判定必须用 content_tokens：单字噪声会让无关问题也拿到高分。"""
    from product_assistant.rag.tokenize import content_tokens

    assert "的" not in content_tokens("我的公积金")
    assert "公积" in content_tokens("我的公积金")


# =========================================================================== #
# 二、检索与拒答
# =========================================================================== #
def test_retrieval_returns_citations(product_service):
    answer = product_service.answer_question("测试公积金信用贷的利率是多少？")
    assert answer.refused is False
    assert len(answer.citations) >= 1
    assert any("BANK-T-GJJ-001" in c for c in answer.citations)


def test_retrieval_refuses_when_no_policy_support(product_service):
    """无政策依据必须拒答 —— 助贷合规底线。"""
    answer = product_service.answer_question("请预测一下明天的股票走势，推荐一只基金")
    assert answer.refused is True
    assert answer.citations == []
    assert "未检索到" in answer.answer


def test_retrieval_hit_scores_are_exposed_for_debugging(product_service):
    answer = product_service.answer_question("测试公积金信用贷准入条件")
    assert answer.hits
    top = answer.hits[0]
    # 三路分数都要可见，便于排障（BM25 / dense / rerank）
    assert {"bm25", "dense", "rerank", "score"} <= set(top)


def test_expired_policy_is_not_retrievable(product_service):
    """过期政策绝不能出现在检索结果里（用旧利率误导客户 = 客诉 + 合规风险）。"""
    hits = product_service.retriever.retrieve(
        "测试已过期产品 的利率", top_k=5, as_of=date.today()
    )
    assert all("BANK-T-OLD-001" not in h.citation.product_code for h in hits)

    # 用产品编号精确限定也应查不到
    scoped = product_service.retriever.retrieve(
        "利率", top_k=5, product_code="BANK-T-OLD-001", as_of=date.today()
    )
    assert scoped == []


def test_active_products_excludes_expired(product_service):
    codes = {p.code for p in product_service.repository.active_products(date.today())}
    assert "BANK-T-OLD-001" not in codes
    assert "BANK-T-GJJ-001" in codes


# =========================================================================== #
# 三、结构化匹配
# =========================================================================== #
def _profile(**overrides) -> CustomerProfile:
    base = {
        "customer_id": "CU-T", "age": 34, "monthly_income": 18000,
        "qualifications": ["公积金"], "credit_query_6m": 3,
        "overdue_count_12m": 0, "overdue_days_max": 0,
        "monthly_debt": 6000, "loans_count": 2,
        "needs": ["债务置换"], "desired_amount": 300000,
    }
    base.update(overrides)
    return CustomerProfile(**base)


def test_hard_filter_rejects_on_qualification(product_service):
    engine = MatchingEngine()
    result = engine.match(
        _profile(qualifications=[]), product_service.repository.list_products()
    )
    assert result.matched is False
    assert any("资质" in r["reason"] for r in result.rejected_summary)


def test_hard_filter_rejects_on_credit_query(product_service):
    engine = MatchingEngine()
    result = engine.match(
        _profile(credit_query_6m=30), product_service.repository.list_products()
    )
    assert result.matched is False


def test_match_ranks_by_configured_weights(product_service):
    result = product_service.match_customer("CU001")

    assert result.matched is True
    assert result.primary is not None
    assert result.primary.pass_probability > 0.5
    assert result.primary.estimated_amount > 0
    # 权重可配置且会被回传（便于回测与审计）
    assert set(result.weights) == {"pass_probability", "rate", "amount"}
    # 必须有可解释的理由
    assert result.primary.reasons


def test_public_match_result_has_no_percent(product_service):
    """对外序列化不含百分比，也不含内部启发式数值。

    `pass_probability` 是内部排序用数值（Field(exclude=True)），
    一旦出现在 API 响应里，业务员就会拿它当"银行审批通过率"对外承诺。
    """
    result = product_service.match_customer("CU001")
    assert result.primary is not None
    payload = result.model_dump(mode="json")
    assert "pass_probability" not in payload["primary"]
    assert all("预估通过率" not in r for r in payload["primary"]["reasons"])
    assert "%" not in payload["primary"]["pass_band_reason"]
    assert payload["primary"]["pass_band"] in {"high", "medium", "low"}
    # 属性本身还在（内部排序仍可用），只是不进序列化
    assert result.primary.pass_probability > 0
    assert "银行" in payload["primary"]["pass_band_reason"]


def test_pass_band_boundaries():
    """三档边界：0.30 / 0.10 分别切高-中、中-低。"""
    assert MatchingEngine._pass_band({"m": 0.30})[0] == "high"
    assert MatchingEngine._pass_band({"m": 0.2999})[0] == "medium"
    assert MatchingEngine._pass_band({"m": 0.10})[0] == "medium"
    assert MatchingEngine._pass_band({"m": 0.0999})[0] == "low"
    for margins in ({"m": 0.9}, {"m": 0.2}, {"m": 0.01}):
        assert "银行" in MatchingEngine._pass_band(margins)[2]


def test_no_match_returns_handoff_reason(product_service):
    """无匹配产品必须转人工，绝不硬配。"""
    result = product_service.match_customer("CU002")
    assert result.matched is False
    assert "转人工" in result.handoff_reason
    assert result.rejected_summary  # 淘汰原因要留痕


def _house_product():
    """房产抵押经营贷（额度基数是评估价，不是收入）—— 就地构造，不依赖夹具。"""
    from product_assistant.domain.models import Product

    return Product(
        code="BANK-T-HOUSE-001", name="测试房产抵押经营贷", bank="测试银行T",
        category="抵押贷", purposes=["经营周转", "负债优化"],
        age_min=22, age_max=60, income_min=5000,
        qualifications=["房产"], occupations=["受薪", "自雇", "企业主"],
        credit_query_6m_max=15, overdue_count_max=2, overdue_days_max=30,
        debt_ratio_max=0.95,
        amount_min=200000, amount_max=3000000,
        rate_min=3.45, rate_max=4.80, term_months=[12, 36, 60],
        quota_multiple=0.7, quota_base="房产评估价",
        effective_date=date(2024, 1, 1),
    )


def test_collateral_product_amount_is_not_income_multiple():
    """抵押类产品的额度不能拿"月收入 × 抵押率"算。

    回归用例：房产抵押经营贷的 quota_base 是"房产评估价"，
    曾经把 0.7（抵押率）当成收入倍数用，算出"月收入 3.4 万 → 2.4 万额度"
    这种离谱数字。现在只对收入类基数做倍数测算，其余取"产品上限与期望的较小值"。
    """
    house = _house_product()
    profile = _profile(
        qualifications=["房产"], monthly_income=34000, desired_amount=1000000,
        needs=["经营周转"],
    )

    amount = MatchingEngine._estimate_amount(profile, house)

    assert amount != profile.monthly_income * house.quota_multiple  # 不是月收入乘出来的
    assert amount == min(house.amount_max, profile.desired_amount)
    # 风险提示要明确告诉业务员"额度以评估价为准"
    risks = MatchingEngine._build_risks(profile, house)
    assert any("评估" in r for r in risks)


def test_income_based_product_still_uses_multiple(product_service):
    """收入类基数（公积金/月收入）照旧按倍数测算 —— 别把上面的修复用过头。"""
    products = product_service.repository.list_products()
    gjj = next(p for p in products if p.quota_base and "公积金" in p.quota_base)
    profile = _profile(monthly_income=18000, desired_amount=1000000)

    amount = MatchingEngine._estimate_amount(profile, gjj)

    assert amount == min(
        gjj.amount_max, profile.desired_amount, profile.monthly_income * gjj.quota_multiple
    )


def test_rejected_summary_explains_each_reason(product_service):
    result = product_service.match_customer("CU002")
    for item in result.rejected_summary:
        assert item["reason"]  # 每个淘汰都要有原因，便于业务员判断


def test_matching_respects_as_of(product_service):
    """政策生效日之前不允许推荐该产品。"""
    engine = MatchingEngine()
    products = product_service.repository.list_products()
    early = engine.match(_profile(), products, as_of=date(2024, 3, 1))

    if early.matched:
        # 2024-03-01 时 BANK-T-DBT-001（2024-09 生效）不应被推荐
        assert early.primary.product_code != "BANK-T-DBT-001"


def test_graph_substitutes_and_multi_hop(product_service):
    subs = dict(product_service.graph.substitutes("BANK-T-GJJ-001"))
    assert "BANK-T-DBT-001" in subs

    ranked = product_service.graph.recommend_by_tags(["公积金"], ["债务置换"])
    assert ranked  # 能通过"资质/用途"多跳召回产品
    assert all(isinstance(code, str) for code, _ in ranked)


# =========================================================================== #
# 四、方案书
# =========================================================================== #
def test_proposal_contains_disclaimer_and_materials(product_service):
    proposal = product_service.generate_proposal("CU001")
    markdown = proposal.to_markdown()

    assert proposal.products
    assert "以银行审批为准" in markdown or "以银行审批结果为准" in markdown
    assert proposal.materials
    assert "不构成任何放款承诺" in markdown
    # 合规：绝不能出现承诺性表述
    for banned in ("包过", "保证下款", "不看征信"):
        assert banned not in markdown


def test_proposal_raises_when_no_match(product_service):
    from agent_platform.errors import PolicyNotFoundError

    with pytest.raises(PolicyNotFoundError):
        product_service.generate_proposal("CU002")


# =========================================================================== #
# 五、Agent 三条路由（在平台上装配后运行）
# =========================================================================== #
@pytest.fixture
def product_agent_runtime(settings, checkpointer, product_service):
    from agent_platform.runtime import AgentRuntime
    from product_assistant.agent import build_product_assistant_agent

    rt = AgentRuntime(settings=settings, checkpointer=checkpointer, embedder=product_service.embedder)
    product_service.register_tools(rt.tools)
    rt.register(build_product_assistant_agent(product_service, rt.tools))
    return rt


def test_agent_route_qa_with_citation(product_agent_runtime):
    result = product_agent_runtime.invoke(
        "product_assistant", "测试公积金信用贷的利率是多少", thread_id="pa_qa"
    )
    assert result.route == "qa"
    assert result.status == "completed"
    assert result.output["citations"]
    assert result.output["refused"] is False


def test_agent_route_qa_refusal_triggers_handoff(product_agent_runtime):
    """无依据 → 拒答 + 转人工，且 Critic 判定通过（因为流程正确）。"""
    result = product_agent_runtime.invoke(
        "product_assistant", "预测下明天股票涨跌", thread_id="pa_refuse"
    )
    assert result.route == "qa"
    assert result.output["refused"] is True
    assert result.output["handoff"] is not None
    tools = [t["tool"] for t in result.tool_calls]
    assert "platform.handoff_to_human" in tools


def test_agent_route_match(product_agent_runtime):
    result = product_agent_runtime.invoke(
        "product_assistant", "客户 CU001 适合做什么产品？", thread_id="pa_match"
    )
    assert result.route == "match"
    assert result.output["match"]["matched"] is True
    assert "主力方案" in result.output["summary"]


def test_agent_route_match_no_result_hands_off(product_agent_runtime):
    result = product_agent_runtime.invoke(
        "product_assistant", "客户 CU002 能做什么产品", thread_id="pa_match_none"
    )
    assert result.route == "match"
    assert result.output["match"]["matched"] is False
    assert result.output["handoff"] is not None


def test_agent_route_proposal(product_agent_runtime):
    result = product_agent_runtime.invoke(
        "product_assistant", "给 CU001 出一份方案书", thread_id="pa_proposal"
    )
    assert result.route == "proposal"
    assert result.output["generated"] is True
    assert result.output["proposal_id"].startswith("PR-")


def test_agent_scoping_reduces_tools_per_route(product_agent_runtime):
    """作用域裁剪要按路由生效：qa 拿不到方案书工具，proposal 拿不到检索工具。"""
    caps = product_agent_runtime.registry.get("product_assistant").capabilities

    qa_tools = {t.name for t in caps.resolve_scoped_tools({"route": "qa"})}
    proposal_tools = {t.name for t in caps.resolve_scoped_tools({"route": "proposal"})}

    assert "product.search_policy" in qa_tools
    assert "product.generate_proposal" not in qa_tools
    assert "product.generate_proposal" in proposal_tools
    # 逃生舱（转人工）在任何作用域都必须可用
    assert "platform.handoff_to_human" in qa_tools
    assert "platform.handoff_to_human" in proposal_tools
    assert len(qa_tools) < len(product_agent_runtime.tools.all())


# =========================================================================== #
# 检索深度策略与阈值校准（换真实 Embedding 后的重校准工具）
# =========================================================================== #
def test_bm25_recall_multiplier_is_configurable(product_service):
    """BM25 深召回系数显式可配；系数变化不改变检索行为正确性。"""
    retriever = product_service.retriever
    assert retriever.bm25_recall_multiplier == 2  # 默认深一倍（过滤后仍够 recall_k）

    retriever.bm25_recall_multiplier = 1
    hits = retriever.retrieve("公积金信用贷的利率区间是多少", top_k=3)
    assert hits, "_multiplier=1 也不该把常规检索搞空"


def test_retriever_calibrate_separates_relevant_from_irrelevant(product_service):
    """校准探针：相关问题与无关问题的 rerank 分必须存在分离带。"""
    labeled = [
        ("公积金信用贷的利率区间是多少", True),
        ("债务置换贷需要准备哪些资料", True),
        ("今天天气怎么样", False),
        ("附近有什么好吃的餐厅", False),
    ]
    report = product_service.retriever.calibrate(labeled)
    rerank = report["rerank"]
    assert rerank["separable"] is True
    assert rerank["relevant_min"] > rerank["irrelevant_max"]
    assert rerank["suggested_threshold"] is not None
    # 明细里每个问题都有检索深度记录（排障用）
    assert all("retrieved" in d for d in report["detail"])
