"""项目三（话术陪练与 AI 考核）测试。

覆盖：模拟客户的"抗性"、四维评分、合规一票否决、全量质检、话术挖掘、双盲校准、
会话持久化与复盘、以及 Agent 的 qc 路由（含高风险告警走 HITL）。
"""

from __future__ import annotations

import pytest

from agent_platform.llm.base import LLMProvider, LLMResponse
from coaching.domain import Persona, RoleplaySession, SessionMessage
from coaching.judge import Judge
from coaching.mining import PhraseMiner
from coaching.qc import QCEngine
from coaching.roleplay import CustomerSimulator, detect_intents


# =========================================================================== #
# 一、意图识别与模拟客户
# =========================================================================== #
def test_detect_intents_recognizes_promise_and_invite():
    intents = detect_intents("您放心我们包过，不看征信，周六来店里签")
    assert "promise" in intents
    assert "invite" in intents


def _session(messages: list[tuple[str, str]], persona: Persona | None = None) -> RoleplaySession:
    p = persona or Persona(persona_id="P-X", name="测试客户", profile="测试")
    return RoleplaySession(
        session_id="s_test",
        persona=p,
        trainee="测试学员",
        messages=[
            SessionMessage(role=role, text=text, ts=float(i * 10))
            for i, (role, text) in enumerate(messages)
        ],
    )


def test_simulator_opening_matches_scene():
    sim = CustomerSimulator()
    session = _session([], Persona(persona_id="P1", name="c", scene="征信解释"))
    assert "征信" in sim.opening(session)


def test_simulator_reacts_suspiciously_to_violation_promise():
    """业务员做违规承诺时，客户必须表现出警惕（训练重点，不能放水）。"""
    sim = CustomerSimulator()
    session = _session(
        [("customer", "你们利率多少？")],
        Persona(persona_id="P-CREDIT", name="c", difficulty="hard"),
    )
    reply = sim.reply(session, "您放心，我们包过，不看征信，保证下款！")
    assert any(k in reply for k in ("不放心", "别家", "真的", "骗", "承诺", "怀疑"))


def test_simulator_answers_qualification_with_facts():
    sim = CustomerSimulator()
    session = _session([("customer", "你好")], Persona(persona_id="P-A", name="c"))
    reply = sim.reply(
        session,
        "我先帮您做资质初审：您公积金缴存多久了？月收入多少？",
        facts={"qualifications": ["公积金"], "monthly_income": 18000},
    )
    assert "公积金" in reply


def test_simulator_resists_when_invited():
    """邀约场景下，难缠客户要会推脱，否则练不出真实邀约能力。"""
    sim = CustomerSimulator()
    persona = Persona(persona_id="P-WAIT", name="拖延型", difficulty="medium")
    session = _session([("customer", "你好")], persona)
    reply = sim.reply(session, "您周六方便过来店里一趟吗？")
    assert any(k in reply for k in ("忙", "线上", "再", "考虑"))


def test_is_resistant_detects_weak_replies():
    assert CustomerSimulator.is_resistant("我再考虑考虑") is True
    assert CustomerSimulator.is_resistant("好的没问题") is False


# =========================================================================== #
# 二、四维评分
# =========================================================================== #
def test_judge_vetoes_on_compliance_violation():
    judge = Judge()
    session = _session([
        ("customer", "你们能保证下款吗？"),
        ("business", "放心，我们包过，保证下款，不看征信！"),
    ])
    card = judge.score_session(session)

    assert card.vetoed is True
    assert card.passed is False
    assert card.total_score < 60
    assert {v.keyword for v in card.violations} >= {"包过", "不看征信"}
    assert any("违规承诺" in w for w in card.weak_points)


def test_judge_scores_need_discovery_by_slot_coverage():
    judge = Judge()
    full = _session([
        ("customer", "我想贷款"),
        ("business", "我先了解下：您公积金缴存多久、月收入多少？征信查询几次、有网贷吗？"
                     "资金用途是什么、大概需要多少额度？"),
    ])
    assert judge.score_session(full).dimensions[1].score == 1.0  # 需求挖掘满分

    empty = _session([("customer", "我想贷款"), ("business", "我们产品很多，您可以看看。")])
    assert judge.score_session(empty).dimensions[1].score == 0.0


def test_judge_rewards_specific_invitation():
    judge = Judge()
    generic = _session([("business", "您可以来我们店里看看")])
    specific = _session([("business", "您周六上午10点方便过来店里吗？")])

    assert judge.score_session(generic).dimensions[3].score == 0.6
    assert judge.score_session(specific).dimensions[3].score == 1.0


def test_judge_penalizes_answering_objection_with_promise():
    judge = Judge()
    session = _session([
        ("customer", "利率会不会太高了？"),
        ("business", "不高不高，我们保证批下来，包过。"),
    ])
    card = judge.score_session(session)
    objection_dim = next(d for d in card.dimensions if d.dimension == "异议处理")
    assert objection_dim.score == 0.0
    assert card.vetoed is True  # 用违规承诺糊弄异议 → 一票否决


def test_judge_gives_credit_for_compliant_objection_handling():
    judge = Judge()
    session = _session([
        ("customer", "利率会不会太高了？我还在考虑。"),
        ("business", "我理解您的顾虑。按您资质预估利率是3.2%到4.6%，以银行审批为准，我不做承诺。"),
    ])
    card = judge.score_session(session)
    objection_dim = next(d for d in card.dimensions if d.dimension == "异议处理")
    assert objection_dim.score > 0.6
    assert card.vetoed is False


def test_judge_produces_actionable_feedback():
    judge = Judge()
    session = _session([("customer", "你好"), ("business", "您好，我们做贷款的。")])
    card = judge.score_session(session)

    assert card.recommendations
    assert card.weak_points
    assert "四连问" in " ".join(card.recommendations) or card.recommendations


def test_judge_calibration_reports_agreement_and_bias():
    report = Judge.calibrate(
        judge_scores=[82, 75, 90, 66, 70],
        human_scores=[80, 78, 88, 70, 72],
        tolerance=10,
    )
    assert report["samples"] == 5
    assert 0 <= report["agreement_rate"] <= 1
    assert "bias_direction" in report
    # 长度不一致必须报错（防止静默错配）
    with pytest.raises(ValueError):
        Judge.calibrate([1, 2], [1])


class _GarbageLLM(LLMProvider):
    """输出纯乱码的假模型：JSON 解析（含 {...} 子串截取重试）必须失败。"""

    name = "garbage-model"

    def complete(self, messages, **kwargs):
        return LLMResponse(content="！@#￥%……&*（）——这不是一段 JSON", model="garbage")


class _OutOfRangeLLM(LLMProvider):
    """部分维度越界（99）的假模型：检验"越界维度回退、合法维度照常融合"。"""

    name = "oor-model"

    def complete(self, messages, **kwargs):
        return LLMResponse(content='{"需求挖掘": 99, "异议处理": 0.7, "邀约促成": 0.2}', model="oor")


def test_judge_falls_back_to_rules_and_flags_degraded_on_garbage_llm():
    """LLM 输出乱码：评分必须照常出（规则分兜底），且评分卡打上 degraded 标记。"""
    session = _session([
        ("customer", "我想贷款"),
        ("business", "您公积金缴存多久、月收入多少？征信查询几次？资金用途和需要多少额度？"),
    ])
    card = Judge(_GarbageLLM()).score_session(session)

    assert card.degraded is True
    assert card.judge_source == "rules"
    # 降级后的分数口径必须与纯规则完全一致 —— 模型挂了只降级，不许带偏分数
    assert card.total_score == Judge().score_session(session).total_score


def test_judge_falls_back_per_dimension_when_llm_score_out_of_range():
    """越界分数（99）不可信：该维度回退规则分；合法维度照常融合，不整轮作废。"""
    session = _session([
        ("customer", "利率会不会太高？我再考虑考虑。"),
        ("business", "我理解您的顾虑，按您资质预估利率3.2%起，以银行审批为准，我不做承诺。"),
    ])
    card = Judge(_OutOfRangeLLM()).score_session(session)

    assert card.degraded is True
    assert card.judge_source == "blended"

    rule_card = Judge().score_session(session)
    needs = next(d for d in card.dimensions if d.dimension == "需求挖掘")
    rule_needs = next(d for d in rule_card.dimensions if d.dimension == "需求挖掘")
    assert needs.score == rule_needs.score, "越界维度必须回退规则分，而不是钳制后混进融合"
    assert "回退规则分" in needs.notes

    # 规则分 1.0（共情+合规提示+解释充分）与 LLM 0.7 各占一半权重
    objection = next(d for d in card.dimensions if d.dimension == "异议处理")
    assert objection.score == pytest.approx(0.85)
    # 合规维度永远纯规则，不参与任何融合与降级
    assert next(d for d in card.dimensions if d.dimension == "合规红线").score == 1.0


# =========================================================================== #
# 三、全量质检
# =========================================================================== #
def test_qc_detects_violation_and_sensitive_info():
    engine = QCEngine()
    report = engine.check({
        "conversation_id": "T1",
        "agent": "业务员甲",
        "outcome": "lost",
        "messages": [
            {"role": "business", "text": "我们包过，不看征信，保证下款。", "ts": 0},
            {"role": "customer", "text": "真的吗？", "ts": 10},
            {"role": "business", "text": "当然，加我 13812345678", "ts": 20},
        ],
    })

    categories = {f.category for f in report.findings}
    assert "违规承诺" in categories
    assert "敏感信息" in categories
    assert report.needs_alert is True
    assert report.score < 100
    # 报告里的敏感号码必须被打码，避免报告本身再泄露一次
    assert "13812345678" not in report.to_text()


def test_qc_detects_short_response_time_gap():
    engine = QCEngine(response_time_limit=60)
    report = engine.check({
        "conversation_id": "T2",
        "agent": "业务员乙",
        "outcome": "pending",
        "messages": [
            {"role": "customer", "text": "我想问下利率", "ts": 0},
            {"role": "business", "text": "稍等", "ts": 600},  # 10 分钟后才回
        ],
    })
    assert any(f.category == "响应时效" for f in report.findings)


def test_qc_detects_missing_sop():
    engine = QCEngine()
    report = engine.check({
        "conversation_id": "T3",
        "agent": "业务员丙",
        "outcome": "won",
        "messages": [
            {"role": "business", "text": "您好，我们可以办贷款。", "ts": 0},
            {"role": "customer", "text": "好的", "ts": 10},
        ],
    })
    sop_findings = [f for f in report.findings if f.category == "SOP缺失"]
    assert sop_findings
    # 未做资质问询 + 未做合规提示 + 未邀约
    assert len(sop_findings) >= 2


def test_qc_clean_conversation_has_no_findings(coaching_service):
    """来自夹具的 CONV-1（规范话术）不应触发高风险。"""
    report = coaching_service.qc("TCONV-1")
    assert report.needs_alert is False
    assert not any(f.category == "违规承诺" for f in report.findings)


def test_qc_all_covers_every_conversation(coaching_service):
    reports = coaching_service.qc_all()
    assert len(reports) == len(coaching_service.conversations)  # 覆盖率 100%


def test_qc_unknown_conversation_raises(coaching_service):
    with pytest.raises(KeyError):
        coaching_service.qc("NOT-EXIST")


# =========================================================================== #
# 四、话术挖掘
# =========================================================================== #
def test_mining_extracts_from_won_only_and_excludes_violations():
    miner = PhraseMiner()
    result = miner.mine([
        {
            "conversation_id": "W1", "outcome": "won",
            "messages": [
                {"role": "business", "text": "我先帮您做资质初审：公积金缴存多久了？征信查询几次？", "ts": 0},
                {"role": "customer", "text": "公积金交了3年，查询3次吧", "ts": 10},
                {"role": "business", "text": "我们包过，保证下款。", "ts": 20},
                {"role": "customer", "text": "好的", "ts": 30},
            ],
        },
        {
            "conversation_id": "L1", "outcome": "lost",
            "messages": [
                {"role": "business", "text": "我先帮您做资质初审：您公积金缴存多久了？", "ts": 0},
                {"role": "customer", "text": "3年", "ts": 10},
            ],
        },
    ])

    texts = [p.text for p in result.mined]
    assert any("资质初审" in t for t in texts)
    assert not any("包过" in t for t in texts)          # 违规话术绝不入库
    assert all(p.source_conversation == "W1" for p in result.mined)  # 只来自成单对话


def test_mining_scores_positive_customer_reaction_higher():
    miner = PhraseMiner()
    result = miner.mine([{
        "conversation_id": "W2", "outcome": "won",
        "messages": [
            {"role": "business", "text": "我先确认一下您的资质：公积金缴存多久了？", "ts": 0},
            {"role": "customer", "text": "好的，我公积金交了3年，月收入18000", "ts": 10},
        ],
    }])
    assert result.mined
    assert result.mined[0].score > 0.5
    assert "正向回应" in result.mined[0].reason or "资质信息" in result.mined[0].reason


def test_mining_semantic_dedup_keeps_best_of_near_duplicates():
    """字面不同但 2-gram 高度重合的同义话术只留一条（默认离线去重路径）。"""
    miner = PhraseMiner()
    result = miner.mine([
        {
            "conversation_id": "W3", "outcome": "won",
            "messages": [
                {"role": "business", "text": "我先帮您做资质初审：公积金缴存多久了？征信查询几次？", "ts": 0},
                {"role": "customer", "text": "好的，公积金交了3年，查询3次", "ts": 10},
            ],
        },
        {
            "conversation_id": "W4", "outcome": "won",
            "messages": [
                {"role": "business", "text": "我先帮您做资质初审：公积金缴存多久了呀？征信查询几次？", "ts": 0},
                {"role": "customer", "text": "好的，公积金交了3年，查询3次", "ts": 10},
            ],
        },
    ])

    assert len(result.mined) == 1, "同义话术不能重复入库"
    assert result.stats["dropped_dupes"] == 1
    # 两条得分相同，按分数稳定排序保留先出现的一条 —— 结果必须确定性可复现
    assert result.mined[0].source_conversation == "W3"


def test_mining_semantic_dedup_with_injected_embedder():
    """注入 embedding 时优先按向量判重：字面几乎不重合的同义话术也能去重。"""

    class _TwinEmbedder:
        """把"问资质"类话术映射到同一向量（模拟真实 embedding 的语义聚拢）。"""

        def embed_documents(self, texts):
            return [[1.0, 0.0] if ("资质" in t or "公积金" in t) else [0.0, 1.0] for t in texts]

    result = PhraseMiner().mine(
        [
            {
                "conversation_id": "W5", "outcome": "won",
                "messages": [
                    {"role": "business", "text": "我先帮您做资质初审：公积金缴存多久了？征信查询几次？", "ts": 0},
                    {"role": "customer", "text": "好的，公积金交了3年", "ts": 10},
                ],
            },
            {
                "conversation_id": "W6", "outcome": "won",
                "messages": [
                    {"role": "business", "text": "咱们先看看资质：您月收入大概多少？有没有房产或保单？", "ts": 0},
                    {"role": "customer", "text": "好的，月收入2万，有套房", "ts": 10},
                ],
            },
        ],
        embedder=_TwinEmbedder(),
    )

    assert len(result.mined) == 1, "向量判为同义的话术只留一条"
    assert result.stats["dropped_dupes"] == 1


def test_mining_dedup_falls_back_to_bigram_when_embedder_fails():
    """向量化挂了不能拖垮去重：自动退化为 2-gram Jaccard，重复话术照样被拦。"""

    class _BoomEmbedder:
        def embed_documents(self, texts):
            raise RuntimeError("embedding service down")

    result = PhraseMiner().mine(
        [
            {
                "conversation_id": "W7", "outcome": "won",
                "messages": [
                    {"role": "business", "text": "我先帮您做资质初审：公积金缴存多久了？征信查询几次？", "ts": 0},
                    {"role": "customer", "text": "好的，公积金交了3年", "ts": 10},
                ],
            },
            {
                "conversation_id": "W8", "outcome": "won",
                "messages": [
                    {"role": "business", "text": "我先帮您做资质初审：公积金缴存多久了呀？征信查询几次？", "ts": 0},
                    {"role": "customer", "text": "好的，公积金交了3年", "ts": 10},
                ],
            },
        ],
        embedder=_BoomEmbedder(),
    )

    assert len(result.mined) == 1
    assert result.stats["dropped_dupes"] == 1


# =========================================================================== #
# 五、会话持久化与复盘
# =========================================================================== #
def test_session_lifecycle_and_replay(coaching_service):
    session = coaching_service.start_session("P-T-EASY", "学员甲")
    assert session.messages[-1].role == "customer"

    coaching_service.reply(session.session_id, "您好，我先了解一下您的资质？")
    coaching_service.reply(session.session_id, "好的，您的公积金缴存多久了？")

    replay = coaching_service.replay(session.session_id)
    assert len(replay) >= 5
    assert replay[0]["role"] == "customer"

    card = coaching_service.finish_session(session.session_id)
    assert card.session_id == session.session_id
    assert 0 <= card.total_score <= 100

    # 会话已结束，不能再继续对话
    with pytest.raises(ValueError):
        coaching_service.reply(session.session_id, "再说一句")


def test_unknown_persona_raises(coaching_service):
    with pytest.raises(KeyError):
        coaching_service.start_session("NOT-EXIST")


# =========================================================================== #
# 六、Agent 的 qc 路由（高风险告警必须走 HITL）
# =========================================================================== #
@pytest.fixture
def coaching_agent_runtime(settings, checkpointer, coaching_service):
    from agent_platform.runtime import AgentRuntime
    from coaching.agent import build_coaching_agent

    rt = AgentRuntime(settings=settings, checkpointer=checkpointer)
    coaching_service.register_tools(rt.tools)
    rt.register(build_coaching_agent(coaching_service, rt.tools))
    return rt


def test_agent_qc_route_interrupts_for_supervisor_alert(coaching_agent_runtime):
    """检出高风险 → 告警主管是高风险动作 → 必须挂起等人工审批。"""
    result = coaching_agent_runtime.invoke(
        "coaching",
        "请对 TCONV-2 做合规质检并告警主管",
        context={"conversation_id": "TCONV-2", "supervisor": "张主管"},
        thread_id="ca_qc",
    )
    assert result.route == "qc"
    assert result.status == "interrupted"
    assert result.pending_approval["tool"] == "platform.send_external_message"
    assert "合规告警" in result.pending_approval["arguments"]["content"]


def test_agent_qc_route_completes_after_approval(coaching_agent_runtime):
    coaching_agent_runtime.invoke(
        "coaching",
        "对 TCONV-2 做合规质检并告警主管",
        context={"conversation_id": "TCONV-2", "supervisor": "张主管"},
        thread_id="ca_qc2",
    )
    resumed = coaching_agent_runtime.resume(
        "ca_qc2", {"approved": True, "decided_by": "张主管", "comment": "确认属实"}
    )
    assert resumed.status == "completed"
    assert resumed.output["alert_sent"] is True
    assert resumed.output["needs_alert"] is True
    assert resumed.approvals[0]["decided_by"] == "张主管"


def test_agent_qc_route_without_high_risk_does_not_alert(coaching_agent_runtime):
    """无高风险时不应触发告警，更不应挂起 —— 避免告警疲劳。"""
    result = coaching_agent_runtime.invoke(
        "coaching",
        "请对 TCONV-1 做合规质检",
        context={"conversation_id": "TCONV-1"},
        thread_id="ca_qc3",
    )
    assert result.status == "completed"
    assert result.output["needs_alert"] is False
    assert "platform.send_external_message" not in [t["tool"] for t in result.tool_calls]


def test_agent_mining_route(coaching_agent_runtime):
    result = coaching_agent_runtime.invoke(
        "coaching", "帮我挖掘高转化话术", thread_id="ca_mining"
    )
    assert result.route == "mining"
    assert result.status == "completed"
    assert "mined" in result.output["result"]


def test_agent_practice_route_starts_session(coaching_agent_runtime):
    result = coaching_agent_runtime.invoke(
        "coaching",
        "我要练习首触话术",
        context={"persona_id": "P-T-HARD", "trainee": "学员乙"},
        thread_id="ca_practice",
    )
    assert result.route == "practice"
    assert result.output["stage"] == "started"
    assert result.output["session_id"]


def test_agent_scoping_by_route(coaching_agent_runtime):
    caps = coaching_agent_runtime.registry.get("coaching").capabilities
    qc_tools = {t.name for t in caps.resolve_scoped_tools({"route": "qc"})}

    assert "coaching.qc" in qc_tools
    # qc 路由允许对外发消息（告警通道）
    assert "platform.send_external_message" in qc_tools
    # 陪练工具不在 qc 作用域内
    assert "coaching.start_session" not in qc_tools
