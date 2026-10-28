"""全局配置。

统一入口：所有可调参数都收敛到这里，通过环境变量 / .env 覆盖。
为什么不用散落各处的 os.getenv：集中管理才能做校验、才能一眼看清有哪些开关。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓库根目录：src/agent_platform/config.py -> 上三级
# parents[0] = agent_platform, [1] = src, [2] = 仓库根
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """全局配置对象。字段名与 .env 中的大写变量一一对应（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",  # 允许 .env 中存在本服务不关心的变量
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ #
    # 1. 服务运行
    # ------------------------------------------------------------------ #
    app_env: Literal["local", "staging", "prod", "docker"] = "local"
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    # 各服务的路由前缀（同进程挂载；也可拆成多个服务分别部署）
    api_prefix_platform: str = "/platform"
    api_prefix_product: str = "/product"
    api_prefix_coaching: str = "/coaching"
    api_prefix_gateway: str = "/gateway"
    api_prefix_marketing: str = "/marketing"

    # ------------------------------------------------------------------ #
    # 1.5 API 鉴权（生产硬开关）
    #      默认关闭 = 本地开发 / 单元测试 / 演示零配置可跑；
    #      生产必须打开：这套接口能建线索、跑 Agent（烧钱）、批合同，
    #      裸奔等于把后台敞开。见 agent_platform/api/auth.py。
    # ------------------------------------------------------------------ #
    api_auth_enabled: bool = False
    api_auth_keys: str = ""  # 逗号分隔多密钥（轮换期可同时挂两把，同 gateway 多密钥思路）

    # ------------------------------------------------------------------ #
    # 2. LLM 供应商
    #    echo      离线确定性实现，零密钥，用于本地开发与单元测试
    #    openai    任意 OpenAI 兼容端点（DashScope / DeepSeek / Ollama / vLLM）
    # ------------------------------------------------------------------ #
    llm_provider: Literal["echo", "openai"] = "echo"
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_api_key: str = ""
    llm_model: str = "qwen-plus"
    llm_temperature: float = 0.2
    llm_max_tokens: int = 2048
    llm_timeout_seconds: float = 60.0

    # 向量化
    embedding_provider: Literal["local", "openai"] = "local"
    embedding_model: str = "text-embedding-v3"
    embedding_dim: int = 1024

    # ------------------------------------------------------------------ #
    # 3. 状态持久化（LangGraph Checkpointer）
    # ------------------------------------------------------------------ #
    checkpointer_backend: Literal["memory", "sqlite", "postgres"] = "sqlite"
    checkpointer_sqlite_path: str = ".data/checkpoints.sqlite"
    postgres_dsn: str = "postgresql://postgres:postgres@localhost:5432/zhudai_ai"
    # thread_id → agent 映射的落盘位置（HITL 挂起跨重启后 resume 需要，
    # 见 thread_map.py 的说明；checkpointer_backend=memory 时不落盘）
    runtime_map_path: str = ".data/runtime/thread_map.sqlite"

    # ------------------------------------------------------------------ #
    # 4.5 分布式锁后端（多 worker 部署必换）
    #      sqlite  默认：锁表在业务库里，只保护单进程多线程
    #      redis   生产：SET NX PX 跨进程互斥，多 worker 同时推进同一条
    #              线索时只有一个人能拿到锁（见 flow_ops/locking.py）
    # ------------------------------------------------------------------ #
    flow_lock_backend: Literal["sqlite", "redis"] = "sqlite"
    redis_url: str = "redis://localhost:6379/0"  # 仅 flow_lock_backend=redis 时需要

    # ------------------------------------------------------------------ #
    # 4. 向量库
    # ------------------------------------------------------------------ #
    vector_backend: Literal["local", "qdrant"] = "local"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""

    # ------------------------------------------------------------------ #
    # 5. 业务数据
    # ------------------------------------------------------------------ #
    policy_data_dir: str = ".data/policies"
    profile_data_dir: str = ".data/profiles"
    marketing_data_dir: str = ".data/marketing"
    sqlite_business_path: str = ".data/business.sqlite"

    # ------------------------------------------------------------------ #
    # 5.1 检索相关性闸门（决定"拒答"还是"作答"）
    #     默认值针对离线 LocalHashEmbedding + 词项 Reranker 校准。
    #     换成真实 Embedding 后请用 probe 方法重新校准，否则会误拒或误答。
    # ------------------------------------------------------------------ #
    rag_min_rerank_score: float = Field(default=0.15, ge=0, le=1, description="词项层相关性下限")
    rag_min_dense_score: float = Field(default=0.35, ge=0, le=1, description="语义层相似度下限")

    # ------------------------------------------------------------------ #
    # 5.5 项目六：大模型推理网关与可观测评测平台
    #     gateway_enabled=False 时，业务照常直连 LLM（保持向后兼容 / 零风险）。
    #     置 True 后，业务调用统一经过网关 —— 路由、缓存、限流、密钥轮转、
    #     成本核算与 span 级可观测全部生效，业务代码一行都不用改。
    # ------------------------------------------------------------------ #
    gateway_enabled: bool = False
    gateway_sqlite_path: str = ".data/gateway.sqlite"

    # 模型分档名（分档是本项目"复杂度路由"的基础：轻任务走小模型省钱）
    gateway_model_small: str = "qwen-turbo"
    gateway_model_medium: str = "qwen-plus"
    gateway_model_large: str = "qwen-max"
    gateway_default_tier: Literal["small", "medium", "large"] = "medium"
    # 复杂度路由的两个阈值（0-1）—— 低于 medium 阈值走小模型，高于 large 阈值走大模型
    gateway_tier_threshold_medium: float = Field(default=0.35, ge=0, le=1)
    gateway_tier_threshold_large: float = Field(default=0.70, ge=0, le=1)

    # 价格表（¥ / 1K tokens，输入价, 输出价）—— 演示估值，生产按账单校准
    gateway_price_small_in: float = 0.0008
    gateway_price_small_out: float = 0.0020
    gateway_price_medium_in: float = 0.0040
    gateway_price_medium_out: float = 0.0120
    gateway_price_large_in: float = 0.0200
    gateway_price_large_out: float = 0.0600
    # 成本基线：用于计算"成本治理节省了多少"。默认取大模型单价（即"不做治理"的对照）
    gateway_baseline_tier: Literal["small", "medium", "large"] = "large"

    # 缓存（两个坑：相似度阈值要"宁漏不错"，且必须带 TTL —— 缓存错答案比不缓存更糟）
    gateway_cache_ttl_seconds: float = Field(default=900.0, ge=0)
    gateway_prefix_cache_max_entries: int = Field(default=512, ge=0)
    gateway_semantic_cache_max_entries: int = Field(default=512, ge=0)
    gateway_semantic_cache_threshold: float = Field(default=0.94, ge=0, le=1)
    # 任务 → 缓存策略覆盖（JSON，如 '{"policy_qa":"off","new_task":"semantic"}'）。
    # 内置默认表在 gateway/gateway.py（合规敏感任务默认 off/exact）；
    # 新增任务类型在这里覆盖即可，不必改网关代码。非法值运行时回落默认表并告警。
    gateway_task_cache_overrides: dict[str, str] = Field(default_factory=dict)

    # 限流与配额
    gateway_rate_limit_per_minute: int = Field(default=600, ge=1)
    gateway_rate_limit_burst: int = Field(default=120, ge=1)
    gateway_quota_tokens_per_scope: int = Field(default=2_000_000, ge=1000)

    # 上下文裁剪
    gateway_max_context_tokens: int = Field(default=6000, ge=256)
    gateway_keep_recent_turns: int = Field(default=6, ge=1)
    gateway_summarize_chars: int = Field(default=200, ge=20)

    # 多密钥轮转：逗号分隔。为空则用 llm_api_key 单一密钥。
    gateway_api_keys: str = ""
    gateway_key_cooldown_seconds: float = Field(default=60.0, ge=0)

    # 评测闭环
    gateway_eval_golden_path: str = ""  # 外部 Golden Set（jsonl）路径；为空则用内置代表性集合
    gateway_eval_max_regression: float = Field(default=0.02, ge=0, le=1, description="CI 准入允许的最大退化幅度")

    # ------------------------------------------------------------------ #
    # 5.6 项目七：销售素材推广与客户激活
    # ------------------------------------------------------------------ #
    marketing_default_channel: Literal["moments", "douyin", "xiaohongshu"] = "moments"
    marketing_dormant_days: int = Field(default=7, ge=0, description="超过多少天未互动视为沉睡客户")
    marketing_activation_cooldown_days: int = Field(default=3, ge=0, description="同一客户两次激活的最小间隔（防骚扰）")
    marketing_max_activation_per_customer: int = Field(default=2, ge=1)

    # ------------------------------------------------------------------ #
    # 5.7 项目一：AI 实时语音对话智能客服（火山引擎 RTC + ASR + TTS + 方舟）
    #
    #     语音链路的"开关"设计：
    #       voice_enabled=False（默认）→ RTC OpenAPI 只记录不下发（dry-run），
    #                                    进房 Token 照常签发、对话链路照常跑通。
    #                                    于是 clone 下来零密钥就能演示与测试。
    #       voice_enabled=True        → 真实开房，需要 AK/SK + 公网回调地址。
    # ------------------------------------------------------------------ #
    voice_enabled: bool = False
    voice_data_dir: str = ".data/voice"
    voice_sqlite_path: str = ".data/voice/voice.sqlite"
    api_prefix_voice: str = "/voice"

    # ---- RTC 应用凭证（控制台 > AI 音视频互动方案 > 应用管理）----
    rtc_app_id: str = ""
    rtc_app_key: str = ""  # 用于签发进房 Token（不是控制台的临时 Token）
    rtc_region: str = "cn-north-1"
    rtc_service: str = "rtc"
    voice_token_ttl_seconds: int = Field(default=86_400, ge=60, description="进房 Token 有效期（官方建议 24h 内）")

    # ---- 火山 IAM（调 OpenAPI 用，与 RTC AppId 必须同账号）----
    volc_access_key: str = ""
    volc_secret_key: str = ""

    # ---- 语音服务凭证 ----
    voice_asr_resource_id: str = "volc.seedasr.sauc.duration"  # 双向流大模型 ASR 的资源 ID
    voice_tts_app_id: str = ""
    voice_tts_cluster: str = "volcano_icl"  # 复刻音色用 volcano_icl，预置音色用 volcano_tts
    voice_tts_voice_type: str = ""  # 空则用平台默认音色
    voice_hotwords: str = "债务置换,公积金信用贷,征信报告,社保,保单"  # 逗号分隔

    # ---- 第三方 LLM 回调 ----
    voice_llm_model: str = "doubao-seed-1-6-flash"  # 语音场景用 flash：首字延迟优先
    voice_public_base_url: str = ""  # RTC 云端必须能访问到（ngrok / 公网域名）
    voice_callback_api_key: str = ""  # 回调鉴权（RTC 每次请求都会带）

    # ---- 语音对话参数（和文本场景刻意不同，见 agent_config.py 的说明）----
    voice_max_tokens: int = Field(default=220, ge=150, le=300, description="语音回复上限：150-300 才听得完")
    voice_temperature: float = Field(default=0.3, ge=0, le=2)
    voice_history_turns: int = Field(default=6, ge=0, le=20, description="带多少轮历史（越多越准，越慢越贵）")
    voice_knowledge_limit: int = Field(default=2, ge=0, le=5, description="注入几条知识片段（1-3 最佳）")
    voice_knowledge_max_chars: int = Field(default=900, ge=200, le=4000)
    voice_min_relevance: float = Field(default=0.25, ge=0, le=1, description="低于该相关度直接拒答")
    voice_rerank_enabled: bool = True
    voice_dense_weight: float = Field(default=0.5, ge=0, le=1, description="混合检索里语义层的权重")
    voice_eval_max_regression: float = Field(default=0.03, ge=0, le=1, description="提示词版本准入允许的退化")

    # ------------------------------------------------------------------ #
    # 5.8 项目八：助贷业务流程数字员工
    #
    #     设计前提（业务边界，写进代码而不是只写进文档）：
    #       自动化范围 = 咨询 → 邀约 → 方案生成 → 费用评估（准备类动作）
    #       人工节点   = 接待 / 谈判 / 费用确认（成交类动作）
    #     高风险动作（发合同 / 确认费用）必须走 HITL 审批，见 flow_ops/approval.py。
    # ------------------------------------------------------------------ #
    flow_enabled: bool = False  # 预留开关：关闭时服务仍可装配，但不驱动真实流程
    flow_data_dir: str = ".data/flow"
    flow_sqlite_path: str = ".data/flow/flow.sqlite"
    api_prefix_flow: str = "/flow"

    # ---- SOP 时限（漏跟进检测的判据，全部显式可配）----
    flow_first_call_sla_minutes: int = Field(default=5, ge=1, description="线索分配后多久内必须完成首呼")
    flow_wechat_sla_hours: int = Field(default=2, ge=1, description="首呼后多久内必须加微")
    flow_followup_sla_hours: int = Field(default=24, ge=1, description="跟进间隔超过多久算停滞")
    flow_invite_confirm_sla_hours: int = Field(default=4, ge=1, description="邀约后多久内必须确认到店时间")

    # ---- 费用预估口径（【待填】上线前必须与财务核对，见 docs/07-flow-ops.md）----
    flow_service_fee_rate: float = Field(default=0.03, ge=0, le=0.1, description="服务费率：按放款金额收取，行业常见 1%-5%")
    flow_fee_rate_large_loan: float = Field(default=0.02, ge=0, le=0.1, description="大额档服务费率")
    flow_fee_large_loan_threshold: float = Field(default=300_000, ge=0, description="大额档门槛（放款金额 ≥ 该值）")
    flow_default_loan_months: int = Field(default=36, ge=6, le=360, description="利息试算默认期数")
    flow_default_annual_rate: float = Field(default=0.072, ge=0, le=0.24, description="利息试算默认年化（助贷常见 7.2%）")

    # ---- 到店前准备 ----
    flow_readiness_warn_minutes: int = Field(default=10, ge=1, description="准备耗时超过多久算异常（预警用）")

    # ---- 可靠性 ----
    flow_idempotency_ttl_hours: int = Field(default=24, ge=1, description="幂等记录保留时长")
    flow_lock_ttl_seconds: int = Field(default=30, ge=1, description="分布式锁租约（秒）")
    flow_high_risk_actions: str = "send_contract,confirm_fee"  # 必须 HITL 的动作（逗号分隔）

    # ---- 调度与通知（"预警必须自己跑、必须送达"的工程保障）----
    #      SOP 扫描放进常驻循环，漏跟进预警才不依赖"有人记得来拉"；
    #      通知渠道把预警/待审批推到主管眼前，落库 ≠ 送达。
    flow_scheduler_enabled: bool = False  # 默认关：测试与本地不起后台任务；生产/compose 打开
    flow_scheduler_interval_seconds: int = Field(default=300, ge=10, description="SOP 扫描间隔（秒）")
    flow_notify_webhook_url: str = ""  # 企微/钉钉机器人 webhook；留空 = 只写日志（LogNotifier）
    flow_notify_timeout_seconds: float = Field(default=3.0, ge=0.5, le=30, description="通知超时（秒），失败只记日志不抛")

    # ---- 评测 ----
    flow_eval_max_regression: float = Field(default=0.03, ge=0, le=1, description="流程行为准入允许的退化幅度")

    # ------------------------------------------------------------------ #
    # 5.9 项目四：助贷对话数据中台
    #
    #     定位：**数据源头**。原始消息不可变（只增不改），
    #     清洗 → 脱敏 → 人工审核 → 多下游复用。
    #
    #     · data_source_path 指向微信本地消息库（MSG 库）；不存在时
    #       自动退回 `policy_data_dir/conversations.jsonl` 演示语料。
    #     · data_auto_ingest=True：首次启动且原始层为空时自动跑一轮抽取。
    #       抽取是幂等的（msg_id / block_id 内容派生），重跑不会产生重复。
    #     · data_feed_enabled=True：已批准对话优先作为项目七的案例语料源。
    # ------------------------------------------------------------------ #
    data_platform_data_dir: str = ".data/data_platform"
    data_platform_sqlite_path: str = ".data/data_platform/data_platform.sqlite"
    data_source_path: str = ".data/data_platform/wechat_source.sqlite"
    data_export_dir: str = ".data/data_platform/exports"
    api_prefix_data: str = "/data"
    # 5 分钟时间窗（行业里微信会话的常见话题跨度；可调）
    data_window_minutes: float = Field(default=5.0, ge=0.1)
    data_min_chars: int = Field(default=6, ge=1, description="低于该有效字符数视为过短")
    data_high_value_quality: float = Field(default=0.70, ge=0, le=1, description="高价值质量分阈值")
    data_chunk_chars: int = Field(default=300, ge=80, description="知识切片长度（字符）")
    data_chunk_overlap: int = Field(default=60, ge=0, description="切片重叠（字符）")
    data_auto_ingest: bool = True
    data_feed_enabled: bool = True

    # ------------------------------------------------------------------ #
    # 6. 平台治理阈值（对应"失败自愈 / 成本治理"）
    # ------------------------------------------------------------------ #
    max_agent_steps: int = Field(default=12, ge=1)
    max_agent_tokens: int = Field(default=60_000, ge=1000)
    tool_max_retries: int = Field(default=3, ge=0)
    tool_retry_base_seconds: float = Field(default=0.2, ge=0)
    circuit_failure_threshold: int = Field(default=5, ge=1)
    circuit_reset_seconds: float = Field(default=30.0, ge=0)
    approval_timeout_seconds: float = Field(default=300.0, ge=0)
    approval_timeout_strategy: Literal["abort", "escalate"] = "abort"
    # 审批单持久化：跨进程/重启共享同一份待办（内存后端仅用于测试）
    approval_store_backend: Literal["memory", "sqlite"] = "sqlite"
    approvals_sqlite_path: str = ".data/platform/approvals.sqlite"

    # ------------------------------------------------------------------ #
    # 校验与派生属性
    # ------------------------------------------------------------------ #
    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        return v.upper()

    @model_validator(mode="after")
    def _check_postgres_dsn(self) -> Settings:
        """选了 postgres 后端，DSN 就必须像样 —— 配置错误在启动时就炸出来。

        为什么在配置层做：等到 build_checkpointer 连接超时才报错，
        日志里只有一句"连接失败"，排查要翻到应用装配深处；
        在这里报错，信息就是"你哪一项配错了"。
        """
        if self.checkpointer_backend == "postgres":
            dsn = (self.postgres_dsn or "").strip()
            if not dsn.startswith(("postgresql://", "postgres://")):
                raise ValueError(
                    "CHECKPOINTER_BACKEND=postgres 时 POSTGRES_DSN 必须以 "
                    "postgresql:// 开头（示例：postgresql://postgres:postgres@localhost:5432/zhudai_ai）"
                )
        return self

    @property
    def sqlite_abs_path(self) -> Path:
        """业务 SQLite 的绝对路径（相对路径按仓库根解析）。"""
        return self._abs(self.sqlite_business_path)

    @property
    def checkpoint_sqlite_abs_path(self) -> Path:
        return self._abs(self.checkpointer_sqlite_path)

    @property
    def runtime_map_abs_path(self) -> Path:
        return self._abs(self.runtime_map_path)

    @property
    def approvals_sqlite_abs_path(self) -> Path:
        return self._abs(self.approvals_sqlite_path)

    @property
    def policy_abs_dir(self) -> Path:
        return self._abs(self.policy_data_dir)

    @property
    def profile_abs_dir(self) -> Path:
        return self._abs(self.profile_data_dir)

    @property
    def marketing_abs_dir(self) -> Path:
        return self._abs(self.marketing_data_dir)

    @property
    def gateway_sqlite_abs_path(self) -> Path:
        return self._abs(self.gateway_sqlite_path)

    @property
    def voice_abs_dir(self) -> Path:
        return self._abs(self.voice_data_dir)

    @property
    def voice_sqlite_abs_path(self) -> Path:
        return self._abs(self.voice_sqlite_path)

    @property
    def voice_callback_url(self) -> str:
        """RTC 云端回调我们的地址（OpenAI 兼容的 chat completions）。"""
        base = (self.voice_public_base_url or "").rstrip("/")
        return f"{base}{self.api_prefix_voice}/callback/chat" if base else ""

    @property
    def voice_callback_reachable(self) -> bool:
        """回调地址是否"云端可能访问到"。

        本地 localhost 一定不行 —— RTC 是在公网调我们。这一条做成属性，
        是为了在开房前就给出明确告警，而不是等客户投诉"AI 不说话"。
        """
        url = self.voice_callback_url
        if not url.startswith("https://") and not url.startswith("http://"):
            return False
        lowered = url.lower()
        return not any(h in lowered for h in ("localhost", "127.0.0.1", "0.0.0.0", "::1"))

    def voice_hotword_list(self) -> list[str]:
        return [w.strip() for w in (self.voice_hotwords or "").split(",") if w.strip()]

    # ---- 5.8 项目八 ----
    @property
    def flow_abs_dir(self) -> Path:
        return self._abs(self.flow_data_dir)

    @property
    def flow_sqlite_abs_path(self) -> Path:
        return self._abs(self.flow_sqlite_path)

    def flow_high_risk_action_set(self) -> frozenset[str]:
        """必须走 HITL 审批的动作集合（从配置读，不硬编码在业务里）。"""
        return frozenset(a.strip() for a in (self.flow_high_risk_actions or "").split(",") if a.strip())

    # ---- 5.9 项目四 ----
    @property
    def data_abs_dir(self) -> Path:
        return self._abs(self.data_platform_data_dir)

    @property
    def data_sqlite_abs_path(self) -> Path:
        return self._abs(self.data_platform_sqlite_path)

    @property
    def data_source_abs_path(self) -> Path:
        return self._abs(self.data_source_path)

    @property
    def data_export_abs_dir(self) -> Path:
        return self._abs(self.data_export_dir)

    def api_auth_key_set(self) -> frozenset[str]:
        """API 鉴权密钥集合（逗号分隔；轮换期同时挂多把，旧key下线后移除即可）。"""
        return frozenset(k.strip() for k in (self.api_auth_keys or "").split(",") if k.strip())

    def gateway_api_key_list(self) -> list[str]:
        """多密钥轮转用的密钥列表。

        优先取 GATEWAY_API_KEYS（逗号分隔，可用于灰度轮换）；
        为空则退化为 LLM_API_KEY 单一密钥（本地/离线模式下就是空列表）。
        """
        raw = (self.gateway_api_keys or "").strip()
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        if keys:
            return keys
        return [self.llm_api_key] if self.llm_api_key else []

    def gateway_price_table(self) -> dict[str, tuple[float, float]]:
        """按分档返回 (输入价, 输出价)，单位 ¥ / 1K tokens。"""
        return {
            "small": (self.gateway_price_small_in, self.gateway_price_small_out),
            "medium": (self.gateway_price_medium_in, self.gateway_price_medium_out),
            "large": (self.gateway_price_large_in, self.gateway_price_large_out),
        }

    def gateway_tier_model(self, tier: str) -> str:
        """分档 → 模型名。"""
        return {
            "small": self.gateway_model_small,
            "medium": self.gateway_model_medium,
            "large": self.gateway_model_large,
        }.get(tier, self.gateway_model_medium)

    def _abs(self, raw: str) -> Path:
        """把配置里的相对路径按仓库根展开成绝对路径。

        为什么这样做：命令行 / docker / 测试的工作目录不同，
        用"相对仓库根"而不是"相对当前工作目录"，行为才稳定可预期。
        """
        p = Path(raw)
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    def ensure_dirs(self) -> None:
        """确保运行期需要的目录存在。启动时调用一次即可。"""
        for d in (
            self.checkpoint_sqlite_abs_path.parent,
            self.runtime_map_abs_path.parent,
            self.approvals_sqlite_abs_path.parent,
            self.sqlite_abs_path.parent,
            self.policy_abs_dir,
            self.profile_abs_dir,
            self.marketing_abs_dir,
            self.gateway_sqlite_abs_path.parent,
            self.voice_abs_dir,
            self.voice_sqlite_abs_path.parent,
            self.flow_abs_dir,
            self.flow_sqlite_abs_path.parent,
            self.data_abs_dir,
            self.data_sqlite_abs_path.parent,
            self.data_source_abs_path.parent,
            self.data_export_abs_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局配置单例。

    用 lru_cache 保证进程内只解析一次 .env；测试里若改了环境变量，
    可调用 get_settings.cache_clear() 强制重新加载。
    """
    settings = Settings()
    settings.ensure_dirs()
    return settings
