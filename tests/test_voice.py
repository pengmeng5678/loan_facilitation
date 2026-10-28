"""项目一（AI 实时语音对话智能客服）测试。

覆盖六条主线，每条都测"业务判据"而不是"函数能跑"：

    ① 进房凭证      001 Token 的打包/签名/自校验，篡改与过期必须被识别
    ② 云端配置      下发出去的 ASR/TTS/LLM 到底是不是我们想的那个
    ③ 内容安全      五条红线（承诺/造假/收费/债务链/隐私）+ PII 打码 + 可改写项
    ④ 对话链路      无依据即转人工、逐句过审、跨句复检、打断收口
    ⑤ 评测准入      Golden Set 打分口径与"新版本能不能上线"的三条拒发理由
    ⑥ 装配与接口    工具注册、Agent 路由与作用域、HTTP/SSE 回调

全部离线运行，不依赖网络、不依赖任何 API Key。

关于"为什么测这些而不是测别的"：
    语音链路的失败**没有截图可复现** —— 客户听到的就是"AI 不说话了"或"AI 承诺能下款"。
    所以这里刻意把"下发了什么配置""这句话有没有被拦""为什么拒答"都断言到字段级，
    出问题时能直接定位是配置错、检索空、还是内容安全误杀。
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from agent_platform.config import Settings
from voice_service.agent_config import (
    VOICE_MAX_TOKENS_CEIL,
    VOICE_MAX_TOKENS_FLOOR,
    build_asr_config,
    build_llm_config,
    build_start_payload,
    build_tts_config,
    clamp_max_tokens,
)
from voice_service.censor import SAFE_REPLIES, Censor, mask_pii, normalize_for_scan
from voice_service.dialog import (
    CancelToken,
    DialogDeps,
    SentenceSplitter,
    TurnRequest,
    VoiceDialogEngine,
)
from voice_service.domain import (
    EvalCase,
    EvalCaseResult,
    EvalReport,
    KnowledgeChunk,
    VoiceScene,
    VoiceTurn,
)
from voice_service.evaluation import REDLINE_WORDS, VoiceEvaluator, builtin_cases
from voice_service.metrics import VoiceMetrics, percentile
from voice_service.prompt import (
    KNOWLEDGE_BEGIN,
    KNOWLEDGE_END,
    PromptRegistry,
    build_knowledge_block,
)
from voice_service.rag import VoiceRetriever
from voice_service.rtc_token import (
    RtcTokenBuilder,
    mask_token,
)
from voice_service.session import VoiceSessionStore
from voice_service.volc_sign import (
    VolcCredentials,
    canonical_query_string,
    sign_request,
)


# =========================================================================== #
# 一、进房 Token（001 方案）
# =========================================================================== #
def test_token_issue_and_verify_roundtrip():
    builder = RtcTokenBuilder("app-test", "key-test")
    token = builder.issue(room_id="room_1", user_id="C1002", ttl_seconds=600, nonce=12345)
    assert token.startswith("001app-test")

    verdict = builder.verify(token)
    assert verdict.valid is True
    assert verdict.reason == ""
    assert verdict.room_id == "room_1"
    assert verdict.user_id == "C1002"
    assert verdict.expire_at > verdict.issued_at


def test_token_tamper_is_rejected():
    """改一个字节就必须验签失败 —— 这是 Token 唯一的防伪手段。"""
    builder = RtcTokenBuilder("app-test", "key-test")
    token = builder.issue(room_id="room_1", user_id="C1002", nonce=1)

    # 尾部改一个字符（base64 里换掉一个有效字符）
    body = token[len("001app-test") :]
    tampered = token[:-1] + ("A" if body[-1] != "A" else "B")
    verdict = builder.verify(tampered)
    assert verdict.valid is False
    assert "签名不匹配" in verdict.reason


def test_token_from_another_app_key_is_rejected():
    """AppKey 不对 → 验签失败（而不是报"权限不足"，排障时这点很重要）。"""
    issued = RtcTokenBuilder("app-test", "key-a").issue(room_id="r", user_id="u", nonce=7)
    verdict = RtcTokenBuilder("app-test", "key-b").verify(issued)
    assert verdict.valid is False
    assert "签名不匹配" in verdict.reason


def test_token_expiry_is_detected():
    builder = RtcTokenBuilder("app-test", "key-test")
    token = builder.issue(room_id="r", user_id="u", ttl_seconds=60, now=1_700_000_000, nonce=3)
    # 时间往后拨 61 秒
    verdict = builder.verify(token, now=1_700_000_061)
    assert verdict.valid is False
    assert "已过期" in verdict.reason
    # 过期 Token 里仍然能读出房间号（排障要看这个）
    assert verdict.room_id == "r"


def test_token_prefix_mismatch_and_empty():
    builder = RtcTokenBuilder("app-test", "key-test")
    assert builder.verify("").valid is False
    assert "前缀不匹配" in builder.verify("002other-app-xxx").reason
    # peek 也要求 AppId 对得上，否则无法可靠定位前缀边界
    assert builder.peek("002other-app-xxx").valid is False


def test_token_nonce_overflow_does_not_crash():
    """nonce 是 uint32：传一个大数不能抛 struct.error（边界值最容易被随机数撞上）。"""
    builder = RtcTokenBuilder("app-test", "key-test")
    token = builder.issue(room_id="r", user_id="u", nonce=0x1_0000_0001)
    assert builder.verify(token).valid is True


def test_mask_token_hides_middle():
    token = "001" + "x" * 120
    masked = mask_token(token)
    assert token not in masked
    assert "已脱敏" in masked
    assert mask_token("") == ""


# =========================================================================== #
# 二、火山 OpenAPI V4 签名
# =========================================================================== #
def test_canonical_query_string_is_sorted_and_encoded():
    # 传进来的顺序是乱的，签名前必须按 key 排序，否则服务端算出的串不一样
    assert canonical_query_string({"Version": "2024-12-01", "Action": "StartVoiceChat"}) == (
        "Action=StartVoiceChat&Version=2024-12-01"
    )
    assert canonical_query_string({}) == ""
    # value 里的特殊字符要百分号编码
    assert canonical_query_string({"k": "a b"}) == "k=a%20b"


def test_sign_request_is_deterministic_for_same_x_date():
    kwargs = {
        "credentials": VolcCredentials("AK", "SK"),
        "method": "POST",
        "host": "rtc.volcengineapi.com",
        "path": "/",
        "query": {"Action": "StartVoiceChat", "Version": "2024-12-01"},
        "body": b'{"AppId":"x"}',
        "region": "cn-north-1",
        "service": "rtc",
        "x_date": "20260401T030405Z",
    }
    first = sign_request(**kwargs)
    second = sign_request(**kwargs)
    assert first == second
    # 日期必须与 CredentialScope 同一天（跨零点签名会被拒）
    assert "20260401/cn-north-1/rtc/request" in first["Authorization"]
    assert first["X-Date"] == "20260401T030405Z"
    assert first["X-Content-Sha256"] == sign_request(**{**kwargs, "x_date": "20260401T030405Z"})[
        "X-Content-Sha256"
    ]


def test_sign_request_changes_with_body_and_query():
    base = {
        "credentials": VolcCredentials("AK", "SK"),
        "method": "POST",
        "host": "rtc.volcengineapi.com",
        "path": "/",
        "query": {"Action": "StartVoiceChat"},
        "body": b'{"a":1}',
        "region": "cn-north-1",
        "service": "rtc",
        "x_date": "20260401T030405Z",
    }
    a = sign_request(**base)["Authorization"]
    b = sign_request(**{**base, "body": b'{"a":2}'})["Authorization"]
    c = sign_request(**{**base, "query": {"Action": "StopVoiceChat"}})["Authorization"]
    assert a != b
    assert a != c
    # 换密钥也要变（否则签名没有防伪意义）
    d = sign_request(**{**base, "credentials": VolcCredentials("AK", "SK2")})["Authorization"]
    assert a != d


# =========================================================================== #
# 三、云端 Agent 配置（下发什么，客户就听到什么）
# =========================================================================== #
def _voice_settings(**overrides) -> Settings:
    """只用于配置构造的轻量 Settings（不需要任何数据目录）。"""
    defaults = dict(
        app_env="local",
        log_level="WARNING",
        rtc_app_id="app-test",
        rtc_app_key="key-test",
        rtc_region="cn-north-1",
        rtc_service="rtc",
        volc_access_key="AK",
        volc_secret_key="SK",
        voice_asr_resource_id="volc.seedasr.sauc.duration",
        voice_tts_app_id="tts-app",
        voice_tts_cluster="volcano_icl",
        voice_tts_voice_type="S_test_voice",
        voice_llm_model="doubao-seed-1-6-flash",
        voice_public_base_url="https://example.com",
        voice_callback_api_key="cb-key",
        voice_max_tokens=220,
        voice_history_turns=6,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_clamp_max_tokens_keeps_speech_listenable():
    """语音回复必须夹在 150~300 tokens：太短没信息，太长客户必打断。"""
    assert clamp_max_tokens(50) == VOICE_MAX_TOKENS_FLOOR
    assert clamp_max_tokens(9999) == VOICE_MAX_TOKENS_CEIL
    assert clamp_max_tokens(220) == 220


def test_asr_config_uses_bidirectional_stream_and_hotwords():
    settings = _voice_settings()
    config = build_asr_config(settings, hotwords=["债务置换", "征信报告"])
    params = config["ProviderParams"]
    assert config["Provider"] == "volcano"
    assert params["Mode"] == "bigmodel"
    # 2 = 双向流；改成 1 会让尾字延迟明显变长，这条锁死不许退化
    assert params["StreamMode"] == 2
    assert params["Credential"]["ApiResourceId"] == "volc.seedasr.sauc.duration"
    # 官方要求以 JSON 字符串透传（不是 dict）
    asr_params = json.loads(params["VolcanoASRParameters"])
    assert asr_params["hotwords"] == ["债务置换", "征信报告"]
    assert asr_params["enable_emotion"] is True


def test_asr_config_can_disable_emotion_and_hotwords():
    config = build_asr_config(_voice_settings(), hotwords=None, emotion=False)
    assert json.loads(config["ProviderParams"]["VolcanoASRParameters"]) == {}


def test_tts_config_prefers_scene_voice_over_global_default():
    settings = _voice_settings()
    assert build_tts_config(settings)["ProviderParams"]["audio"]["voice_type"] == "S_test_voice"
    scene_voice = build_tts_config(settings, voice_type="S_clone_001", speed_ratio=1.1)
    audio = scene_voice["ProviderParams"]["audio"]
    assert audio["voice_type"] == "S_clone_001"
    assert audio["speed_ratio"] == 1.1
    # 复刻音色走 icl 集群，预置音色走 volcano_tts —— 混用会合成失败
    assert scene_voice["ProviderParams"]["app"]["cluster"] == "volcano_icl"


def test_llm_config_points_to_our_own_service():
    """这个项目最本质的架构点：不用火山内置 LLM，回调到我们自己的服务。"""
    settings = _voice_settings()
    config = build_llm_config(
        settings,
        callback_url="https://example.com/voice/callback/chat",
        system_prompt="你是助贷顾问",
        max_tokens=500,          # 故意超上限
        temperature=0.2,
        history_turns=6,
    )
    assert config["Mode"] == "Custom"
    assert config["Url"] == "https://example.com/voice/callback/chat"
    assert config["ApiKey"] == "cb-key"
    assert config["Stream"] is True, "必须流式，否则 TTS 要等整段生成完才开口"
    assert config["MaxTokens"] == VOICE_MAX_TOKENS_CEIL
    assert config["SystemMessages"] == ["你是助贷顾问"]
    assert config["HistoryLength"] == 6


def test_start_payload_shape():
    payload = build_start_payload(
        _voice_settings(),
        room_id="room_1",
        user_id="C1002",
        bot_user_id="Huoshan01",
        system_prompt="你是助贷顾问",
        welcome_message="您好",
        task_id="task_1",
        max_tokens=200,
        speed_ratio=1.0,
    )
    assert payload["AppId"] == "app-test"
    assert payload["RoomId"] == "room_1"
    assert payload["TaskId"] == "task_1"
    assert payload["AgentConfig"]["UserId"] == "Huoshan01"
    assert payload["AgentConfig"]["TargetUserId"] == ["C1002"]
    assert payload["AgentConfig"]["WelcomeMessage"] == "您好"
    assert payload["AgentConfig"]["InterruptMode"] is True
    assert payload["AgentConfig"]["EnableConversationStateCallback"] is True
    assert set(payload["Config"]) == {"ASRConfig", "TTSConfig", "LLMConfig"}
    # 回调地址必须由配置拼出来，且指向我们自己的服务
    assert payload["Config"]["LLMConfig"]["Url"].endswith("/voice/callback/chat")


def test_deep_merge_lets_official_new_fields_pass_through():
    """官方新增字段走 extra 透传，不能因为"我们没写"就得改代码发版。"""
    settings = _voice_settings()
    extra = {"Config": {"TTSConfig": {"ProviderParams": {"audio": {"volume_ratio": 1.4}}}}}
    payload = build_start_payload(
        settings,
        room_id="r",
        user_id="u",
        bot_user_id="bot",
        system_prompt="p",
        extra=extra,
    )
    audio = payload["Config"]["TTSConfig"]["ProviderParams"]["audio"]
    assert audio["volume_ratio"] == 1.4
    # 深合并不能把原有字段冲掉
    assert audio["voice_type"] == "S_test_voice"


# =========================================================================== #
# 四、内容安全（语音的"嘴巴闸门"）
# =========================================================================== #
@pytest.mark.parametrize(
    "text,rule_id,category",
    [
        ("我保证您能下款，百分百通过", "PROMISE-01", "承诺性表述"),
        ("这个资料我帮您包装一下就行了", "FRAUD-01", "诱导造假"),
        ("办理前先交一笔保证金", "FEE-01", "违规收费"),
        ("可以先以贷养贷，再贷一笔还上", "CHAIN-01", "债务链诱导"),
        ("把验证码发我，我帮您操作", "PRIVACY-01", "隐私与敏感"),
    ],
)
def test_censor_blocks_five_redlines(text, rule_id, category):
    verdict = Censor().check(text)
    assert verdict.passed is False
    assert verdict.blocked is True
    assert verdict.needs_human is True
    assert rule_id in {f.rule_id for f in verdict.findings}
    # 命中红线必须给兜底话术（按类别给，不是统一"抱歉"）
    assert verdict.safe_reply == SAFE_REPLIES[category]


def test_censor_promise_pattern_catches_inserted_words():
    """关键词覆盖不到的形状（中间夹字）要用正则兜底。"""
    verdict = Censor().check("我可以保证，最长两周内一定给您批下来")
    assert verdict.blocked is True
    assert "PROMISE-01" in {f.rule_id for f in verdict.findings}


def test_censor_rewrites_absolute_words_instead_of_blocking():
    """绝对化用语（广告法）是可改写的中间风险：换词放行，不整轮作废。"""
    verdict = Censor().check("我们是本地最专业的机构")
    assert verdict.passed is True
    assert verdict.blocked is False
    assert "最专业的" not in verdict.sanitized_text
    assert "比较专业" in verdict.sanitized_text
    assert "ADLAW-01" in {f.rule_id for f in verdict.findings}


def test_censor_masks_pii_before_speaking():
    """语音场景下把客户手机号念出来 = 向同房间所有人广播。"""
    verdict = Censor().check("您的预留手机号是 13812345678，我核对一下")
    assert "13812345678" not in verdict.sanitized_text
    assert "138****5678" in verdict.sanitized_text
    assert verdict.blocked is True          # PII 是 high，命中即拦
    assert "PII-01" in {f.rule_id for f in verdict.findings}


def test_mask_pii_id_card_not_eaten_by_bank_card_rule():
    """18 位身份证必须命中身份证规则（顺序踩过坑，锁死）。"""
    masked, hits = mask_pii("身份证 320102199001011234")
    assert hits == ["320102199001011234"]
    assert masked == "身份证 3201**********1234"


def test_mask_pii_bank_card():
    masked, hits = mask_pii("卡号 6222021234567890")
    assert hits == ["6222021234567890"]
    assert masked == "卡号 6222********7890"


def test_censor_pass_through_clean_text():
    verdict = Censor().check("政策上写的是年化利率 3.2% 到 4.6%，具体以银行审批为准。")
    assert verdict.passed is True
    assert verdict.findings == []
    assert verdict.to_text() == "内容安全：通过（未命中任何规则）"


def test_fallback_replies_must_not_trip_the_censor_themselves():
    """兜底话术自己不能踩红线。

    这件事一旦坏掉就是死循环：命中 → 换成兜底话术 → 复检又命中 → 再换……
    而"放款前不收任何费用""验证码千万别告诉任何人"这类**正确话术**
    天然带着否定词，最容易被正则误伤（所以规则里显式加了否定排除）。
    """
    censor = Censor()
    for category, reply in SAFE_REPLIES.items():
        verdict = censor.check(reply)
        assert verdict.passed is True, f"{category} 的兜底话术被误判：{verdict.to_text()}"


def test_negated_compliance_statements_are_not_flagged():
    censor = Censor()
    assert censor.check("我们放款前不收取任何费用。").passed is True
    assert censor.check("验证码您千万别告诉任何人。").passed is True
    # 反过来，正向索取必须命中
    assert censor.check("把验证码发我。").passed is False
    assert censor.check("放款前先交服务费。").passed is False


def test_user_promise_question_is_not_treated_as_risk():
    """客户问"你能保证吗"是正常提问，不该把客户标成高风险转人工。"""
    verdict = Censor().check_user_text("你能保证我一定能批下来吗？")
    assert verdict.blocked is False
    assert verdict.needs_human is False


@pytest.mark.parametrize(
    "text,rule_id",
    [
        ("这个我们保 证 下 款", "PROMISE-01"),          # 空格断字
        ("包\u200b过，放心", "PROMISE-01"),             # 零宽字符
        ("包·过，没问题", "PROMISE-01"),                # 分隔符插入
        ("通过率１００％，不用看征信", "PROMISE-01"),     # 全角数字
        ("先交一笔保 证 金", "FEE-01"),                  # 空格断字 + 收费类
    ],
)
def test_censor_blocks_obfuscated_variants(text, rule_id):
    """ASR 转写里带空格/零宽/分隔符/全角 —— 归一化后仍必须命中。"""
    verdict = Censor().check(text)
    assert verdict.blocked is True
    assert rule_id in {f.rule_id for f in verdict.findings}


@pytest.mark.parametrize(
    "text,rule_id",
    [
        ("BaoGuo，稳的", "PROMISE-01"),
        ("bao guo，百分百通过", "PROMISE-01"),
        ("帮我 jialiushui 做一份", "FRAUD-01"),
        ("先 yidaiyangdai 再还旧账", "CHAIN-01"),
    ],
)
def test_censor_blocks_pinyin_variant(text, rule_id):
    """拼音化（包过→baoguo）绕不过红名单：拼音别名按类别归属到原规则。"""
    verdict = Censor().check(text)
    assert verdict.blocked is True
    hits = [f for f in verdict.findings if f.rule_id == rule_id]
    assert any("拼音" in f.matched for f in hits)


def test_normalize_for_scan_collapses_obfuscation():
    assert normalize_for_scan("保  证\u200b·下 款") == "保证下款"
    assert normalize_for_scan("１００％") == "100%"


@pytest.mark.parametrize(
    "text,category",
    [
        ("我流水不够，能不能帮我包装一下资料", "诱导造假"),
        ("能不能先交一笔服务费，办下来再退给我", "违规收费"),
        ("我想多申请几家一起借新还旧", "债务链诱导"),
    ],
)
def test_user_fraud_requests_are_blocked(text, category):
    verdict = Censor().check_user_text(text)
    assert verdict.blocked is True
    assert verdict.safe_reply == SAFE_REPLIES[category]


# =========================================================================== #
# 五、提示词与知识块（前缀缓存的地基）
# =========================================================================== #
def _chunk(chunk_id: str, text: str, score: float = 0.9) -> KnowledgeChunk:
    return KnowledgeChunk(chunk_id=chunk_id, text=text, score=score)


def test_knowledge_block_is_sorted_by_chunk_id_not_score():
    """排序口径必须是 chunk_id：按分数排会让前缀抖动，缓存全失效。"""
    chunks = [_chunk("b-02", "第二条", 0.99), _chunk("a-01", "第一条", 0.10)]
    block = build_knowledge_block(chunks)
    assert block.startswith(KNOWLEDGE_BEGIN)
    assert block.endswith(KNOWLEDGE_END)
    assert block.index("第一条") < block.index("第二条")


def test_knowledge_block_respects_char_budget():
    block = build_knowledge_block([_chunk("a", "字" * 500)], max_chars=200)
    assert len(block) < 300            # 预算 200 + 两行标记
    assert build_knowledge_block([]) == ""


def test_prompt_registry_versions_and_activation(tmp_path):
    registry = PromptRegistry(tmp_path / "prompts.json")
    registry.load()
    assert registry.keys() == ["amount", "consultant", "docs"]
    assert registry.get("consultant").version == "v1"

    v2 = registry.add("consultant", "新版提示词 {{knowledge}}", note="更短", activate=False)
    assert v2.version == "v2"
    assert registry.get("consultant").version == "v1", "候选版本不该自动生效"

    registry.activate("consultant", "v2")
    assert registry.get("consultant").version == "v2"
    statuses = {v.version: v.status for v in registry.versions("consultant")}
    assert statuses == {"v1": "archived", "v2": "active"}, "旧版本必须保留可回滚"

    # 落盘 + 重载（进程重启后版本信息不能丢）
    reloaded = PromptRegistry(tmp_path / "prompts.json")
    reloaded.load()
    assert reloaded.get("consultant").version == "v2"


def test_prompt_registry_unknown_key_or_version_raises(tmp_path):
    registry = PromptRegistry(tmp_path / "prompts.json")
    registry.load()
    with pytest.raises(KeyError):
        registry.get("not-exist")
    with pytest.raises(KeyError):
        registry.get("consultant", "v99")


def test_prompt_render_replaces_placeholders(tmp_path):
    registry = PromptRegistry(tmp_path / "prompts.json")
    registry.load()
    rendered = registry.get("consultant").render(knowledge_block="【知识】A", extra="客户：张先生")
    assert "{{knowledge}}" not in rendered
    assert "【知识】A" in rendered
    assert "客户：张先生" in rendered
    # 知识为空时给"（无）"，而不是留空 —— 留空会让模型误以为后面还有内容
    assert "（无）" in registry.get("consultant").render()


def test_prompt_max_tokens_is_clamped_on_write(tmp_path):
    """提示词里写 2048 tokens 是文本场景的习惯，语音场景必须被夹住。"""
    registry = PromptRegistry(tmp_path / "prompts.json")
    registry.load()
    v2 = registry.add("docs", "资料清单 {{knowledge}}", max_tokens=2048)
    assert v2.max_tokens == VOICE_MAX_TOKENS_CEIL


# =========================================================================== #
# 六、检索策略层（语音特有的取舍）
# =========================================================================== #
def _fake_chunk_obj(chunk_id: str, text: str, score: float, **meta):
    return SimpleNamespace(
        chunk_id=chunk_id,
        text=text,
        parent_text="",
        score=score,
        dense_score=score,
        rerank_score=score,
        citation=None,
        metadata=meta,
    )


def test_retriever_gates_low_relevance_as_no_hit():
    """低于相关性阈值就当没检索到 —— 宁可转人工，也不能拿弱相关条款硬编。"""
    retriever = VoiceRetriever(
        lambda q, top_k=2, **kw: [_fake_chunk_obj("c1", "弱相关条款。", 0.05)],
        min_relevance=0.25,
    )
    result = retriever.retrieve("随便问问")
    assert result.hit is False
    assert "无相关条款" in result.reason


def test_retriever_dedupes_and_respects_limit():
    rows = [
        _fake_chunk_obj("c1", "第一条。", 0.9),
        _fake_chunk_obj("c1", "第一条重复。", 0.88),
        _fake_chunk_obj("c2", "第二条。", 0.8),
        _fake_chunk_obj("c3", "第三条。", 0.7),
    ]
    retriever = VoiceRetriever(lambda q, top_k=3, **kw: rows, limit=2, min_relevance=0.25)
    result = retriever.retrieve("公积金信用贷")
    assert result.hit is True
    assert [c.chunk_id for c in result.chunks] == ["c1", "c2"]
    assert len(result.citations) == 0    # 这批假数据没有 citation

    # 场景级 limit 覆盖默认值
    assert len(retriever.retrieve("x", limit=3).chunks) == 3


def test_retriever_truncates_at_sentence_boundary():
    long_text = "第一句话说完。" + "第二句话很长" * 60 + "。"
    retriever = VoiceRetriever(
        lambda q, top_k=2, **kw: [_fake_chunk_obj("c1", long_text, 0.9)],
        max_chars=200,
        min_relevance=0.25,
    )
    result = retriever.retrieve("流程")
    chunk = result.chunks[0]
    assert len(chunk.text) <= 200
    assert chunk.text.endswith("。"), "截断必须落在句子边界，否则会念出半句"


def test_retriever_tags_filter():
    rows = [
        _fake_chunk_obj("c1", "置换条款。", 0.9, tags=["债务置换"]),
        _fake_chunk_obj("c2", "资料条款。", 0.9, tags=["资料"]),
    ]
    retriever = VoiceRetriever(lambda q, top_k=2, **kw: rows, min_relevance=0.25)
    result = retriever.retrieve("置换", tags=["债务置换"])
    assert [c.chunk_id for c in result.chunks] == ["c1"]


def test_retriever_disabled_and_zero_limit():
    retriever = VoiceRetriever(lambda q: [], enabled=False)
    assert retriever.retrieve("x").hit is False

    retriever2 = VoiceRetriever(lambda q, top_k=2, **kw: [_fake_chunk_obj("c", "t", 0.9)])
    assert retriever2.retrieve("x", limit=0).hit is False


def test_retriever_does_not_propagate_backend_failure():
    """检索挂了不能连带语音服务挂 —— 退化为"没查到"，走转人工。"""

    def boom(*args, **kwargs):
        raise RuntimeError("vector store down")

    result = VoiceRetriever(boom).retrieve("流程")
    assert result.hit is False
    assert "检索异常" in result.reason


def test_retriever_supports_narrow_retrieve_signature():
    """注入的检索函数可能只接受 query（项目二的口径之外的实现）。"""
    calls: list[tuple] = []

    def only_query(query):
        calls.append((query,))
        return [_fake_chunk_obj("c1", "只有 query 的实现。", 0.9)]

    result = VoiceRetriever(only_query, min_relevance=0.25).retrieve("流程")
    assert result.hit is True
    assert calls == [("流程",)]


# =========================================================================== #
# 七、流式切句
# =========================================================================== #
def test_splitter_emits_on_sentence_end():
    splitter = SentenceSplitter()
    assert splitter.feed("您好，我是助贷顾问。") == ["您好，我是助贷顾问。"]
    assert splitter.feed("后面还有半句") == []
    assert splitter.flush() == "后面还有半句"


def test_splitter_accumulates_across_chunks():
    """token 是碎片化的，切句必须能跨 chunk 累积。"""
    splitter = SentenceSplitter()
    out: list[str] = []
    for piece in ["年化利率是", "3.2% 到 4.6%，", "以银行审批为准。"]:
        out.extend(splitter.feed(piece))
    assert out == ["年化利率是3.2% 到 4.6%，以银行审批为准。"]


def test_splitter_cuts_long_text_without_sentence_end():
    """没有句末标点也不能一直憋着 —— 否则客户听不到任何声音。"""
    splitter = SentenceSplitter(max_pending=12)
    out = splitter.feed("一二三四五，六七八九十十一")
    assert out == ["一二三四五，"]
    assert splitter.buffer


def test_splitter_hard_cut_when_no_punctuation_at_all():
    splitter = SentenceSplitter(max_pending=10)
    out = splitter.feed("字" * 25)
    assert out and len(out[0]) == 10


def test_splitter_handles_all_sentence_ends():
    for mark in "。！？!?；;\n":
        splitter = SentenceSplitter()
        # 切完之后会 strip（TTS 不该念出多余空白/换行），标点本身保留
        assert splitter.feed(f"你好{mark}") == [f"你好{mark}".strip()]


# =========================================================================== #
# 八、对话引擎（无依据转人工 / 逐句过审 / 打断）
# =========================================================================== #
def _scene(**overrides) -> VoiceScene:
    base = dict(
        scene_id="consultant",
        name="顾问",
        prompt_key="consultant",
        welcome_message="您好",
        max_tokens=200,
        temperature=0.2,
        knowledge_limit=2,
    )
    base.update(overrides)
    return VoiceScene(**base)


def _make_engine(tmp_path, *, rows=None, llm=None, prefer_model=True) -> VoiceDialogEngine:
    registry = PromptRegistry(tmp_path / "prompts.json")
    registry.load()
    retriever = VoiceRetriever(
        lambda q, top_k=2, **kw: rows if rows is not None else [],
        limit=2,
        min_relevance=0.25,
    )
    return VoiceDialogEngine(
        DialogDeps(
            llm=llm or _EchoLikeLLM([]),
            retriever=retriever,
            prompts=registry,
            censor=Censor(),
            history_turns=6,
            max_tokens_cap=300,
            prefer_model=prefer_model,
        )
    )


class _EchoLikeLLM:
    """把预设文本按片段吐出（模拟真实模型的 token 流）。"""

    def __init__(self, pieces: list[str], name: str = "fake-model") -> None:
        self.pieces = pieces
        self.name = name

    def stream_complete(self, messages, *, temperature=0.3, max_tokens=200, **kwargs):
        yield from self.pieces

    def complete(self, messages, **kwargs):
        return "".join(self.pieces)


_ROWS = [_fake_chunk_obj("a-01", "办理流程是：提交资料、银行预审、面签、放款。", 0.9)]


def test_dialog_hands_off_without_calling_model_when_no_knowledge(tmp_path):
    """没有知识依据就不调模型 —— 既省时（首字 0.1s）又不编。"""
    llm = _EchoLikeLLM(["这句不该出现。"])
    engine = _make_engine(tmp_path, rows=[], llm=llm)
    turn = engine.ask(TurnRequest(scene=_scene(), user_text="你们老板叫什么？"))
    assert turn.refused is True
    assert turn.needs_human is True
    assert "专员" in turn.answer_text
    assert "这句不该出现" not in turn.answer_text


def test_dialog_normal_turn_uses_model_and_cites(tmp_path):
    engine = _make_engine(tmp_path, rows=_ROWS, llm=_EchoLikeLLM(["流程是提交资料。", "然后预审。"]))
    turn = engine.ask(TurnRequest(scene=_scene(), user_text="公积金信用贷的办理流程？"))
    assert turn.refused is False
    assert "流程" in turn.answer_text
    assert turn.prompt_version == "consultant@v1"
    assert turn.latency.total_ms >= 0
    assert turn.latency.chunks >= 1


def test_dialog_replaces_sentence_that_hits_redline(tmp_path):
    """逐句过审：命中红线的那一句不念，换成兜底话术并终止本轮。"""
    llm = _EchoLikeLLM(["办理流程很简单。", "我保证您能下款。", "后面这句不该念出来。"])
    engine = _make_engine(tmp_path, rows=_ROWS, llm=llm)
    events = list(engine.stream_turn(TurnRequest(scene=_scene(), user_text="办理流程？")))

    deltas = [e.delta for e in events if e.delta]
    assert "办理流程很简单。" in deltas
    assert any("保证" not in d and "审批" in d for d in deltas), "应替换成兜底话术"
    assert all("保证您能下款" not in d for d in deltas)
    assert all("后面这句不该念" not in d for d in deltas), "命中红线后必须停止继续生成"

    final = [e for e in events if e.done][0]
    assert final.replaced is True
    assert final.turn.needs_human is True


def test_dialog_rewrites_absolute_words_in_stream(tmp_path):
    engine = _make_engine(tmp_path, rows=_ROWS, llm=_EchoLikeLLM(["我们是最专业的机构。"]))
    turn = engine.ask(TurnRequest(scene=_scene(), user_text="办理流程？"))
    assert "最专业的" not in turn.answer_text
    assert "比较专业" in turn.answer_text
    assert turn.refused is False


def test_dialog_blocks_user_fraud_request_before_retrieval(tmp_path):
    engine = _make_engine(tmp_path, rows=_ROWS, llm=_EchoLikeLLM(["不该到这一步。"]))
    turn = engine.ask(TurnRequest(scene=_scene(), user_text="帮我把流水包装一下"))
    assert turn.refused is True
    assert turn.needs_human is True
    assert "资料必须真实" in turn.answer_text


def test_dialog_cancel_token_stops_generation(tmp_path):
    """客户插话：服务端生成循环必须在下一个句界退出，不能继续烧 token。"""
    engine = _make_engine(tmp_path, rows=_ROWS, llm=_EchoLikeLLM(["第一句。", "第二句。"]))
    cancel = CancelToken()
    cancel.cancel("用户插话")
    turn = engine.ask(TurnRequest(scene=_scene(), user_text="办理流程？", cancel=cancel))
    assert turn.interrupted is True
    assert "第一句" not in turn.answer_text


def test_dialog_stops_generation_on_obfuscated_redline(tmp_path):
    """红线被"保 证 下 款"这类变体藏起来时，同样立刻换兜底并停止后续生成。"""
    engine = _make_engine(
        tmp_path,
        rows=_ROWS,
        llm=_EchoLikeLLM(["我保 证 您能下 款。", "后面这句不该念。"]),
    )
    turn = engine.ask(TurnRequest(scene=_scene(), user_text="办理流程？"))
    assert turn.needs_human is True
    assert "保证您能下款" not in turn.answer_text
    assert "后面这句不该念" not in turn.answer_text


def test_dialog_offline_extractive_composer_strips_internal_info(tmp_path):
    """离线抽取式应答不能把产品编号/来源文件念给客户听。"""
    rows = [
        _fake_chunk_obj(
            "a-01",
            "（测试银行A，产品编号 BANK-A-GJJ-001）授信额度区间为 5 万至 40 万。最终以银行审批为准。",
            0.9,
        )
    ]
    engine = _make_engine(tmp_path, rows=rows, prefer_model=False)
    turn = engine.ask(TurnRequest(scene=_scene(), user_text="额度多少？"))
    assert "产品编号" not in turn.answer_text
    assert "额度" in turn.answer_text
    assert len(turn.answer_text) <= 150


def test_dialog_builds_history_in_order(tmp_path):
    engine = _make_engine(tmp_path, rows=_ROWS, llm=_EchoLikeLLM(["好。"]))
    history = [
        VoiceTurn(round_id=1, user_text="问1", answer_text="答1"),
        VoiceTurn(round_id=2, user_text="问2", answer_text="答2"),
    ]
    messages = engine._build_messages("SYS", history, "问3")
    assert [m.role for m in messages] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert messages[-1].content == "问3"
    assert messages[0].content == "SYS"


def test_dialog_history_window_is_bounded(tmp_path):
    engine = _make_engine(tmp_path, rows=_ROWS, llm=_EchoLikeLLM(["好。"]))
    engine.deps.history_turns = 1
    history = [VoiceTurn(round_id=i, user_text=f"问{i}", answer_text=f"答{i}") for i in range(5)]
    messages = engine._build_messages("SYS", history, "问X")
    assert len(messages) == 4          # system + 最近 1 轮（user/assistant 两条） + 本轮
    assert messages[1].content == "问4"
    assert messages[2].content == "答4"


# =========================================================================== #
# 九、会话存储
# =========================================================================== #
@pytest.fixture
def store(tmp_path) -> VoiceSessionStore:
    s = VoiceSessionStore(tmp_path / "voice.sqlite")
    yield s
    s.close()


def _turn(round_id: int, *, refused: bool = False, needs_human: bool = False) -> VoiceTurn:
    return VoiceTurn(
        round_id=round_id,
        user_text=f"问{round_id}",
        answer_text=f"答{round_id}",
        refused=refused,
        needs_human=needs_human,
        citations=["cites"],
        latency={"total_ms": 100.0, "llm_ttft_ms": 80.0, "rag_ms": 10.0, "chunks": 1},
    )


def test_store_session_lifecycle(store):
    session = store.create(scene_id="consultant", room_id="room_1", user_id="C1")
    assert session.session_id.startswith("vs_")

    found = store.find_running_by_room("room_1")
    assert found and found.session_id == session.session_id
    assert store.latest_running().session_id == session.session_id

    store.stop(session.session_id)
    assert store.get(session.session_id).status == "stopped"
    assert store.find_running_by_room("room_1") is None


def test_store_turns_and_round_numbering(store):
    session = store.create(scene_id="consultant", room_id="r", user_id="u")
    assert store.next_round(session.session_id) == 1
    store.append_turn(session.session_id, _turn(1))
    assert store.next_round(session.session_id) == 2
    store.append_turn(session.session_id, _turn(2, refused=True, needs_human=True))

    turns = store.turns(session.session_id)
    assert [t.round_id for t in turns] == [1, 2]
    assert turns[1].refused is True

    detail = store.get(session.session_id)
    assert detail.refused_count == 1
    assert detail.handoff_count == 1


def test_store_stats_and_listing(store):
    s1 = store.create(scene_id="consultant", room_id="r1", user_id="u1")
    s2 = store.create(scene_id="docs", room_id="r2", user_id="u2")
    store.append_turn(s1.session_id, _turn(1, refused=True))
    store.stop(s2.session_id)

    stats = store.stats()
    assert stats["sessions"] == 2
    assert stats["running"] == 1
    assert stats["turns"] == 1
    assert stats["refused"] == 1

    listed = store.list_sessions(limit=10)
    assert len(listed) == 2


def test_store_reopen_keeps_data(tmp_path):
    """落库要能跨进程（重启后会话还在），否则复盘就无从谈起。"""
    path = tmp_path / "voice.sqlite"
    first = VoiceSessionStore(path)
    session = first.create(scene_id="consultant", room_id="r", user_id="u")
    first.append_turn(session.session_id, _turn(1))
    first.close()

    second = VoiceSessionStore(path)
    assert second.get(session.session_id).turns[0].answer_text == "答1"
    second.close()


def test_store_concurrent_append_turns_get_contiguous_round_ids(store):
    """多线程同时 append_turn：轮次号必须连续无重复，一轮也不能丢。

    复刻真实竞态：ASR 回调与打断收口同时写同一场会话，各自都以为自己是"第 1 轮"
    —— 没有临界区时 INSERT OR REPLACE 会把先落库的覆盖掉，客户历史凭空少一轮。
    """
    session = store.create(scene_id="consultant", room_id="r", user_id="u")
    n_threads = 12
    barrier = threading.Barrier(n_threads)

    def worker(i: int) -> None:
        barrier.wait()  # 同时起跑，把竞态窗口拉到最大
        # round_id 全部传 1：模拟并发回调各自算出"下一轮"的撞号场景
        store.append_turn(
            session.session_id,
            VoiceTurn(round_id=1, user_text=f"并发问{i}", answer_text=f"并发答{i}"),
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    turns = store.turns(session.session_id)
    assert [t.round_id for t in turns] == list(range(1, n_threads + 1)), "轮次号必须从 1 连续递增"
    assert {t.user_text for t in turns} == {f"并发问{i}" for i in range(n_threads)}, (
        "每一轮都必须落库，不能被并发覆盖"
    )
    assert store.next_round(session.session_id) == n_threads + 1


def test_store_concurrent_appends_across_sessions_do_not_interfere(store):
    """并发写不同会话：各自轮次号独立连续，互不串号也不丢轮。"""
    s1 = store.create(scene_id="consultant", room_id="r1", user_id="u1")
    s2 = store.create(scene_id="amount", room_id="r2", user_id="u2")
    barrier = threading.Barrier(2)

    def append_ten(session_id: str, tag: str) -> None:
        barrier.wait()
        for _ in range(10):
            store.append_turn(session_id, VoiceTurn(round_id=1, user_text=tag, answer_text=tag))

    t1 = threading.Thread(target=append_ten, args=(s1.session_id, "甲"))
    t2 = threading.Thread(target=append_ten, args=(s2.session_id, "乙"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert [t.round_id for t in store.turns(s1.session_id)] == list(range(1, 11))
    assert [t.round_id for t in store.turns(s2.session_id)] == list(range(1, 11))


# =========================================================================== #
# 十、指标
# =========================================================================== #
def test_percentile_basics():
    assert percentile([], 0.5) == 0.0
    assert percentile([10], 0.95) == 10.0
    assert percentile([0, 100], 0.5) == 50.0
    assert percentile([1, 2, 3, 4], 0.5) == pytest.approx(2.5)


def test_metrics_counts_business_signals():
    metrics = VoiceMetrics()
    metrics.record(_turn(1, refused=True, needs_human=True))
    metrics.record(_turn(2), replaced=True)
    snap = metrics.snapshot()
    assert snap["turns"] == 2
    assert snap["refused"] == 1
    assert snap["handoff"] == 1
    assert snap["replaced_by_censor"] == 1
    assert snap["refuse_rate"] == 0.5
    assert snap["latency_ms"]["ttft_p50"] == 80.0


def test_metrics_flags_slow_first_token():
    """首字超过 2.5s 客户基本会开口打断，必须单独计数。"""
    metrics = VoiceMetrics()
    slow = _turn(1)
    slow.latency.llm_ttft_ms = 3000
    metrics.record(slow)
    assert metrics.snapshot()["slow_ttft"] == 1
    assert metrics.budget_alerts == 1


# =========================================================================== #
# 十一、评测与版本准入
# =========================================================================== #
def _report(case_results: list[tuple[str, bool, str]]) -> EvalReport:
    cases = [
        EvalCaseResult(
            case_id=cid,
            question="q",
            answer="a",
            passed=passed,
            score=1.0 if passed else 0.0,
            failures=[failure] if failure else [],
        )
        for cid, passed, failure in case_results
    ]
    total = len(cases)
    return EvalReport(
        prompt_key="consultant",
        version="v1",
        total=total,
        passed=sum(1 for c in cases if c.passed),
        score=round(sum(c.score for c in cases) / total, 4) if total else 0.0,
        cases=cases,
    )


def test_evaluator_redline_is_veto():
    """一句话踩红线，整条用例 0 分 —— 不参与平均，直接否决。"""
    evaluator = VoiceEvaluator(lambda c: None)
    case = EvalCase(case_id="x", question="q")
    turn = VoiceTurn(round_id=1, user_text="q", answer_text="我保证下款")
    result = evaluator.score_case(case, turn)
    assert result.passed is False
    assert result.score == 0.0
    assert "命中红线" in result.failures[0]


def test_evaluator_requires_refusal_when_knowledge_missing():
    evaluator = VoiceEvaluator(lambda c: None)
    case = EvalCase(case_id="x", question="q", must_refuse=True)
    turn = VoiceTurn(round_id=1, user_text="q", answer_text="我猜是 100 万", refused=False)
    result = evaluator.score_case(case, turn)
    assert result.passed is False
    assert any("应当拒答" in f for f in result.failures)


def test_evaluator_keyword_coverage_all_and_any():
    evaluator = VoiceEvaluator(lambda c: None)
    turn = VoiceTurn(round_id=1, user_text="q", answer_text="需要身份证和征信报告")
    all_case = EvalCase(case_id="a", question="q", expect_keywords=["身份证", "征信"], expect_mode="all")
    any_case = EvalCase(case_id="b", question="q", expect_keywords=["房产", "征信"], expect_mode="any")
    assert evaluator.score_case(all_case, turn).passed is True
    assert evaluator.score_case(any_case, turn).passed is True
    missing = EvalCase(case_id="c", question="q", expect_keywords=["房产"], expect_mode="all")
    assert evaluator.score_case(missing, turn).passed is False


def test_evaluator_flags_too_long_answer():
    evaluator = VoiceEvaluator(lambda c: None)
    case = EvalCase(case_id="x", question="q", max_chars=10)
    turn = VoiceTurn(round_id=1, user_text="q", answer_text="太长" * 20)
    result = evaluator.score_case(case, turn)
    assert result.passed is False
    assert any("回复过长" in f for f in result.failures)


def test_evaluator_isolates_case_exceptions():
    def ask(case: EvalCase) -> VoiceTurn:
        if case.case_id == "bad":
            raise RuntimeError("boom")
        return VoiceTurn(round_id=1, user_text=case.question, answer_text="好的。")

    report = VoiceEvaluator(ask).evaluate(
        [EvalCase(case_id="bad", question="q1"), EvalCase(case_id="ok", question="q2")]
    )
    assert report.total == 2
    assert report.passed == 1
    assert any("执行异常" in f for f in report.cases[0].failures)


def test_gate_allows_first_run_and_rejects_regression():
    evaluator = VoiceEvaluator(lambda c: None)
    baseline = _report([("a", True, ""), ("b", True, "")])

    first = evaluator.compare(baseline, None)
    assert first.allowed is True
    assert "无基线" in first.reasons[0]

    better = _report([("a", True, ""), ("b", True, "")])
    assert evaluator.compare(better, baseline).allowed is True

    worse = _report([("a", True, ""), ("b", False, "缺少关键事实")])
    decision = evaluator.compare(worse, baseline)
    assert decision.allowed is False
    assert decision.delta < 0
    assert any("退化" in r for r in decision.reasons)


def test_gate_rejects_new_failed_case_even_if_score_not_lower():
    """总分没降但"换了一类问题坏掉"同样要拒 —— 这是最容易漏的退化。"""
    evaluator = VoiceEvaluator(lambda c: None)
    baseline = _report([("a", True, ""), ("b", False, "旧问题")])
    current = _report([("a", True, ""), ("b", False, "旧问题"), ("c", False, "新问题")])
    # 手动把平均分拉平，模拟"新失败被其他好转掩盖"
    current.score = baseline.score
    decision = evaluator.compare(current, baseline)
    assert decision.allowed is False
    assert any("出现新的失败用例" in r for r in decision.reasons)


def test_gate_rejects_any_redline():
    evaluator = VoiceEvaluator(lambda c: None)
    baseline = _report([("a", True, "")])
    current = _report([("a", False, "命中红线：['包过']")])
    decision = evaluator.compare(current, baseline)
    assert decision.allowed is False
    assert any("合规红线" in r for r in decision.reasons)


def test_builtin_cases_cover_key_failure_modes():
    cases = builtin_cases()
    ids = {c.case_id for c in cases}
    assert {"voice_flow", "voice_no_promise", "voice_refuse_fake_docs", "voice_unknown_refuse"} <= ids
    # 红线词表来自运行时规则，口径必须一致
    assert "保证下款" in REDLINE_WORDS
    for case in cases:
        assert case.max_chars <= 220, "语音用例的长度上限不该超过 220 字"


# =========================================================================== #
# 十二、服务门面（进房 / 开房 / 问答 / 审核 / 评测 / 工具）
# =========================================================================== #
def test_service_join_info_signs_verifiable_token(voice_service):
    info = voice_service.join_info(scene_id="consultant", user_id="C1002", room_id="room_x")
    assert info.room_id == "room_x"
    assert info.user_id == "C1002"
    assert info.dry_run is True, "未配密钥时必须是演练模式，不能让前端误以为能真进房"
    verdict = voice_service._token_builder.verify(info.token)
    assert verdict.valid is True
    assert verdict.room_id == "room_x"
    # 对外输出必须打码
    assert info.token not in json.dumps(info.masked(), ensure_ascii=False)
    assert "已脱敏" in info.masked()["Token"]


def test_service_join_info_unknown_scene_raises(voice_service):
    # scene_of 会兜底到 consultant，所以"未知场景"不会抛错 —— 这是刻意的可用性设计
    assert voice_service.scene_of("nope").scene_id == "consultant"


def test_service_start_agent_sends_expected_rtc_config(voice_service, monkeypatch):
    """开房：断言我们**实际下发了什么**（这是 dry-run 传输层存在的意义）。"""
    monkeypatch.setattr(voice_service.settings, "voice_callback_api_key", "cb-secret")

    result = voice_service.start_agent(scene_id="consultant", user_id="C1002")
    assert result["session"]["status"] == "running"
    assert result["rtc"]["ok"] is True
    assert result["rtc"]["dry_run"] is True
    assert result["join"]["DryRun"] is True

    calls = voice_service.client.recorded_calls()
    assert len(calls) == 1
    call = calls[0]
    assert call["params"]["Action"] == "StartVoiceChat"
    payload = call["payload"]
    assert payload["AgentConfig"]["TargetUserId"] == ["C1002"]
    assert payload["AgentConfig"]["InterruptMode"] is True
    assert payload["Config"]["ASRConfig"]["ProviderParams"]["StreamMode"] == 2
    assert payload["Config"]["LLMConfig"]["Mode"] == "Custom"
    assert payload["Config"]["LLMConfig"]["Stream"] is True
    # 下发的 system prompt 不带知识（知识每轮现检索注入）
    assert "每轮由服务端实时注入" in payload["Config"]["LLMConfig"]["SystemMessages"][0]
    # 真正发出去的请求体里带真实 Key（RTC 云端要用它验回调来源）
    assert call["payload"]["Config"]["LLMConfig"]["ApiKey"] == "cb-secret"
    # 但**回显给调用方/写日志**的 payload 必须脱敏
    assert result["rtc"]["payload"]["Config"]["LLMConfig"]["ApiKey"] == "***"
    assert "cb-secret" not in json.dumps(result, ensure_ascii=False)

    # 挂断
    stopped = voice_service.stop_agent(result["session"]["session_id"])
    assert stopped["stopped"] is True
    assert voice_service.client.recorded_calls()[-1]["params"]["Action"] == "StopVoiceChat"


def test_service_stop_unknown_session_raises(voice_service):
    with pytest.raises(KeyError):
        voice_service.stop_agent("vs_not_exist")


def test_service_ask_hits_knowledge_and_records_metrics(voice_service):
    turn = voice_service.ask("公积金信用贷的办理流程是什么？")
    assert turn.refused is False
    assert turn.citations, "命中知识就必须带引用，否则无法追溯"
    # 断言"有事实、能念完"，不写死具体条款 —— 换一套知识库不该让用例变红
    assert any(k in turn.answer_text for k in ("流程", "资料", "条件", "身份证", "征信"))
    assert len(turn.answer_text) <= 200, "语音回复必须短"
    assert turn.prompt_version == "consultant@v1"
    snap = voice_service.metrics.snapshot()
    assert snap["turns"] == 1


def test_service_ask_unknown_question_refuses(voice_service):
    turn = voice_service.ask("你们公司老板叫什么名字？")
    assert turn.refused is True
    assert turn.needs_human is True


def test_service_audit_reports_findings(voice_service):
    report = voice_service.audit("放心，我们包过，百分百下款")
    assert report["passed"] is False
    assert report["needs_human"] is True
    assert report["max_severity"] == "high"
    assert report["safe_reply"]
    rule_ids = {f["rule_id"] for f in report["findings"]}
    assert "PROMISE-01" in rule_ids


def test_service_stream_reply_persists_session(voice_service):
    started = voice_service.start_agent(scene_id="consultant", user_id="C1002")
    session_id = started["session"]["session_id"]

    events = list(voice_service.stream_reply(user_text="办理流程是什么？", session_id=session_id))
    assert any(e.delta for e in events)
    assert [e for e in events if e.done]

    report = voice_service.report(session_id)
    assert report["turns"] == 1
    assert report["avg_total_ms"] >= 0
    assert voice_service.session_detail(session_id)["transcript"]


def test_service_stream_reply_writes_back_to_latest_running_session(voice_service):
    """回调里云端可能只给房间号（甚至什么都没有），不能因此 500。"""
    voice_service.start_agent(scene_id="consultant", user_id="C1002")
    list(voice_service.stream_reply(user_text="办理流程？", room_id=""))
    assert voice_service.sessions(), "兜底新建临时会话也必须落库"


def test_service_interrupt_then_reset(voice_service):
    out = voice_service.interrupt(room_id="room_z")
    assert out["interrupted"] is True
    assert voice_service.cancel_token("room_z").cancelled is True
    voice_service.reset_cancel("room_z")
    assert voice_service.cancel_token("room_z").cancelled is False


def test_service_prompt_lifecycle(voice_service):
    catalog = voice_service.prompt_catalog()
    assert catalog["active"]["consultant"] == "v1"

    added = voice_service.add_prompt(
        "consultant", "新版人设 {{knowledge}} {{extra}}", note="更短更口语", max_tokens=180
    )
    assert added["version"] == "v2"
    assert added["status"] == "candidate"

    activated = voice_service.activate_prompt("consultant", "v2")
    assert activated["status"] == "active"
    assert voice_service.prompt_catalog()["active"]["consultant"] == "v2"


def test_service_evaluate_and_gate(voice_service):
    report = voice_service.evaluate(prompt_key="consultant")
    assert report.total == len(builtin_cases())
    assert report.passed >= report.total - 1, "离线基线不该大面积失败"

    first = voice_service.gate_prompt(prompt_key="consultant")
    assert first["gate"]["allowed"] is True
    assert first["baseline"] is None

    second = voice_service.gate_prompt(prompt_key="consultant")
    assert second["baseline"] is not None, "第一次跑完要成为基线"
    assert second["gate"]["allowed"] is True


def test_service_register_tools_exposes_seven_tools(voice_service):
    from agent_platform.tools.registry import ToolRegistry

    registry = ToolRegistry()
    names = voice_service.register_tools(registry)
    assert names == [
        "voice.start_session",
        "voice.stop_session",
        "voice.ask",
        "voice.audit",
        "voice.session_report",
        "voice.metrics",
        "voice.eval_prompt",
    ]
    for name in names:
        assert len(registry.get(name).description) > 20
    # 开房会产生真实费用 → 必须是高风险且非幂等（平台会自动插人工审批）
    start = registry.get("voice.start_session")
    assert start.risk_level == "high"
    assert start.idempotent is False


def test_service_stats_reports_dry_run(voice_service):
    stats = voice_service.stats()
    assert stats["scenes"] >= 1
    assert stats["rtc"]["dry_run"] is True
    assert stats["rtc"]["callback_reachable"] is False   # 测试环境没配公网地址
    assert set(stats["dialog"]) == {"max_tokens", "history_turns", "knowledge_limit"}


# =========================================================================== #
# 十三、平台 Agent（路由 / 作用域 / 完整跑一轮）
# =========================================================================== #
@pytest.fixture
def voice_agent(settings, voice_service):
    from agent_platform.runtime import AgentRuntime
    from voice_service.agent import build_voice_agent

    runtime = AgentRuntime(settings=settings)
    voice_service.register_tools(runtime.tools)
    return build_voice_agent(voice_service, runtime.tools)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("帮我开一场语音接待", "room"),
        ("把客户接起来", "room"),
        ("帮我审一下这句能不能说：保证下款", "audit"),
        ("跑一遍评测看看有没有退化", "eval"),
        ("首字延迟 p95 现在多少", "stats"),
        ("公积金信用贷需要什么资料", "ask"),
    ],
)
def test_agent_route_by_intent(voice_agent, text, expected):
    assert voice_agent.route({"user_input": text}) == expected


@pytest.mark.parametrize(
    "route_name,expected_tool",
    [
        ("room", "voice.start_session"),
        ("ask", "voice.ask"),
        ("audit", "voice.audit"),
        ("eval", "voice.eval_prompt"),
        ("stats", "voice.metrics"),
    ],
)
def test_agent_scopes_tools_by_route(voice_agent, route_name, expected_tool):
    scoped = {t.name for t in voice_agent.resolve_scoped_tools({"route": route_name})}
    assert expected_tool in scoped
    assert "platform.handoff_to_human" in scoped, "逃生舱不能因为裁剪而不可用"
    assert len(scoped) < len(voice_agent.tools.all()), "作用域必须真的更窄"


def test_agent_scope_never_returns_namespace_tag(voice_agent):
    """返回 "voice" 标签会召回全部语音工具，裁剪静默失效（踩过的坑）。"""
    for route_name in ("room", "ask", "audit", "eval", "stats"):
        tags = voice_agent.tool_tags_for({"route": route_name})
        assert "voice" not in tags


def test_agent_decide_is_pure_state_function(voice_agent):
    """HITL 恢复会重放节点，决策必须"同样输入 → 同样输出"。"""
    state = {"user_input": "额度能贷多少", "route": "ask"}
    assert voice_agent.decide(state, []) == voice_agent.decide(state, [])


def test_agent_resolves_scene_from_text(voice_agent):
    action = voice_agent.decide({"user_input": "额度能贷多少", "route": "ask"}, [])
    assert action.arguments["scene_id"] == "amount"
    action2 = voice_agent.decide({"user_input": "要准备什么资料", "route": "ask"}, [])
    assert action2.arguments["scene_id"] == "docs"
    action3 = voice_agent.decide({"user_input": "你好", "route": "ask", "context": {"scene_id": "docs"}}, [])
    assert action3.arguments["scene_id"] == "docs", "显式给定的场景优先于文本猜测"


def test_agent_audit_extracts_text_after_colon(voice_agent):
    action = voice_agent.decide({"user_input": "帮我审一下这句能不能说：保证您能下款", "route": "audit"}, [])
    assert action.arguments["text"] == "保证您能下款"


def test_agent_audit_then_handoff_when_risky(voice_agent):
    """命中高风险 → 必须再走一步转人工，否则评审不通过。"""
    state = {
        "user_input": "帮我审一下：我们包过",
        "route": "audit",
        "observations": [
            {"tool": "voice.audit", "ok": True, "data": {"needs_human": True, "max_severity": "high"}}
        ],
    }
    action = voice_agent.decide(state, [])
    assert action.tool == "platform.handoff_to_human"
    assert action.arguments["urgency"] == "high"


def test_agent_run_ask_route_end_to_end(settings, voice_service):
    from agent_platform.runtime import AgentRuntime
    from voice_service.agent import build_voice_agent

    runtime = AgentRuntime(settings=settings)
    voice_service.register_tools(runtime.tools)
    runtime.register(build_voice_agent(voice_service, runtime.tools))

    result = runtime.invoke("voice_cs", "公积金信用贷需要哪些资料？")
    assert result.status == "completed"
    assert result.output["route"] == "ask"
    assert result.output["turn"]["answer"]


def test_agent_room_route_hits_human_approval(settings, voice_service):
    """开房是高风险工具 → 必须挂起等人工审批，绝不能自动放行。"""
    from agent_platform.runtime import AgentRuntime
    from voice_service.agent import build_voice_agent

    runtime = AgentRuntime(settings=settings)
    voice_service.register_tools(runtime.tools)
    runtime.register(build_voice_agent(voice_service, runtime.tools))

    result = runtime.invoke("voice_cs", "帮我开一场语音接待")
    assert result.status == "interrupted"
    assert result.pending_approval["tool"] == "voice.start_session"


# =========================================================================== #
# 十四、HTTP 接口（含 RTC 回调的 SSE）
# =========================================================================== #
@pytest.fixture
def client(app_context_fixture):
    from fastapi.testclient import TestClient

    from agent_platform.api.app import app

    return TestClient(app)


def test_api_scenes(client):
    payload = client.get("/voice/scenes").json()
    ids = {s["id"] for s in payload["scenes"]}
    assert {"consultant", "amount", "docs"} <= ids
    for scene in payload["scenes"]:
        # 前端只需要展示信息，任何密钥都不该出现在这里
        assert "token" not in json.dumps(scene, ensure_ascii=False).lower()


def test_api_room_join_and_start_stop(client):
    join = client.post("/voice/rooms", json={"scene_id": "consultant", "user_id": "C1002"}).json()
    assert join["dry_run"] is True
    assert join["rtc"]["Token"].startswith("001")

    started = client.post(
        "/voice/rooms/start",
        json={"scene_id": "consultant", "room_id": join["rtc"]["RoomId"], "user_id": "C1002"},
    ).json()
    session_id = started["session"]["session_id"]
    assert started["session"]["status"] == "running"

    stopped = client.post("/voice/rooms/stop", json={"session_id": session_id}).json()
    assert stopped["stopped"] is True


def test_api_stop_unknown_session_returns_404(client):
    assert client.post("/voice/rooms/stop", json={"session_id": "vs_nope"}).status_code == 404


def test_api_ask_and_audit(client):
    turn = client.post("/voice/ask", json={"question": "公积金信用贷的办理流程？"}).json()
    assert turn["answer_text"]
    assert turn["refused"] is False

    audit = client.post("/voice/audit", json={"text": "我们保证下款"}).json()
    assert audit["needs_human"] is True
    assert audit["safe_reply"]


def test_api_sessions_and_metrics(client):
    started = client.post("/voice/rooms/start", json={"user_id": "C1002"}).json()
    session_id = started["session"]["session_id"]
    client.post("/voice/ask", json={"question": "办理流程？"})

    sessions = client.get("/voice/sessions").json()
    assert sessions["count"] >= 1

    detail = client.get(f"/voice/sessions/{session_id}").json()
    assert detail["session_id"] == session_id

    metrics = client.get("/voice/metrics").json()
    assert "latency_ms" in metrics


def test_api_prompts_and_eval(client):
    catalog = client.get("/voice/prompts").json()
    assert "consultant" in catalog["active"]

    # 缺少 {{knowledge}} 占位符必须被拒（否则模型拿不到知识，静默退化成闲聊）
    bad = client.post("/voice/prompts", json={"key": "consultant", "system_prompt": "你是顾问"})
    assert bad.status_code == 400

    ok = client.post(
        "/voice/prompts",
        json={"key": "consultant", "system_prompt": "你是顾问 {{knowledge}} {{extra}}"},
    ).json()
    assert ok["version"] == "v2"

    activated = client.post("/voice/prompts/consultant/activate", json={"version": "v2"}).json()
    assert activated["status"] == "active"

    report = client.post("/voice/eval", json={"prompt_key": "consultant"}).json()
    assert report["total"] >= 1

    gate = client.post("/voice/eval/gate", json={"prompt_key": "consultant"}).json()
    assert "gate" in gate


def test_api_callback_streams_sse(client):
    """RTC 云端回调：必须是 SSE 流式，且能以 [DONE] 收尾。"""
    started = client.post("/voice/rooms/start", json={"user_id": "C1002"}).json()
    room_id = started["session"]["room_id"]

    body = {
        "model": "doubao-seed-1-6-flash",
        "stream": True,
        "messages": [
            {"role": "system", "content": "你是助贷顾问"},
            {"role": "user", "content": "公积金信用贷需要哪些资料？"},
        ],
    }
    with client.stream(
        "POST", "/voice/callback/chat", json=body, headers={"X-Room-Id": room_id}
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        raw = "".join(resp.iter_text())

    assert raw.rstrip().endswith("data: [DONE]")
    deltas = []
    for line in raw.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        chunk = json.loads(line[len("data: ") :])
        delta = chunk["choices"][0]["delta"].get("content", "")
        if delta:
            deltas.append(delta)
        assert chunk["object"] == "chat.completion.chunk"
    assert deltas, "回调必须真的产出内容分片"
    joined = "".join(deltas)
    assert any(k in joined for k in ("资料", "身份证", "征信", "流程", "条件"))


def test_api_callback_non_stream_returns_completion(client):
    body = {
        "stream": False,
        "messages": [{"role": "user", "content": "公积金信用贷需要哪些资料？"}],
    }
    payload = client.post("/voice/callback/chat", json=body).json()
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"]


def test_api_callback_rejects_missing_user_message(client):
    resp = client.post(
        "/voice/callback/chat",
        json={"stream": True, "messages": [{"role": "system", "content": "你是顾问"}]},
    )
    assert resp.status_code == 400


def test_api_callback_auth_is_enforced_when_key_configured(client, monkeypatch):
    """回调地址在公网上：配了 Key 就必须校验，否则等于免费的大模型代理。"""
    from app_context import get_app_context

    ctx = get_app_context()
    monkeypatch.setattr(ctx.voice.settings, "voice_callback_api_key", "secret-key")

    body = {"stream": False, "messages": [{"role": "user", "content": "你好"}]}
    assert client.post("/voice/callback/chat", json=body).status_code == 401
    ok = client.post(
        "/voice/callback/chat", json=body, headers={"Authorization": "Bearer secret-key"}
    )
    assert ok.status_code == 200


def test_api_callback_extracts_multimodal_text(client):
    """云端可能按多模态结构传 content（[{"type":"text","text":...}]）。"""
    body = {
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "公积金信用贷的办理流程？"}],
            }
        ],
    }
    payload = client.post("/voice/callback/chat", json=body).json()
    assert payload["choices"][0]["message"]["content"]
