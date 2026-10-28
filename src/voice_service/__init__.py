"""项目一：AI 实时语音对话智能客服。

模块划分（按一条语音的流向排）：

    rtc_token.py     进房凭证（001 方案，HMAC-SHA256）
    rtc_client.py    火山 RTC OpenAPI（StartVoiceChat / StopVoiceChat / UpdateVoiceChat）
    volc_sign.py     火山 OpenAPI 的 V4 签名
    agent_config.py  下发给云端的 ASR / TTS / LLM 配置
    rag.py           语音检索策略（limit 1-3、相关性闸门、稳定排序）
    prompt.py        提示词注册表（版本化 + 前缀缓存友好的知识块拼接）
    dialog.py        对话引擎（流式切句 + 逐句过审 + 延迟拆解）
    censor.py        内容安全（承诺/造假/违规收费/以贷养贷/隐私五条红线）
    session.py       会话与轮次落库
    metrics.py       首字延迟与业务指标
    evaluation.py    Golden Set 评测与版本准入
    service.py       门面（装配 + 对外能力）
    agent.py         平台 Agent（五条路由，开房走人工审批）
    api.py           HTTP 接口（含 RTC 云端回调 / SSE）
"""

from voice_service.domain import (
    CensorVerdict,
    LatencyBreakdown,
    RetrievalResult,
    RoomJoinInfo,
    RtcCallResult,
    VoiceScene,
    VoiceSession,
    VoiceTurn,
)
from voice_service.service import VoiceService

__all__ = [
    "CensorVerdict",
    "LatencyBreakdown",
    "RetrievalResult",
    "RoomJoinInfo",
    "RtcCallResult",
    "VoiceScene",
    "VoiceService",
    "VoiceSession",
    "VoiceTurn",
]
