"""数据中台服务门面（项目四）。

对外六件事：
    ingest           抽取 → 清洗 → 脱敏 → 切块 → 进暂存区
    review           人工审核闸门：approve / reject / edit
    export           导出四种下游格式（只导已批准数据）
    build_knowledge  构建向量切片 + 结构化文章
    search_knowledge 知识检索（RAG 的原料）
    feed_conversations 把已审对话供给其他业务（项目三/七的数据源）

**这个项目没有独立的业务 Agent**，这是刻意的：
    数据中台不面向客户对话，它的使用者是"数据运营 + 其他业务系统"。
    所以它只向平台注册**工具**（data.*），不注册 Agent ——
    也避免了"为了凑数硬造一个 Agent"的反模式。

跨项目咬合（全部通过数据，不 import 业务代码）：
    · 输出：`feed_conversations()` → 装配层注入项目七做案例素材源
    · 输出：导出文件（RAG CSV / JSONL）→ 项目二知识库、项目三题库的上游
    · 输入：项目二客户档案里的真实姓名 → 脱敏词典
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from agent_platform.config import Settings, get_settings
from agent_platform.llm.base import EmbeddingProvider, LLMProvider
from agent_platform.llm.providers import build_embedder, build_llm
from agent_platform.logging_config import get_logger
from agent_platform.tools.registry import ToolRegistry
from agent_platform.tools.spec import ToolSpec
from data_platform.chunker import ConversationBuilder
from data_platform.domain import (
    CATEGORY_NAMES,
    ExportFormat,
    IngestReport,
    RawMessage,
    ReviewAction,
    StagedConversation,
)
from data_platform.errors import DataPlatformError, SourceUnavailable
from data_platform.exporter import DatasetExporter
from data_platform.filters import ConversationFilter
from data_platform.knowledge import KnowledgeBuilder
from data_platform.masking import MaskingEngine, collect_known_names
from data_platform.source import (
    JsonlSource,
    MessageSource,
    WeChatSqliteSource,
    load_sessions,
)
from data_platform.staging import ReviewGate
from data_platform.store import DataPlatformStore

logger = get_logger(__name__)

#: 成交信号（用于给下游一个 outcome 标记；规则判定，不靠模型）
_WON_MARKERS = ("签约", "签了", "签合同", "放款", "到账", "办成了", "通过了")

#: 知识构建水位的键名（build_watermarks 表目前只有这一个消费者）
_KNOWLEDGE_WATERMARK = "knowledge"


class DataPlatformService:
    """助贷对话数据中台。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        llm: LLMProvider | None = None,
        embedder: EmbeddingProvider | None = None,
        source: MessageSource | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.llm = llm or build_llm(self.settings)
        self.embedder = embedder or build_embedder(self.settings)

        self.store = DataPlatformStore(self.settings.data_sqlite_abs_path)
        self.masker = MaskingEngine(known_names=self._load_known_names())
        self.filter = ConversationFilter(
            min_chars=self.settings.data_min_chars,
            high_value_threshold=self.settings.data_high_value_quality,
        )
        self.builder = ConversationBuilder(
            window_minutes=self.settings.data_window_minutes,
            masker=self.masker,
            conv_filter=self.filter,
        )
        self.gate = ReviewGate(self.store, masker=self.masker, conv_filter=self.filter)
        self.exporter = DatasetExporter(
            self.store,
            masker=self.masker,
            conv_filter=self.filter,
            export_dir=self.settings.data_export_abs_dir,
        )
        self.knowledge = KnowledgeBuilder(
            self.store,
            llm=self.llm,
            embedder=self.embedder,
            conv_filter=self.filter,
            masker=self.masker,
        )
        self._source = source
        self._last_report: IngestReport | None = None

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #
    def bootstrap(self) -> dict[str, Any]:
        """启动自检：探测数据源；配置了自动抽取就先跑一轮（幂等，重跑无害）。"""
        source = self.default_source()
        stats = self.stats()
        logger.info(
            "数据中台就绪：源=%s 原始消息=%d 待审=%d 已批准=%d",
            source.name if source else "未配置",
            stats["raw_messages"],
            stats["staging"]["pending"],
            stats["staging"]["approved"],
        )
        if self.settings.data_auto_ingest and source is not None and stats["raw_messages"] == 0:
            try:
                report = self.ingest(source=source)
                logger.info("数据中台自动抽取完成：%s", report.to_text())
            except Exception:  # noqa: BLE001 - 抽不到数据不能拖垮整个应用
                logger.exception("数据中台自动抽取失败（不影响其它服务）")
        return stats

    def _load_known_names(self) -> list[str]:
        path = self.settings.profile_abs_dir / "customers.json"
        if not path.exists():
            return []
        try:
            return collect_known_names(json.loads(path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001 - 词表缺失只影响姓名打码的覆盖率
            logger.warning("读取客户姓名词表失败：%s", path)
            return []

    def default_source(self) -> MessageSource | None:
        """默认数据源：优先微信库，其次 JSONL 演示语料。"""
        if self._source is not None:
            return self._source
        if self.settings.data_source_abs_path.exists():
            return WeChatSqliteSource(self.settings.data_source_abs_path)
        fallback = self.settings.policy_abs_dir / "conversations.jsonl"
        if fallback.exists():
            return JsonlSource(fallback)
        return None

    # ------------------------------------------------------------------ #
    # 能力一：抽取与清洗
    # ------------------------------------------------------------------ #
    def ingest(
        self,
        *,
        source: MessageSource | None = None,
        limit: int = 0,
        dry_run: bool = False,
    ) -> IngestReport:
        started = _now_ms()
        src = source or self.default_source()
        if src is None:
            raise SourceUnavailable(
                "未找到数据源：请先运行 scripts/seed_data.py 生成微信夹具，"
                "或把 DATA_SOURCE_PATH 指向真实微信库"
            )
        messages = list(src.iter_messages(limit=limit))
        sessions = load_sessions(messages)
        # 批次号在抽取入口生成：raw 层每条消息带上它，且与 ingest_batches 同 id，
        # "这批数据来自哪次抽取"一查便知。时间戳只到秒，同秒重跑会撞号
        # （INSERT OR REPLACE 会覆盖上一份报告），所以拼一段短随机后缀保证唯一。
        batch_id = f"batch_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex[:6]}"
        report = IngestReport(
            source=getattr(src, "name", "unknown"),
            batch_id=batch_id,
            raw_messages=len(messages),
            sessions=len(sessions),
            dry_run=dry_run,
        )

        # 跨会话共享去重集合：同一批数据里重复转发/刷屏只保留第一份
        seen: set[str] = set()
        now = _now()
        staged_new = 0
        staged_existing = 0
        for session in sessions:
            blocks = self.builder.build(session, seen_signatures=seen)
            for block in blocks:
                report.blocks_total += 1
                report.by_category[block.category] = report.by_category.get(block.category, 0) + 1
                report.masked_findings += len(block.findings)
                report.price_hits += block.price_hits
                # 所有块都落库（含被丢弃的）：漏斗/丢弃率要能从这里现算，
                # 也留住了"为什么这条被丢"的证据（category_reason）。
                if not dry_run:
                    self.store.save_block(block, created_at=now)
                if block.keepable:
                    report.blocks_kept += 1
                else:
                    report.blocks_dropped += 1
                    continue
                if dry_run:
                    continue
                _, created = self.gate.submit(block)
                if created:
                    staged_new += 1
                else:
                    staged_existing += 1

        report.staged_new = staged_new
        report.staged_existing = staged_existing
        report.elapsed_ms = _now_ms() - started
        if not dry_run:
            self.store.save_raw_messages(messages, created_at=now, batch_id=batch_id)
            self.store.save_ingest(
                batch_id=batch_id,
                report=report,
                source=getattr(src, "name", "unknown"),
                created_at=now,
            )
            self._last_report = report
        logger.info("数据中台抽取：%s", report.to_text())
        return report

    # ------------------------------------------------------------------ #
    # 能力二：人工审核
    # ------------------------------------------------------------------ #
    def review(
        self,
        staging_id: str,
        action: ReviewAction,
        *,
        reviewer: str,
        comment: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> StagedConversation:
        if action == "approve":
            return self.gate.approve(staging_id, reviewer=reviewer, comment=comment)
        if action == "reject":
            return self.gate.reject(staging_id, reviewer=reviewer, comment=comment)
        if action == "edit":
            if not messages:
                raise DataPlatformError("edit 需要提供 messages（编辑后的对话内容）")
            current = self.store.get_staged(staging_id)
            if current is None:
                raise DataPlatformError(f"暂存条目不存在：{staging_id}")
            rebuilt = [
                RawMessage(
                    msg_id=f"edit-{staging_id}-{i}",
                    session_id=current.session_id,
                    sender_name=str(m.get("sender_name") or ""),
                    role=str(m.get("role") or "customer"),  # type: ignore[arg-type]
                    content=str(m.get("content") or ""),
                    ts=float(m.get("ts") or i),
                )
                for i, m in enumerate(messages)
                if str(m.get("content") or "").strip()
            ]
            return self.gate.edit(
                staging_id, reviewer=reviewer, messages=rebuilt, comment=comment
            )
        raise DataPlatformError(f"未知审核动作：{action}")

    def list_staging(self, *, status: str = "", limit: int = 50) -> list[StagedConversation]:
        return self.store.list_staged(status=status, limit=limit)

    def get_staging(self, staging_id: str) -> StagedConversation | None:
        return self.store.get_staged(staging_id)

    def audits(self, *, staging_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        return self.store.list_audits(staging_id=staging_id, limit=limit)

    # ------------------------------------------------------------------ #
    # 能力三：导出
    # ------------------------------------------------------------------ #
    def export(self, fmt: ExportFormat) -> Any:
        return self.exporter.export(fmt)

    def list_exports(self, limit: int = 20) -> list[Any]:
        return self.store.list_exports(limit=limit)

    # ------------------------------------------------------------------ #
    # 能力四：知识产物
    # ------------------------------------------------------------------ #
    def build_knowledge(self, *, limit: int = 0) -> Any:
        """全量构建（兼容入口）：处理全部已批准数据，完成后推进水位。"""
        return self._build_knowledge(limit=limit, since=None)

    def rebuild_knowledge(self, *, incremental: bool = True, limit: int = 0) -> Any:
        """重建知识产物，默认**增量**。

        增量：从"上次构建水位"取新批准的条目，向量/文章按 source(staging_id)
        upsert，旧条目不重算；**不处理删除** —— 有条目被驳回/撤销时，
        走全量（incremental=False）清理残留。
        水位取**构建开始时刻**且构建成功后才写入：构建期间新批准的数据
        时间戳必然 >= 水位，下一轮增量不会漏。
        """
        since = self.store.get_build_watermark(_KNOWLEDGE_WATERMARK) if incremental else None
        return self._build_knowledge(limit=limit, since=since)

    def _build_knowledge(self, *, limit: int, since: str | None) -> Any:
        started = _now()
        report = self.knowledge.build(limit=limit, since=since)
        self.store.set_build_watermark(_KNOWLEDGE_WATERMARK, started)
        logger.info("数据中台知识构建（%s）：%s", "增量" if since else "全量", report.to_text())
        return report

    def search_knowledge(self, query: str, *, top_k: int = 3) -> list[dict[str, object]]:
        return self.knowledge.search(query, top_k=top_k)

    # ------------------------------------------------------------------ #
    # 能力五：供给下游（项目三/七）
    # ------------------------------------------------------------------ #
    def feed_conversations(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """已批准对话 → 项目三/七能直接消费的语料格式。

        只给 approved 且 dataset_eligible 的：
            敏感对话（投诉/维权）业务上不能拿去做"成单案例素材"，
            否则等于把客户投诉包装成营销案例 —— 这是事故而不是瑕疵。
        """
        rows: list[dict[str, Any]] = []
        for staged in self.store.approved_staged(eligible_only=True)[:limit]:
            messages = [m for m in staged.messages if m.role in ("customer", "business")]
            if not messages:
                continue
            start = messages[0].ts
            joined = "\n".join(m.content for m in messages)
            rows.append({
                "conversation_id": staged.session_id or staged.staging_id,
                "customer_id": staged.customer_id,
                "agent": "中台已审对话",
                "outcome": "won" if any(k in joined for k in _WON_MARKERS) else "unknown",
                "channel": "wecom",
                "started_at": datetime.fromtimestamp(start).isoformat(timespec="seconds")
                if start else "",
                "messages": [
                    {
                        "role": m.role,
                        "text": m.content,
                        "ts": int(max(0.0, m.ts - start)),
                    }
                    for m in messages
                ],
            })
        return rows

    # ------------------------------------------------------------------ #
    # 观测
    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, Any]:
        staging = self.store.staging_counts()
        return {
            "source": {
                "path": str(self.settings.data_source_abs_path),
                "exists": self.settings.data_source_abs_path.exists(),
            },
            "raw_messages": self.store.raw_count(),
            "sessions": self.store.raw_sessions(),
            "blocks": self.store.block_count(),
            "blocks_by_category": self.store.blocks_by_category(),
            "staging": staging,
            "approved_eligible": len(self.store.approved_staged(eligible_only=True)),
            "exports": len(self.store.list_exports(limit=1000)),
            "chunks": self.store.chunk_count(),
            "articles": self.store.article_count(),
            "article_extractor": self.store.article_extractor_counts(),
            "last_ingest": self.store.last_ingest(),
            "window_minutes": self.settings.data_window_minutes,
            "min_chars": self.settings.data_min_chars,
            "high_value_quality": self.settings.data_high_value_quality,
        }

    def pipeline(self) -> dict[str, Any]:
        """漏斗视图：每层剩多少、丢掉的是什么（给前端与面试讲数用）。"""
        stats = self.stats()
        total_blocks = stats["blocks"] or 0
        dropped = sum(
            n for c, n in stats["blocks_by_category"].items() if c not in ("normal", "high_value", "sensitive")
        )
        return {
            "raw_messages": stats["raw_messages"],
            "sessions": stats["sessions"],
            "blocks": total_blocks,
            "dropped": dropped,
            "drop_rate": round(dropped / total_blocks, 3) if total_blocks else 0.0,
            "pending": stats["staging"]["pending"],
            "approved": stats["staging"]["approved"],
            "rejected": stats["staging"]["rejected"],
            "approved_eligible": stats["approved_eligible"],
            "exports": stats["exports"],
            "chunks": stats["chunks"],
            "articles": stats["articles"],
            "by_category": {
                CATEGORY_NAMES.get(k, k): v for k, v in stats["blocks_by_category"].items()
            },
        }

    def rules(self) -> dict[str, Any]:
        """规则清单（透明性：面试/合规都能逐条对）。"""
        return {
            "masking": self.masker.rules(),
            "categories": [
                {"key": k, "name": CATEGORY_NAMES[k], "keep": k in ("normal", "high_value", "sensitive"),
                 "dataset_eligible": k != "sensitive"}
                for k in CATEGORY_NAMES
            ],
            "layers": ["clean（成块时）", "write（入库/编辑时）", "export（导出时）"],
            "window_minutes": self.settings.data_window_minutes,
        }

    # ------------------------------------------------------------------ #
    # 工具注册
    # ------------------------------------------------------------------ #
    def register_tools(self, registry: ToolRegistry) -> list[str]:
        registered: list[str] = []

        def _ingest(dry_run: bool = True, limit: int = 0) -> dict[str, Any]:
            report = self.ingest(limit=limit, dry_run=dry_run)
            return {**report.model_dump(mode="json"), "text": report.to_text()}

        registry.register(ToolSpec(
            name="data.ingest",
            description=(
                "从微信源库抽取并清洗对话：过滤 9 类噪声、三层脱敏、5 分钟切块、质量打分，"
                "可清洗的进入待审队列。dry_run=true 时只出报告不落库（推荐先跑 dry_run）。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "dry_run": {"type": "boolean", "description": "只出报告不写库，默认 true"},
                    "limit": {"type": "integer", "description": "最多抽取多少条消息（0=不限）"},
                },
                "required": [],
            },
            handler=_ingest,
            tags=("data", "ingest", "write"),
            risk_level="medium",
            idempotent=True,  # msg_id / block_id 内容派生，重跑不产生重复
            timeout_seconds=60.0,
        ))
        registered.append("data.ingest")

        def _staging(status: str = "pending", limit: int = 10) -> dict[str, Any]:
            rows = self.list_staging(status=status, limit=limit)
            return {
                "count": len(rows),
                "items": [
                    {
                        "staging_id": s.staging_id,
                        "status": s.status,
                        "category": s.category,
                        "quality": s.quality_score,
                        "turns": s.turns,
                        "dataset_eligible": s.dataset_eligible,
                        "preview": " ｜ ".join(m.content[:40] for m in s.messages[:3]),
                    }
                    for s in rows
                ],
            }

        registry.register(ToolSpec(
            name="data.list_staging",
            description="查看待审/已审的对话队列（含分类、质量分、是否可用于训练）。",
            parameters={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "pending | approved | rejected，空=全部"},
                    "limit": {"type": "integer"},
                },
                "required": [],
            },
            handler=_staging,
            tags=("data", "review", "read"),
        ))
        registered.append("data.list_staging")

        def _review(staging_id: str, action: str, reviewer: str = "", comment: str = "") -> dict:
            staged = self.review(
                staging_id, action, reviewer=reviewer or "agent", comment=comment  # type: ignore[arg-type]
            )
            return {
                "staging_id": staged.staging_id,
                "status": staged.status,
                "revision": staged.revision,
                "note": "编辑(action=edit)必须由人工在界面完成，工具不支持改写数据",
            }

        registry.register(ToolSpec(
            name="data.review",
            description=(
                "对暂存对话做审核（approve 批准 / reject 驳回）。"
                "**这是对数据资产的高风险动作**：批准即意味着该数据可进入训练集，"
                "必须经人工确认；不支持通过工具编辑内容（改写必须由人做）。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "staging_id": {"type": "string"},
                    "action": {"type": "string", "description": "approve | reject"},
                    "reviewer": {"type": "string"},
                    "comment": {"type": "string", "description": "驳回/撤销必须写理由"},
                },
                "required": ["staging_id", "action"],
            },
            handler=_review,
            tags=("data", "review", "write"),
            risk_level="high",  # 会改变"哪些数据可用于训练"，必须过 HITL
            idempotent=False,
        ))
        registered.append("data.review")

        def _export(format: str = "sharegpt") -> dict[str, Any]:
            manifest = self.export(format)  # type: ignore[arg-type]
            return manifest.model_dump(mode="json")

        registry.register(ToolSpec(
            name="data.export",
            description=(
                "导出已批准数据：sharegpt / alpaca / openai / rag_csv。"
                "导出前会再脱敏一次并校验零残留；同输入同输出（幂等复用已有产物）。"
            ),
            parameters={
                "type": "object",
                "properties": {"format": {"type": "string", "description": "sharegpt | alpaca | openai | rag_csv"}},
                "required": ["format"],
            },
            handler=_export,
            tags=("data", "export", "write"),
            risk_level="medium",
            idempotent=True,
        ))
        registered.append("data.export")

        def _search(query: str, top_k: int = 3) -> dict[str, Any]:
            hits = self.search_knowledge(query, top_k=top_k)
            return {"query": query, "count": len(hits), "hits": hits}

        registry.register(ToolSpec(
            name="data.search_knowledge",
            description="在已批准对话构建的知识库里检索（向量优先，失败降级关键词）。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer"},
                },
                "required": ["query"],
            },
            handler=_search,
            tags=("data", "knowledge", "read"),
        ))
        registered.append("data.search_knowledge")

        def _stats() -> dict[str, Any]:
            return self.pipeline()

        registry.register(ToolSpec(
            name="data.pipeline_stats",
            description="数据管线漏斗：原始消息 / 对话块 / 待审 / 已批准 / 导出 / 知识产物。",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_stats,
            tags=("data", "stats", "read"),
        ))
        registered.append("data.pipeline_stats")

        logger.info("数据中台注册工具 %d 个：%s", len(registered), registered)
        return registered

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        self.store.close()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _now_ms() -> float:
    import time

    return time.perf_counter() * 1000
