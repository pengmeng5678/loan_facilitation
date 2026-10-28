"""项目四（助贷对话数据中台）测试。

三层验证：
    ① **单元**：脱敏 / 分类 / 切块 / 抽取 / 导出格式（纯函数，不碰 IO）
    ② **服务**：端到端管线 + 审核状态机 + 幂等
    ③ **HTTP**：/data 路由（TestClient，走真实装配）

数据全部现场生成（微信夹具 + tmp_path），不依赖仓库 .data，不联网。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from data_platform.chunker import ConversationBuilder
from data_platform.domain import ChatSession, RawMessage
from data_platform.errors import ExportBlocked, InvalidTransition, ReviewRequired
from data_platform.exporter import DatasetExporter
from data_platform.filters import ConversationFilter
from data_platform.masking import MaskingEngine
from data_platform.source import (
    DEMO_SESSIONS,
    JsonlSource,
    WeChatSqliteSource,
    build_wechat_fixture,
    load_sessions,
)


# =========================================================================== #
# 夹具
# =========================================================================== #
def msg(text: str, role: str = "customer", ts: float = 0.0, mid: str = "") -> RawMessage:
    return RawMessage(
        msg_id=mid or f"m-{role}-{ts}-{abs(hash(text)) % 10000}",
        session_id="s-test",
        role=role,  # type: ignore[arg-type]
        content=text,
        ts=ts,
    )


def dialogue() -> list[RawMessage]:
    """一段信息完整的业务对话（10 轮，含价格与 PII，用于各层测试）。"""
    return [
        msg("你好，我想问下公积金能不能贷？", "customer", 0),
        msg("您好，我是助贷顾问小李。您公积金缴存多久了？", "business", 40),
        msg("交了 3 年，查询 3 次，有按揭房，还有 3 笔网贷共 15 万想置换。", "customer", 90),
        msg("可以做债务置换，年化大概 3.2% 到 4.6%，额度 20 万到 50 万，以审批为准。", "business", 150),
        msg("利率能再低点吗？", "customer", 210),
        msg("低于 3% 做不到，承诺具体利率不合规。您的优势是公积金连续缴存。", "business", 260),
        msg("行，我周六上午 10 点过来，电话 13812345678。", "customer", 300),
        msg("好的，周六上午 10 点给您留位，带身份证和缴存明细。", "business", 340),
        msg("签了合同是不是就能放款？", "customer", 380),
        msg("签约后还要银行审批，一般 1 到 3 个工作日出结果。", "business", 420),
    ]


@pytest.fixture
def wechat_db(settings) -> Path:
    return build_wechat_fixture(settings.data_source_abs_path, DEMO_SESSIONS)


@pytest.fixture
def svc(settings, wechat_db):
    from data_platform.service import DataPlatformService

    service = DataPlatformService(settings=settings)
    service.bootstrap()
    yield service
    service.close()


@pytest.fixture
def ingested(svc):
    report = svc.ingest()
    return svc, report


@pytest.fixture
def client(app_context_fixture) -> TestClient:
    from agent_platform.api.app import app

    return TestClient(app)


# =========================================================================== #
# 一、脱敏
# =========================================================================== #
def test_mask_phone():
    result = MaskingEngine().mask("我的电话是 13812345678")
    assert "138****5678" in result.text
    assert result.findings[0].kind == "phone"


def test_mask_id_card_before_bank_card():
    """18 位身份证必须被识别为身份证，而不是 16-19 位的银行卡。"""
    result = MaskingEngine().mask("身份证 320102199001011234")
    assert [f.kind for f in result.findings] == ["id_card"]
    assert "320102********1234" in result.text


def test_mask_bank_card():
    result = MaskingEngine().mask("卡号 6222021234567890123")
    assert [f.kind for f in result.findings] == ["bank_card"]
    assert "6222***********0123" in result.text


def test_mask_email():
    result = MaskingEngine().mask("邮箱 zhang.san@example.com")
    assert result.findings[0].kind == "email"
    assert "@example.com" in result.text and "zhang.san" not in result.text


def test_mask_api_key():
    result = MaskingEngine().mask("key=sk-abcdefghijklmnopqrst")
    assert result.findings[0].kind == "api_key"
    assert "sk-abc" not in result.text


def test_mask_price_amount():
    result = MaskingEngine().mask("额度 30 万，另外 50,000 元")
    assert "[金额已脱敏]" in result.text
    assert "30" not in result.text


def test_mask_price_rate():
    result = MaskingEngine().mask("年化 4.6% 左右")
    assert "[利率已脱敏]" in result.text
    assert result.price_hits == 1


def test_price_point_requires_unit():
    """裸的"N 点"是时间，不能当点位脱敏（假阳性比漏报更伤可用性）。"""
    engine = MaskingEngine()
    assert "10 点" in engine.mask("周六上午 10 点见").text
    assert "3" not in engine.mask("服务费 3 个点").text


def test_mask_known_name():
    engine = MaskingEngine(known_names=["张先生"])
    assert "张**" in engine.mask("张先生您好").text


def test_mask_is_idempotent():
    """三层冗余脱敏的前提：第二次跑必须零命中。"""
    engine = MaskingEngine(known_names=["张先生"])
    text = "张先生电话 13812345678，身份证 320102199001011234，额度 30 万，年化 4.6%"
    first = engine.mask(text)
    second = engine.mask(first.text)
    assert second.findings == []
    assert second.text == first.text


def test_mask_fullwidth_phone_not_bypassed():
    """全角数字 + 全角分隔符是"看着像打了码"的绕过写法，必须拦住。"""
    engine = MaskingEngine()
    result = engine.mask("电话１３８－１２３４－５６７８")
    assert result.findings[0].kind == "phone"
    assert "138****5678" in result.text
    assert "１３８" not in result.text
    # 连续全角数字（不带分隔符）同样要命中
    assert "138****5678" in engine.mask("电话１３８１２３４５６７８").text


def test_mask_fullwidth_id_card():
    """全角身份证：归一化后按 18 位身份证处理，而不是漏掉或误判成银行卡。"""
    result = MaskingEngine().mask("身份证３２０１０２１９９００１０１１２３４")
    assert [f.kind for f in result.findings] == ["id_card"]
    assert "320102********1234" in result.text
    assert "３２０１０２" not in result.text


def test_mask_fullwidth_is_idempotent():
    """归一化不能破坏幂等：同一段全角文本跑两遍，第二遍零命中且结果一致。"""
    engine = MaskingEngine()
    text = "电话１３８－１２３４－５６７８，身份证３２０１０２１９９００１０１１２３４，额度３０万"
    first = engine.mask(text)
    second = engine.mask(first.text)
    assert second.findings == []
    assert second.text == first.text


def test_verify_detects_residual():
    assert MaskingEngine().verify("电话 13812345678")


def test_mask_messages_marks_layer():
    masked, findings, _ = MaskingEngine().mask_messages([msg("电话 13812345678")], layer="clean")
    assert masked[0].msg_id.endswith("#clean")
    assert len(findings) == 1


def test_rules_priority_order():
    rules = MaskingEngine(known_names=["张三"]).rules()
    names = [r["rule"] for r in rules]
    assert names.index("id_card") < names.index("bank_card")
    assert names.index("api_key") < names.index("phone")


# =========================================================================== #
# 二、分类与质量
# =========================================================================== #
def test_system_block_dropped():
    decision = ConversationFilter().classify([msg("对方撤回了一条消息"), msg("你已添加了对方")])
    assert decision.category == "system" and not decision.keep


def test_media_block_dropped():
    decision = ConversationFilter().classify([msg("[图片]"), msg("[语音]", "business", 5)])
    assert decision.category == "media" and not decision.keep


def test_too_short_dropped():
    decision = ConversationFilter().classify([msg("在吗"), msg("在的", "business", 5)])
    assert decision.category == "too_short"


def test_sensitive_kept_but_never_training_data():
    decision = ConversationFilter().classify([
        msg("你们这是骗人的吧？我要投诉，还要打 12378 举报！"),
        msg("非常抱歉，我马上核实。", "business", 5),
    ])
    assert decision.category == "sensitive"
    assert decision.keep is True          # 主管必须看到
    assert decision.dataset_eligible is False  # 但永远不进训练集
    assert decision.risk is True


def test_sensitive_priority_over_too_short():
    """四个字的投诉也是投诉，不能因为短就丢。"""
    decision = ConversationFilter().classify([msg("你们骗人"), msg("抱歉", "business", 5)])
    assert decision.category == "sensitive"


def test_spam_dropped():
    decision = ConversationFilter().classify([
        msg("秒批！无需审核！日赚 500，点击链接 http://spam.example.com"),
        msg("免费办理，代理加盟", "customer", 5),
    ])
    assert decision.category == "spam" and not decision.keep


def test_chitchat_dropped():
    decision = ConversationFilter().classify([msg("嗯嗯"), msg("好的", "business", 3), msg("谢谢"), msg("不客气", "business", 6)])
    assert decision.category == "chitchat"


def test_duplicate_flag():
    decision = ConversationFilter().classify(dialogue(), duplicate=True)
    assert decision.category == "duplicate" and not decision.keep


def test_high_value_detected():
    decision = ConversationFilter().classify(dialogue())
    assert decision.category == "high_value"
    assert decision.keep and decision.dataset_eligible


def test_quality_score_monotonic():
    f = ConversationFilter()
    short = f.quality([msg("咨询一下额度", "customer"), msg("好的，您说", "business", 3)])
    rich = f.quality(dialogue())
    assert rich > short


def test_extract_qa_both_directions():
    pairs = ConversationFilter().extract_qa(dialogue())
    askers = {p.asker for p in pairs}
    assert "customer" in askers
    assert all(p.question.endswith(("？", "?")) for p in pairs)
    assert all(p.answer for p in pairs)


def test_scene_of():
    f = ConversationFilter()
    assert f.scene_of("想把网贷置换成银行低息") == "债务置换"
    assert f.scene_of("征信查询次数太多") == "征信解读"
    assert f.scene_of("先聊点别的") == "其他"


# =========================================================================== #
# 三、切块
# =========================================================================== #
def _builder() -> ConversationBuilder:
    return ConversationBuilder(window_minutes=5, masker=MaskingEngine(), conv_filter=ConversationFilter())


def _session(messages: list[RawMessage], sid: str = "s-test") -> ChatSession:
    return ChatSession(
        session_id=sid,
        customer_id="CU001",
        customer_name="张先生",
        messages=[m.model_copy(update={"session_id": sid}) for m in messages],
    )


def test_split_by_window():
    blocks = _builder().build(_session([msg("咨询额度", "customer", 0), msg("好的您说", "business", 400)]))
    assert len(blocks) == 2


def test_merge_same_role():
    merged = _builder().merge_roles([
        msg("第一句", "customer", 0), msg("第二句", "customer", 10), msg("回复", "business", 20),
    ])
    assert len(merged) == 2
    assert "\n" in merged[0].content


def test_dedupe_within():
    assert len(_builder().dedupe_within([msg("一样的话"), msg("一样的话", "customer", 5)])) == 1


def test_block_id_is_stable():
    session = _session(dialogue())
    first = _builder().build(session)
    second = _builder().build(session)
    assert first[0].block_id == second[0].block_id


def test_cross_session_duplicate_detected():
    builder = _builder()
    seen: set[str] = set()
    session = _session(dialogue(), sid="s-1")
    builder.build(session, seen_signatures=seen)
    copied = _session(dialogue(), sid="s-2")
    blocks = builder.build(copied, seen_signatures=seen)
    assert blocks[0].category == "duplicate"


def test_empty_session_yields_no_blocks():
    assert _builder().build(_session([])) == []


# =========================================================================== #
# 四、数据源
# =========================================================================== #
def test_wechat_source_reads_messages(settings, wechat_db):
    source = WeChatSqliteSource(wechat_db)
    messages = list(source.iter_messages())
    assert len(messages) == 33
    assert {m.role for m in messages} == {"customer", "business"}


def test_wechat_source_display_name(settings, wechat_db):
    messages = list(WeChatSqliteSource(wechat_db).iter_messages())
    zhang = [m for m in messages if m.session_id == "wxid_demo_zhang"]
    assert zhang[0].sender_name == "张先生"


def test_wechat_source_group_sender_parsed(wechat_db):
    messages = list(WeChatSqliteSource(wechat_db).iter_messages())
    group = [m for m in messages if m.session_id.endswith("@chatroom")]
    assert group
    assert group[0].sender_wxid == "wxid_member_a"
    assert not group[0].content.startswith("wxid_member_a:")


def test_wechat_source_media_placeholder(wechat_db):
    messages = list(WeChatSqliteSource(wechat_db).iter_messages())
    assert any(m.content == "[图片]" and m.msg_type == "图片" for m in messages)


def test_wechat_source_missing_file(tmp_path):
    source = WeChatSqliteSource(tmp_path / "nope.sqlite")
    with pytest.raises(FileNotFoundError):
        list(source.iter_messages())


def test_jsonl_source(tmp_path):
    path = tmp_path / "conv.jsonl"
    path.write_text(json.dumps({
        "conversation_id": "C1", "customer_id": "CU1", "agent": "小王",
        "started_at": "2026-08-01T10:00:00",
        "messages": [
            {"role": "business", "text": "您好", "ts": 0},
            {"role": "customer", "text": "想问额度", "ts": 30},
        ],
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    messages = list(JsonlSource(path).iter_messages())
    assert len(messages) == 2
    assert messages[0].role == "business"
    sessions = load_sessions(messages)
    assert sessions[0].session_id == "C1"


# =========================================================================== #
# 五、审核闸门
# =========================================================================== #
def _pending(svc) -> list:
    return svc.list_staging(status="pending", limit=10)


def test_submit_is_idempotent(ingested):
    svc, report = ingested
    assert report.staged_new == 3
    second = svc.ingest()
    assert second.staged_new == 0
    assert second.staged_existing == 3


def test_approve_and_audit(ingested):
    svc, _ = ingested
    target = _pending(svc)[0]
    approved = svc.review(target.staging_id, "approve", reviewer="主管A", comment="合格")
    assert approved.status == "approved"
    audits = svc.audits(staging_id=target.staging_id)
    assert audits[0]["action"] == "approve"
    assert {row["action"] for row in audits} == {"submit", "approve"}


def test_approve_twice_no_extra_audit(ingested):
    svc, _ = ingested
    target = _pending(svc)[0]
    svc.review(target.staging_id, "approve", reviewer="主管A")
    before = len(svc.audits(staging_id=target.staging_id))
    svc.review(target.staging_id, "approve", reviewer="主管A")
    assert len(svc.audits(staging_id=target.staging_id)) == before


def test_reject_approved_requires_comment(ingested):
    svc, _ = ingested
    target = _pending(svc)[0]
    svc.review(target.staging_id, "approve", reviewer="主管A")
    with pytest.raises(InvalidTransition):
        svc.review(target.staging_id, "reject", reviewer="主管A")
    revoked = svc.review(target.staging_id, "reject", reviewer="主管A", comment="发现客户信息错误")
    assert revoked.status == "rejected"


def test_approve_after_reject_raises(ingested):
    svc, _ = ingested
    target = _pending(svc)[0]
    svc.review(target.staging_id, "reject", reviewer="主管A", comment="样本不合格")
    with pytest.raises(InvalidTransition):
        svc.review(target.staging_id, "approve", reviewer="主管A")


def test_edit_remasks_and_returns_to_pending(ingested):
    svc, _ = ingested
    target = _pending(svc)[0]
    svc.review(target.staging_id, "approve", reviewer="主管A")
    edited = svc.review(
        target.staging_id, "edit", reviewer="主管A", comment="补一句",
        messages=[
            {"role": "customer", "content": "我电话 13900001111，想问额度"},
            {"role": "business", "content": "收到，稍后给您回电。"},
        ],
    )
    assert edited.status == "pending"
    assert edited.revision == 2
    assert "13900001111" not in edited.messages[0].content
    assert "139****1111" in edited.messages[0].content


def test_submit_non_keepable_raises(ingested):
    from data_platform.domain import ConversationBlock

    svc, _ = ingested
    block = ConversationBlock(block_id="blk_x", session_id="s", category="spam")
    with pytest.raises(InvalidTransition):
        svc.gate.submit(block)


def test_assert_approved_raises(ingested):
    svc, _ = ingested
    with pytest.raises(ReviewRequired):
        svc.gate.assert_approved([_pending(svc)[0].staging_id])


# =========================================================================== #
# 六、导出
# =========================================================================== #
def _approve_all(svc) -> None:
    for item in _pending(svc):
        svc.review(item.staging_id, "approve", reviewer="主管A", comment="批量通过")


def test_export_requires_approved(ingested):
    svc, _ = ingested
    with pytest.raises(ExportBlocked):
        svc.export("sharegpt")


def test_export_sharegpt_structure(ingested):
    svc, _ = ingested
    _approve_all(svc)
    manifest = svc.export("sharegpt")
    rows = [json.loads(line) for line in Path(manifest.path).read_text(encoding="utf-8").splitlines()]
    assert rows
    assert {"from", "value"} <= set(rows[0]["conversations"][0])
    assert rows[0]["conversations"][0]["from"] in ("human", "gpt")


def test_export_alpaca_structure(ingested):
    svc, _ = ingested
    _approve_all(svc)
    manifest = svc.export("alpaca")
    row = json.loads(Path(manifest.path).read_text(encoding="utf-8").splitlines()[0])
    assert {"instruction", "input", "output"} <= set(row)
    assert row["output"]


def test_export_openai_structure(ingested):
    svc, _ = ingested
    _approve_all(svc)
    manifest = svc.export("openai")
    row = json.loads(Path(manifest.path).read_text(encoding="utf-8").splitlines()[0])
    assert row["messages"][0]["role"] == "system"
    assert any(m["role"] == "assistant" for m in row["messages"])


def test_export_rag_csv(ingested):
    import csv

    svc, _ = ingested
    _approve_all(svc)
    manifest = svc.export("rag_csv")
    with Path(manifest.path).open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows
    assert {"id", "text", "scene", "quality"} <= set(rows[0])


def test_export_is_idempotent(ingested):
    svc, _ = ingested
    _approve_all(svc)
    first = svc.export("sharegpt")
    second = svc.export("sharegpt")
    assert second.reused is True
    assert second.path == first.path


def test_export_excludes_sensitive(ingested):
    """敏感对话即使被批准，也不能进训练集。"""
    svc, _ = ingested
    _approve_all(svc)
    manifest = svc.export("rag_csv")
    assert len(manifest.staging_ids) == 2  # 3 条待审里只有 2 条 eligible


def test_export_rejects_unknown_format(ingested):
    svc, _ = ingested
    _approve_all(svc)
    with pytest.raises(ExportBlocked):
        svc.export("parquet")  # type: ignore[arg-type]


def test_exporter_render_is_deterministic():
    exporter = DatasetExporter
    rows = [{"b": 1, "a": 2}]
    assert exporter._render("sharegpt", rows) == exporter._render("sharegpt", rows)


# =========================================================================== #
# 七、知识产物
# =========================================================================== #
def test_build_knowledge(ingested):
    svc, _ = ingested
    _approve_all(svc)
    report = svc.build_knowledge()
    assert report.chunks > 0
    assert report.articles == 2          # 只有 2 条 eligible
    assert report.rule_articles == 2     # echo 模型产出空字段 → 规则兜底


def test_chunks_are_masked(ingested):
    svc, _ = ingested
    _approve_all(svc)
    svc.build_knowledge()
    text = "".join(c.text for c in svc.store.list_chunks())
    assert "13812345678" not in text
    assert "320102199001011234" not in text


def test_article_rule_fallback_fields(ingested):
    svc, _ = ingested
    _approve_all(svc)
    svc.build_knowledge()
    articles = svc.store.list_articles()
    assert articles
    assert all(a.extractor == "rule" for a in articles)
    assert all(a.customer_utterance and a.recommended_reply for a in articles)
    assert all(0 <= a.confidence <= 1 for a in articles)


def test_search_knowledge(ingested):
    svc, _ = ingested
    _approve_all(svc)
    svc.build_knowledge()
    hits = svc.search_knowledge("公积金", top_k=3)
    assert hits
    assert any("公积金" in str(h["text"]) for h in hits)


def test_search_empty_when_no_data(svc):
    assert svc.search_knowledge("任何问题") == []


def test_knowledge_incremental_build(ingested):
    """增量构建：总数 = 旧 + 1，且旧条目不重算（只把新条目送去向量化）。"""
    svc, _ = ingested
    _approve_all(svc)
    # 水位与批准同秒时，>= 口径会把旧条目再算一遍（幂等的重复，代价为零）；
    # 这里让水位落到批准的下一秒，"旧条目不重算"才可被确定性地断言
    time.sleep(1.1)
    svc.build_knowledge()
    assert svc.store.get_build_watermark("knowledge")  # 全量构建也推进水位
    before_chunks = svc.store.chunk_count()
    before_articles = svc.store.article_count()
    old_chunk_ids = {c.chunk_id for c in svc.store.list_chunks()}

    # 计数桩：记录增量构建实际送去向量化的文本（旧条目若被重建就会出现在这里）
    embedded: list[list[str]] = []
    real_embed = svc.knowledge.embedder.embed_documents

    def spy_embed(texts):
        embedded.append(list(texts))
        return real_embed(texts)

    svc.knowledge.embedder.embed_documents = spy_embed

    # 构建之后又来一条新对话：走真实管线（切块 → 提交 → 批准）
    new_session = ChatSession(
        session_id="wxid_demo_new",
        customer_id="CU_new0001",
        messages=[
            msg("我想问下车子抵押贷款能做吗？", "customer", 0),
            msg("可以做，需要看车况和征信，利率以银行审批为准。", "business", 30),
        ],
    )
    block = svc.builder.build(new_session, seen_signatures=set())[0]
    staged, _ = svc.gate.submit(block)
    svc.gate.approve(staged.staging_id, reviewer="测试员")

    report = svc.rebuild_knowledge(incremental=True)
    assert report.incremental is True
    assert report.chunks == 1                            # 新对话足够短 → 恰好 1 个切片
    assert svc.store.chunk_count() == before_chunks + 1
    assert svc.store.article_count() == before_articles + 1
    flat = [t for batch in embedded for t in batch]
    assert flat and all("抵押" in t for t in flat)        # 只向量化了新条目
    assert not any("公积金" in t for t in flat)           # 旧条目没有被重算
    assert old_chunk_ids <= {c.chunk_id for c in svc.store.list_chunks()}  # 旧切片原样保留

    # 重复增量构建不产生重复产物（upsert 幂等；同秒临界条目重跑一次无害）
    svc.rebuild_knowledge(incremental=True)
    assert svc.store.chunk_count() == before_chunks + 1
    assert svc.store.article_count() == before_articles + 1


# =========================================================================== #
# 八、服务与漏斗
# =========================================================================== #
def test_pipeline_counts(ingested):
    svc, report = ingested
    assert report.raw_messages == 33
    assert report.sessions == 7
    assert report.blocks_total == 7
    assert report.blocks_kept == 3
    assert report.blocks_dropped == 4
    pipe = svc.pipeline()
    assert pipe["blocks"] == 7
    assert pipe["drop_rate"] == round(4 / 7, 3)
    assert pipe["pending"] == 3


def test_dry_run_does_not_write(settings, svc):
    report = svc.ingest(dry_run=True)
    assert report.staged_new == 0
    assert svc.stats()["raw_messages"] == 0
    assert svc.stats()["staging"]["total"] == 0


def test_feed_conversations(ingested):
    svc, _ = ingested
    _approve_all(svc)
    feed = svc.feed_conversations()
    assert len(feed) == 2
    assert all({"conversation_id", "messages", "outcome"} <= set(row) for row in feed)
    # 投诉那条不能出现在"可对外使用"的语料里
    assert all(row["conversation_id"] != "wxid_demo_li" for row in feed)
    assert any(row["outcome"] == "won" for row in feed)


def test_rules_payload(ingested):
    svc, _ = ingested
    payload = svc.rules()
    assert len(payload["categories"]) == 9
    assert payload["layers"]
    assert any(r["kind"] == "price" for r in payload["masking"])


def test_register_tools(svc):
    from agent_platform.tools.registry import ToolRegistry

    registry = ToolRegistry()
    names = svc.register_tools(registry)
    assert set(names) == {
        "data.ingest", "data.list_staging", "data.review",
        "data.export", "data.search_knowledge", "data.pipeline_stats",
    }
    # 审核是高风险动作 → 必须走 HITL
    assert registry.get("data.review").risk_level == "high"


def test_stats_shape(ingested):
    svc, _ = ingested
    stats = svc.stats()
    assert stats["raw_messages"] == 33
    assert stats["blocks_by_category"]["high_value"] == 1
    assert stats["last_ingest"] is not None


def test_raw_batch_id_distinguishes_ingests(settings, svc):
    """两轮抽取的批次号可区分，且 msg_id 去重语义不被 batch_id 破坏。"""
    r1 = svc.ingest()
    assert r1.batch_id

    # 源库"新到一条消息"（模拟增量抽取场景）
    conn = sqlite3.connect(settings.data_source_abs_path)
    try:
        conn.execute(
            "INSERT INTO message (type, isSend, createTime, talker, content, sender) "
            "VALUES (1, 0, ?, ?, ?, ?)",
            (1_790_000_500, "wxid_demo_late", "请问抵押贷利率多少？电话 13700001111", "wxid_demo_late"),
        )
        conn.commit()
    finally:
        conn.close()

    r2 = svc.ingest()
    assert r2.batch_id and r1.batch_id != r2.batch_id
    assert svc.stats()["raw_messages"] == 34              # 33 旧消息去重不重复 + 1 新消息
    assert set(svc.store.raw_batch_ids()) == {r1.batch_id, r2.batch_id}
    assert svc.stats()["last_ingest"]["batch_id"] == r2.batch_id  # 统计里也能对上批次


# =========================================================================== #
# 九、HTTP 接口
# =========================================================================== #
def test_api_index_lists_data(client):
    assert client.get("/").json()["endpoints"]["data"] == "/data"


def test_api_stats_and_rules(client):
    assert client.get("/data/stats").status_code == 200
    rules = client.get("/data/rules").json()
    assert len(rules["categories"]) == 9


def test_api_ingest_and_staging(client, wechat_db):
    dry = client.post("/data/ingest", json={"dry_run": True})
    assert dry.status_code == 200
    assert dry.json()["blocks_total"] == 7

    real = client.post("/data/ingest", json={"dry_run": False})
    assert real.status_code == 200
    assert real.json()["staged_new"] == 3

    listing = client.get("/data/staging", params={"status": "pending"})
    body = listing.json()
    assert body["count"] == 3
    assert body["counts"]["pending"] == 3


def test_api_ingest_falls_back_to_jsonl_corpus(client):
    """没配微信源时自动回退到演示语料（conftest 写好的 conversations.jsonl）。

    这是"离线可跑"的落点：新环境没微信库也能跑通整条管线。
    """
    response = client.post("/data/ingest", json={"dry_run": False})
    assert response.status_code == 200
    assert response.json()["raw_messages"] == 9
    assert response.json()["sessions"] == 2


def test_api_review_flow(client, wechat_db):
    client.post("/data/ingest", json={"dry_run": False})
    items = client.get("/data/staging", params={"status": "pending"}).json()["items"]
    staging_id = items[0]["staging_id"]

    approved = client.post(f"/data/staging/{staging_id}/review", json={"action": "approve", "reviewer": "主管A"})
    assert approved.status_code == 200
    assert approved.json()["item"]["status"] == "approved"

    detail = client.get(f"/data/staging/{staging_id}").json()
    assert detail["audits"][0]["action"] == "approve"

    # 非法流转 → 409（不是 400）
    conflict = client.post(f"/data/staging/{staging_id}/review", json={"action": "reject", "reviewer": "主管A"})
    assert conflict.status_code == 409


def test_api_export_flow(client, wechat_db):
    client.post("/data/ingest", json={"dry_run": False})
    for item in client.get("/data/staging", params={"status": "pending"}).json()["items"]:
        client.post(f"/data/staging/{item['staging_id']}/review", json={"action": "approve", "reviewer": "A"})
    exported = client.post("/data/export", json={"format": "sharegpt"})
    assert exported.status_code == 200
    assert exported.json()["rows"] == 2
    assert client.get("/data/exports").json()["count"] == 1
    assert client.post("/data/export", json={"format": "parquet"}).status_code == 422


def test_api_knowledge_and_feed(client, wechat_db):
    client.post("/data/ingest", json={"dry_run": False})
    for item in client.get("/data/staging", params={"status": "pending"}).json()["items"]:
        client.post(f"/data/staging/{item['staging_id']}/review", json={"action": "approve", "reviewer": "A"})
    built = client.post("/data/knowledge/build", json={})
    assert built.status_code == 200
    assert built.json()["chunks"] > 0

    search = client.get("/data/knowledge/search", params={"q": "公积金"})
    assert search.json()["count"] >= 1

    feed = client.get("/data/feed").json()
    assert feed["count"] == 2


def test_api_missing_staging_returns_404(client):
    assert client.get("/data/staging/not-exist").status_code == 404
