"""云端 Agent 的启动配置（ASR / TTS / 第三方 LLM）。

这三个 config 决定了"客户听到的 AI 是什么样"：

    ASRConfig  怎么听  —— 双向流、大模型版、热词、情绪检测、敏感词
    LLMConfig  怎么想  —— **指向我们自己的 Python 服务**（第三方 LLM 回填模式），
                          于是 RAG、提示词、内容安全全部握在自己手里
    TTSConfig  怎么说  —— 音色（可取复刻音色）、语速、音量、情绪

关键设计：**默认不用火山内置 LLM**。
    内置 LLM 是"云端 RTC → 直接调方舟模型"，我们的知识库、提示词版本管理、
    合规红线、内容安全全都插不进去；改成第三方 LLM 回调后，
    RTC 只负责 ASR/TTS 和音频传输，**回答的每一句话都经过我们的服务端** ——
    这是这个项目最本质的架构改造（对应面试里的"Agent 服务改造"）。

字段命名对齐官方「AI 音视频互动方案」的下发配置；不在我们封装范围内的字段
统一走 `extra` 透传（dict 深合并），避免"官方加了个参数就得改代码发版"。
"""

from __future__ import annotations

from typing import Any

from agent_platform.config import Settings

# 语音场景的输出上限区间。
# 依据：人说话约 4-5 字/秒，150 tokens ≈ 100-120 字 ≈ 20-30 秒；
# 超过 1 分钟用户必然打断。所以语音场景不该沿用文本场景的 2048。
VOICE_MAX_TOKENS_FLOOR = 150
VOICE_MAX_TOKENS_CEIL = 300


def clamp_max_tokens(value: int) -> int:
    """把回复长度限制在"能听完"的区间内。"""
    return max(VOICE_MAX_TOKENS_FLOOR, min(int(value), VOICE_MAX_TOKENS_CEIL))


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """深合并：extra 覆盖 base，但 dict 层级递归合并而不是整体替换。"""
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


# =========================================================================== #
# ASR：怎么听
# =========================================================================== #
def build_asr_config(
    settings: Settings,
    *,
    hotwords: list[str] | None = None,
    emotion: bool = True,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建 ASR 配置。

    重点参数（都在简历里能讲出"为什么"）：
        StreamMode=2   双向流：用户还在说话就持续回传中间结果，
                       识别时长/音频时长（RTF）更低，尾字延迟更短；
                       Mode=1 的单向流要等一句话结束才开始识别，体感明显更慢；
        hotwords       热词：把"债务置换""征信报告""公积金信用贷"这类
                       业务专有名词加进去 —— 通用模型容易听成别的东西，
                       而语音场景没有"重看一遍"的机会；
        emotion        情绪检测：客户说"你们这是骗人的吧"时情绪是负向的，
                       话术要转安抚而不是继续推产品（见 tts.build_tts_config 的呼应）。
    """
    asr_params: dict[str, Any] = {}
    if hotwords:
        # 官方以 JSON 字符串透传底层 ASR 参数；这里保持字符串形态（别改成 dict）
        asr_params["hotwords"] = hotwords
    if emotion:
        asr_params["enable_emotion"] = True

    import json

    config: dict[str, Any] = {
        "Provider": "volcano",
        "ProviderParams": {
            "Mode": "bigmodel",
            "Credential": {"ApiResourceId": settings.voice_asr_resource_id},
            # 2 = 双向流（推荐）；1 = 单向流
            "StreamMode": 2,
            "VolcanoASRParameters": json.dumps(asr_params, ensure_ascii=False),
        },
    }
    return _deep_merge(config, extra or {})


# =========================================================================== #
# TTS：怎么说
# =========================================================================== #
def build_tts_config(
    settings: Settings,
    *,
    voice_type: str = "",
    speed_ratio: float = 1.0,
    emotion: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建 TTS 配置。

    音色（voice_type）优先取场景配置，其次取全局默认；
    复刻音色与预置音色在同一个字段里（复刻音色形如 S_xxxxx），
    所以"用业务员自己的声音接待客户"只改一个配置，不改代码。
    """
    audio: dict[str, Any] = {
        "voice_type": voice_type or settings.voice_tts_voice_type,
        "speed_ratio": speed_ratio,
        "pitch_ratio": 1.0,
        "volume_ratio": 1.0,
    }
    if emotion:
        # 情绪化播报：客户急躁时语速略快、语气更稳；客户犹豫时放慢
        audio["emotion"] = emotion

    config: dict[str, Any] = {
        "Provider": "volcano",
        "ProviderParams": {
            "app": {"appid": settings.voice_tts_app_id, "cluster": settings.voice_tts_cluster},
            "audio": audio,
        },
    }
    return _deep_merge(config, extra or {})


# =========================================================================== #
# LLM：怎么想（第三方回调 → 我们自己的服务）
# =========================================================================== #
def build_llm_config(
    settings: Settings,
    *,
    callback_url: str,
    system_prompt: str,
    max_tokens: int,
    temperature: float,
    history_turns: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建第三方 LLM 配置。

    `Mode="Custom"` + `Url` 的含义是：RTC 云端拿到 ASR 文本后，
    不再自己调模型，而是把 OpenAI 协议的请求打到我们这里。
    **回调地址必须公网可达** —— 本地开发用 ngrok / 内网穿透，
    否则 RTC 云端连不上，表现是"AI 不说话"（而日志里一切正常，很难查）。
    """
    config: dict[str, Any] = {
        "Mode": "Custom",
        "Url": callback_url,
        "ApiKey": settings.voice_callback_api_key,
        "ModelName": settings.voice_llm_model,
        # SystemMessages 用列表（官方结构），内容只有一条 ——
        # 与 dialog.py 里"system + 知识合并成一条"的策略保持一致，为了前缀缓存。
        "SystemMessages": [system_prompt] if system_prompt else [],
        "HistoryLength": max(0, int(history_turns)),
        "Temperature": temperature,
        "MaxTokens": clamp_max_tokens(max_tokens),
        # 必须开流式：TTS 要边收边读，否则要等整段生成完才开口
        "Stream": True,
    }
    return _deep_merge(config, extra or {})


# =========================================================================== #
# StartVoiceChat 的完整请求体
# =========================================================================== #
def build_start_payload(
    settings: Settings,
    *,
    room_id: str,
    user_id: str,
    bot_user_id: str,
    system_prompt: str,
    welcome_message: str = "",
    task_id: str = "",
    max_tokens: int = 220,
    temperature: float = 0.3,
    history_turns: int = 6,
    voice_type: str = "",
    speed_ratio: float = 1.0,
    hotwords: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建 StartVoiceChat 请求体（把 AI"拉进房间"）。

    结构上分四块，读的时候按这个顺序看：
        AppId / RoomId      —— 进哪个房间
        AgentConfig         —— AI 以什么身份、说什么开场白、要不要字幕/状态回调
        Config.ASR/TTS/LLM  —— 听/说/想各自的参数
        extra               —— 官方新增字段的透传口
    """
    payload: dict[str, Any] = {
        "AppId": settings.rtc_app_id,
        "RoomId": room_id,
        "TaskId": task_id or f"task_{room_id}",
        "AgentConfig": {
            "UserId": bot_user_id,
            # 只服务当前进房用户；多客服共用房间时再扩列表
            "TargetUserId": [user_id],
            "WelcomeMessage": welcome_message,
            # 打开会话状态回调，前端才能显示"正在思考/正在说话/说完了"
            "EnableConversationStateCallback": True,
            # 允许打断：这是语音体验的分水岭，关掉用户会觉得"AI 不理我"
            "InterruptMode": True,
        },
        "Config": {
            "ASRConfig": build_asr_config(settings, hotwords=hotwords),
            "TTSConfig": build_tts_config(settings, voice_type=voice_type, speed_ratio=speed_ratio),
            "LLMConfig": build_llm_config(
                settings,
                callback_url=settings.voice_callback_url,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                history_turns=history_turns,
            ),
        },
    }
    return _deep_merge(payload, extra or {})
