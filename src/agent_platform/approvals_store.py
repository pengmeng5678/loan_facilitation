"""审批单的 SQLite 持久化。

为什么需要它：`InMemoryApprovalStore` 只活在单个进程里，一旦 uvicorn 多 worker
或进程重启，审批中心看到的待办就"凭空消失"，而图却仍等着 resume。
把审批单落到共享 SQLite 后：
    - 重启不丢待办；
    - 多个 worker 看到同一份待办；
    - 审批结论（谁批的、什么时候批的）天然可审计。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from agent_platform.hitl import ApprovalRequest
from agent_platform.logging_config import get_logger

logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_approvals (
    request_id     TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    thread_id      TEXT NOT NULL,
    tool           TEXT NOT NULL,
    subject_hash   TEXT NOT NULL DEFAULT '',
    arguments_json TEXT NOT NULL,
    risk_level     TEXT NOT NULL,
    reason         TEXT NOT NULL,
    status         TEXT NOT NULL,
    created_at     REAL NOT NULL,
    expires_at     REAL NOT NULL,
    decided_by     TEXT,
    decided_at     REAL,
    comment        TEXT,
    escalated_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_agent_approvals_status ON agent_approvals(status, expires_at);
"""


class SqliteApprovalStore:
    """跨进程共享的审批单存储。SQL 语法标准，换 PostgreSQL 只需换驱动。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        logger.info("审批库就绪：%s", self.db_path)

    def get(self, request_id: str) -> ApprovalRequest | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agent_approvals WHERE request_id=?", (request_id,)
            ).fetchone()
        return _row_to_request(row) if row is not None else None

    def put(self, req: ApprovalRequest) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO agent_approvals
                    (request_id, run_id, thread_id, tool, subject_hash, arguments_json,
                     risk_level, reason, status, created_at, expires_at,
                     decided_by, decided_at, comment, escalated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(request_id) DO UPDATE SET
                    status=excluded.status,
                    decided_by=excluded.decided_by,
                    decided_at=excluded.decided_at,
                    comment=excluded.comment,
                    escalated_at=excluded.escalated_at
                """,
                (
                    req.request_id,
                    req.run_id,
                    req.thread_id,
                    req.tool,
                    req.subject_hash,
                    json.dumps(req.arguments, ensure_ascii=False, default=str),
                    req.risk_level,
                    req.reason,
                    req.status,
                    req.created_at,
                    req.expires_at,
                    req.decided_by,
                    req.decided_at,
                    req.comment,
                    req.escalated_at,
                ),
            )

    def list_all(self, limit: int = 200) -> list[ApprovalRequest]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agent_approvals ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_request(r) for r in rows]


def _row_to_request(row: sqlite3.Row) -> ApprovalRequest:
    return ApprovalRequest(
        request_id=row["request_id"],
        run_id=row["run_id"],
        thread_id=row["thread_id"],
        tool=row["tool"],
        arguments=json.loads(row["arguments_json"] or "{}"),
        risk_level=row["risk_level"],
        reason=row["reason"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        status=row["status"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
        comment=row["comment"],
        subject_hash=row["subject_hash"] or "",
        escalated_at=row["escalated_at"],
    )
