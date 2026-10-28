"""数据中台的持久化（SQLite）。

九张表对应管线上的九个事实，**每一张都回答一个具体问题**：

    raw_messages           原始消息（不可变层）      "原来是什么样"（能否回溯）
    conversation_blocks    清洗后的对话块            "清洗逻辑跑出了什么"
    staging_conversations  人工审核闸门              "哪些数据被批准用于下游"
    review_audit           审核流水                  "谁在什么时候批了什么"（可审计）
    exports                导出记录                  "这批模型是用哪批数据训的"
    knowledge_chunks       向量切片                  "检索命中的是原始对话的哪一段"
    knowledge_articles     结构化知识                "话术/场景从哪条对话提炼的"
    ingest_batches         抽取批次                  "每次清洗的输入输出规模"
    build_watermarks       增量构建水位              "上次知识构建处理到哪了"

**为什么原始层只增不改**：清洗规则会迭代，原始数据一旦被覆盖就不可追溯。
副作用是表会变大，但换来的是"任何一批导出都能追溯到原始消息"，
对强监管行业这是必须付的成本。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from agent_platform.logging_config import get_logger
from data_platform.domain import (
    ConversationBlock,
    ExportManifest,
    IngestReport,
    KnowledgeArticle,
    KnowledgeChunk,
    RawMessage,
    ReviewRecord,
    StagedConversation,
)

logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_messages (
    msg_id       TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    role         TEXT,
    sender_wxid  TEXT,
    sender_name  TEXT,
    content      TEXT,
    ts           REAL,
    msg_type     TEXT,
    is_group     INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL,
    batch_id     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_raw_session ON raw_messages(session_id, ts);

CREATE TABLE IF NOT EXISTS conversation_blocks (
    block_id        TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    customer_id     TEXT,
    category        TEXT,
    quality         REAL DEFAULT 0,
    dataset_eligible INTEGER DEFAULT 1,
    payload_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blocks_category ON conversation_blocks(category);

CREATE TABLE IF NOT EXISTS staging_conversations (
    staging_id      TEXT PRIMARY KEY,
    block_id        TEXT NOT NULL,
    status          TEXT NOT NULL,
    revision        INTEGER DEFAULT 1,
    category        TEXT,
    quality         REAL DEFAULT 0,
    dataset_eligible INTEGER DEFAULT 1,
    payload_json    TEXT NOT NULL,
    reviewer        TEXT,
    comment         TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    decided_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_staging_status ON staging_conversations(status);

CREATE TABLE IF NOT EXISTS review_audit (
    audit_id     TEXT PRIMARY KEY,
    staging_id   TEXT NOT NULL,
    action       TEXT NOT NULL,
    from_status  TEXT,
    to_status    TEXT,
    reviewer     TEXT,
    comment      TEXT,
    revision     INTEGER DEFAULT 1,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_staging ON review_audit(staging_id);

CREATE TABLE IF NOT EXISTS exports (
    export_id    TEXT PRIMARY KEY,
    export_key   TEXT UNIQUE,
    format       TEXT NOT NULL,
    path         TEXT,
    rows         INTEGER DEFAULT 0,
    bytes        INTEGER DEFAULT 0,
    content_hash TEXT,
    watermark    TEXT,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_chunks (
    chunk_id     TEXT PRIMARY KEY,
    staging_id   TEXT NOT NULL,
    session_id   TEXT,
    scene        TEXT,
    quality      REAL DEFAULT 0,
    text         TEXT,
    vector_json  TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_staging ON knowledge_chunks(staging_id);

CREATE TABLE IF NOT EXISTS knowledge_articles (
    article_id   TEXT PRIMARY KEY,
    staging_id   TEXT NOT NULL,
    scene        TEXT,
    confidence   REAL DEFAULT 0,
    extractor    TEXT,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_batches (
    batch_id    TEXT PRIMARY KEY,
    source      TEXT,
    report_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS build_watermarks (
    name       TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""


class DataPlatformStore:
    """SQLite 持久化门面。线程安全（单连接 + 锁，与 flow_ops/marketing 同款）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """轻量迁移：先探测再加列，重复启动幂等（旧库打开即升级，不搬数据）。

        为什么不直接改 _SCHEMA 就完事：CREATE TABLE IF NOT EXISTS 对已存在的
        旧表不会补新列，必须显式 ALTER —— 只加列、不改不删，老数据默认值兜底。
        """
        cols = {str(r[1]) for r in self._conn.execute("PRAGMA table_info(raw_messages)")}
        if cols and "batch_id" not in cols:
            self._conn.execute("ALTER TABLE raw_messages ADD COLUMN batch_id TEXT NOT NULL DEFAULT ''")
            logger.info("raw_messages 补齐 batch_id 列（旧库平滑升级）")

    # ------------------------------------------------------------------ #
    # 原始层
    # ------------------------------------------------------------------ #
    def save_raw_messages(
        self, messages: list[RawMessage], *, created_at: str, batch_id: str = ""
    ) -> int:
        """只增不改：重复 msg_id 直接忽略（保证原始层不可变）。

        batch_id 只是"这批消息来自哪次抽取"的标识，**不参与去重**：
        msg_id 去重语义保持不变，同一消息重复抽取仍不会产生第二条。
        """
        if not messages:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO raw_messages "
                "(msg_id, session_id, role, sender_wxid, sender_name, content, ts, msg_type, is_group, "
                " created_at, batch_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (m.msg_id, m.session_id, m.role, m.sender_wxid, m.sender_name,
                     m.content, m.ts, m.msg_type, 1 if m.is_group else 0, created_at, batch_id)
                    for m in messages
                ],
            )
            self._conn.commit()
            return self._conn.total_changes - before

    def raw_count(self) -> int:
        return self._scalar("SELECT COUNT(*) FROM raw_messages")

    def raw_sessions(self) -> int:
        return self._scalar("SELECT COUNT(DISTINCT session_id) FROM raw_messages")

    def raw_batch_ids(self) -> list[str]:
        """出现过的批次号（可观测：能回答"库里的数据来自哪几次抽取"）。"""
        rows = self._rows("SELECT DISTINCT batch_id FROM raw_messages WHERE batch_id<>'' ORDER BY batch_id")
        return [str(r["batch_id"]) for r in rows]

    # ------------------------------------------------------------------ #
    # 对话块
    # ------------------------------------------------------------------ #
    def save_block(self, block: ConversationBlock, *, created_at: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO conversation_blocks "
                "(block_id, session_id, customer_id, category, quality, dataset_eligible, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (block.block_id, block.session_id, block.customer_id, block.category,
                 block.quality_score, 1 if block.dataset_eligible else 0,
                 block.model_dump_json(), created_at),
            )
            self._conn.commit()

    def block_count(self) -> int:
        return self._scalar("SELECT COUNT(*) FROM conversation_blocks")

    def blocks_by_category(self) -> dict[str, int]:
        rows = self._rows("SELECT category, COUNT(*) AS n FROM conversation_blocks GROUP BY category")
        return {str(r["category"]): int(r["n"]) for r in rows}

    # ------------------------------------------------------------------ #
    # 暂存区
    # ------------------------------------------------------------------ #
    def submit_staged(self, staged: StagedConversation) -> bool:
        """幂等提交：已存在则不动（返回 False）。

        注意"不动"是刻意的：一条已经 approved 的数据，重跑清洗不应该
        被打回 pending —— 那会让下游导出突然少数据，且人工要重审一遍。
        """
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO staging_conversations "
                "(staging_id, block_id, status, revision, category, quality, dataset_eligible, "
                " payload_json, reviewer, comment, created_at, updated_at, decided_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (staged.staging_id, staged.block_id, staged.status, staged.revision,
                 staged.category, staged.quality_score,
                 1 if staged.dataset_eligible else 0, staged.model_dump_json(),
                 staged.reviewer, staged.comment, staged.created_at,
                 staged.updated_at, staged.decided_at or None),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def update_staged(self, staged: StagedConversation) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE staging_conversations SET status=?, revision=?, category=?, quality=?, "
                "dataset_eligible=?, payload_json=?, reviewer=?, comment=?, updated_at=?, decided_at=? "
                "WHERE staging_id=?",
                (staged.status, staged.revision, staged.category, staged.quality_score,
                 1 if staged.dataset_eligible else 0, staged.model_dump_json(),
                 staged.reviewer, staged.comment, staged.updated_at,
                 staged.decided_at or None, staged.staging_id),
            )
            self._conn.commit()

    def get_staged(self, staging_id: str) -> StagedConversation | None:
        row = self._one("SELECT payload_json FROM staging_conversations WHERE staging_id=?", (staging_id,))
        return StagedConversation.model_validate_json(row["payload_json"]) if row else None

    def list_staged(self, *, status: str = "", limit: int = 50) -> list[StagedConversation]:
        if status:
            rows = self._rows(
                "SELECT payload_json FROM staging_conversations WHERE status=? "
                "ORDER BY quality DESC, created_at ASC LIMIT ?",
                (status, limit),
            )
        else:
            rows = self._rows(
                "SELECT payload_json FROM staging_conversations ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        return [StagedConversation.model_validate_json(r["payload_json"]) for r in rows]

    def approved_staged(self, *, eligible_only: bool = True) -> list[StagedConversation]:
        sql = "SELECT payload_json FROM staging_conversations WHERE status='approved'"
        if eligible_only:
            sql += " AND dataset_eligible=1"
        sql += " ORDER BY staging_id ASC"
        return [StagedConversation.model_validate_json(r["payload_json"]) for r in self._rows(sql)]

    def approved_staged_since(self, since: str, *, eligible_only: bool = True) -> list[StagedConversation]:
        """上次构建水位之后批准（或重新批准）的条目 —— 增量构建的取数口径。

        为什么用 >= 而不是 >：updated_at 与水位同秒时 > 会把临界条目漏掉，
        而重复处理的代价是零 —— 下游 chunk/文章都按 staging_id upsert，重跑幂等。
        since 传空串（首次增量、还没有水位）时等价于全量。
        """
        sql = "SELECT payload_json FROM staging_conversations WHERE status='approved' AND updated_at>=?"
        if eligible_only:
            sql += " AND dataset_eligible=1"
        sql += " ORDER BY staging_id ASC"
        return [StagedConversation.model_validate_json(r["payload_json"]) for r in self._rows(sql, (since,))]

    def get_build_watermark(self, name: str) -> str:
        """读取构建水位（没有则返回空串，表示"从没构建过"）。"""
        row = self._one("SELECT created_at FROM build_watermarks WHERE name=?", (name,))
        return str(row["created_at"]) if row else ""

    def set_build_watermark(self, name: str, ts: str) -> None:
        """记录构建水位（构建成功后由 service 层写入，构建失败不推进）。"""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO build_watermarks (name, created_at) VALUES (?, ?)", (name, ts)
            )
            self._conn.commit()

    def staging_counts(self) -> dict[str, int]:
        rows = self._rows("SELECT status, COUNT(*) AS n FROM staging_conversations GROUP BY status")
        counts = {"pending": 0, "approved": 0, "rejected": 0}
        for row in rows:
            counts[str(row["status"])] = int(row["n"])
        counts["total"] = sum(counts.values())
        return counts

    # ------------------------------------------------------------------ #
    # 审核流水
    # ------------------------------------------------------------------ #
    def append_audit(self, record: ReviewRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO review_audit "
                "(audit_id, staging_id, action, from_status, to_status, reviewer, comment, revision, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.audit_id, record.staging_id, record.action, record.from_status,
                 record.to_status, record.reviewer, record.comment, record.revision, record.created_at),
            )
            self._conn.commit()

    def list_audits(self, *, staging_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        # 同一秒内的多条审计要靠 rowid 兜底排序 —— 否则 created_at 相同，
        # "最近一条"是不确定的（测试与前端都会看到随机顺序）
        if staging_id:
            rows = self._rows(
                "SELECT * FROM review_audit WHERE staging_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (staging_id, limit),
            )
        else:
            rows = self._rows(
                "SELECT * FROM review_audit ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 导出
    # ------------------------------------------------------------------ #
    def save_export(self, manifest: ExportManifest, *, export_key: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO exports "
                "(export_id, export_key, format, path, rows, bytes, content_hash, watermark, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (manifest.export_id, export_key, manifest.format, manifest.path, manifest.rows,
                 manifest.bytes, manifest.content_hash, manifest.watermark,
                 manifest.model_dump_json(), manifest.created_at),
            )
            self._conn.commit()

    def find_export(self, export_key: str) -> ExportManifest | None:
        row = self._one("SELECT payload_json FROM exports WHERE export_key=?", (export_key,))
        return ExportManifest.model_validate_json(row["payload_json"]) if row else None

    def list_exports(self, limit: int = 20) -> list[ExportManifest]:
        rows = self._rows("SELECT payload_json FROM exports ORDER BY created_at DESC LIMIT ?", (limit,))
        return [ExportManifest.model_validate_json(r["payload_json"]) for r in rows]

    # ------------------------------------------------------------------ #
    # 知识产物
    # ------------------------------------------------------------------ #
    def replace_chunks(self, chunks: list[KnowledgeChunk], *, created_at: str) -> int:
        """按 staging_id 覆盖：重建知识库时不残留旧切片。"""
        if not chunks:
            return 0
        staging_ids = sorted({c.staging_id for c in chunks})
        with self._lock:
            self._conn.executemany(
                "DELETE FROM knowledge_chunks WHERE staging_id=?", [(sid,) for sid in staging_ids]
            )
            self._conn.executemany(
                "INSERT OR REPLACE INTO knowledge_chunks "
                "(chunk_id, staging_id, session_id, scene, quality, text, vector_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (c.chunk_id, c.staging_id, c.session_id, c.scene, c.quality_score,
                     c.text, json.dumps(c.vector), created_at)
                    for c in chunks
                ],
            )
            self._conn.commit()
        return len(chunks)

    def list_chunks(self, limit: int = 1000) -> list[KnowledgeChunk]:
        rows = self._rows(
            "SELECT chunk_id, staging_id, session_id, scene, quality, text, vector_json "
            "FROM knowledge_chunks ORDER BY chunk_id LIMIT ?",
            (limit,),
        )
        return [
            KnowledgeChunk(
                chunk_id=r["chunk_id"], staging_id=r["staging_id"], session_id=r["session_id"] or "",
                scene=r["scene"] or "", quality_score=float(r["quality"] or 0), text=r["text"] or "",
                vector=json.loads(r["vector_json"] or "[]"),
            )
            for r in rows
        ]

    def chunk_count(self) -> int:
        return self._scalar("SELECT COUNT(*) FROM knowledge_chunks")

    def save_articles(self, articles: list[KnowledgeArticle], *, created_at: str) -> int:
        if not articles:
            return 0
        staging_ids = sorted({a.staging_id for a in articles})
        with self._lock:
            self._conn.executemany(
                "DELETE FROM knowledge_articles WHERE staging_id=?", [(sid,) for sid in staging_ids]
            )
            self._conn.executemany(
                "INSERT OR REPLACE INTO knowledge_articles "
                "(article_id, staging_id, scene, confidence, extractor, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (a.article_id, a.staging_id, a.scene, a.confidence, a.extractor,
                     a.model_dump_json(), created_at)
                    for a in articles
                ],
            )
            self._conn.commit()
        return len(articles)

    def list_articles(self, limit: int = 200) -> list[KnowledgeArticle]:
        rows = self._rows(
            "SELECT payload_json FROM knowledge_articles ORDER BY confidence DESC, article_id LIMIT ?",
            (limit,),
        )
        return [KnowledgeArticle.model_validate_json(r["payload_json"]) for r in rows]

    def article_count(self) -> int:
        return self._scalar("SELECT COUNT(*) FROM knowledge_articles")

    def article_extractor_counts(self) -> dict[str, int]:
        rows = self._rows(
            "SELECT extractor, COUNT(*) AS n FROM knowledge_articles GROUP BY extractor"
        )
        return {str(r["extractor"]): int(r["n"]) for r in rows}

    # ------------------------------------------------------------------ #
    # 批次
    # ------------------------------------------------------------------ #
    def save_ingest(self, batch_id: str, report: IngestReport, *, source: str, created_at: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO ingest_batches (batch_id, source, report_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (batch_id, source, report.model_dump_json(), created_at),
            )
            self._conn.commit()

    def last_ingest(self) -> dict[str, Any] | None:
        # 同秒多批靠 rowid 兜底排序（与 list_audits 同款）：
        # batch_id 唯一化之后，同秒两轮抽取是两行记录，"最近一批"必须确定
        row = self._one("SELECT * FROM ingest_batches ORDER BY created_at DESC, rowid DESC LIMIT 1")
        return dict(row) if row else None

    # ------------------------------------------------------------------ #
    # 基础
    # ------------------------------------------------------------------ #
    def reset(self) -> None:
        """清空全部数据（演示重置用）。

        为什么用 DELETE 而不是删库文件：
            线上/开发服务器可能正开着这个库，删文件会让它继续往
            "已删除的 inode" 写，数据看起来凭空消失；
            SQLite 的并发写由它自己加锁，清表是安全的。
        """
        tables = (
            "raw_messages", "conversation_blocks", "staging_conversations",
            "review_audit", "exports", "knowledge_chunks", "knowledge_articles",
            "ingest_batches", "build_watermarks",
        )
        with self._lock:
            for table in tables:
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.commit()
        logger.info("数据中台已清空（%d 张表）", len(tables))

    def _scalar(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
