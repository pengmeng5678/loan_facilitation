"""陪练会话持久化（SQLite）。

为什么要落盘而不是只放内存：
    陪练是"长会话"——学员今天练一半、明天接着练是常态，
    而且主管要能**回看整场表现**。所以会话必须持久化（对应简历里的"Checkpointer 续练与复盘回放"）。
    这里用 SQLite 做到零依赖可运行；生产可换 PostgreSQL（同一套接口）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from agent_platform.logging_config import get_logger
from coaching.domain import Persona, RoleplaySession, ScoreCard, SessionMessage

logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    persona_id   TEXT NOT NULL,
    trainee      TEXT NOT NULL,
    status       TEXT NOT NULL,
    persona_json TEXT NOT NULL,
    turn         INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    finished_at  TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    text       TEXT NOT NULL,
    ts         REAL NOT NULL DEFAULT 0,
    seq        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
CREATE TABLE IF NOT EXISTS scorecards (
    session_id     TEXT PRIMARY KEY,
    payload_json   TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
"""


class SessionStore:
    """陪练会话仓储。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：FastAPI 的线程池会跨线程使用连接
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        logger.info("陪练会话库就绪：%s", self.db_path)

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    def create(self, session_id: str, persona: Persona, trainee: str) -> RoleplaySession:
        created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions(session_id, persona_id, trainee, status, persona_json, turn, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (session_id, persona.persona_id, trainee, "active", persona.model_dump_json(), 0, created),
            )
            self._conn.commit()
        return RoleplaySession(
            session_id=session_id, persona=persona, trainee=trainee,
            status="active", messages=[], turn=0, created_at=created,
        )

    def append_message(self, session_id: str, message: SessionMessage, turn: int) -> None:
        with self._lock:
            seq = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
            self._conn.execute(
                "INSERT INTO messages(session_id, role, text, ts, seq) VALUES (?,?,?,?,?)",
                (session_id, message.role, message.text, message.ts, seq),
            )
            self._conn.execute(
                "UPDATE sessions SET turn = ? WHERE session_id = ?", (turn, session_id)
            )
            self._conn.commit()

    def finish(self, session_id: str) -> None:
        finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET status = 'finished', finished_at = ? WHERE session_id = ?",
                (finished, session_id),
            )
            self._conn.commit()

    def save_scorecard(self, card: ScoreCard) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO scorecards(session_id, payload_json, created_at) VALUES (?,?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET payload_json=excluded.payload_json",
                (card.session_id, card.model_dump_json(), card.created_at),
            )
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    def get(self, session_id: str) -> RoleplaySession | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            msg_rows = self._conn.execute(
                "SELECT role, text, ts FROM messages WHERE session_id = ? ORDER BY seq", (session_id,)
            ).fetchall()

        persona = Persona.model_validate_json(row["persona_json"])
        return RoleplaySession(
            session_id=row["session_id"],
            persona=persona,
            trainee=row["trainee"],
            status=row["status"],
            messages=[SessionMessage(role=m["role"], text=m["text"], ts=m["ts"]) for m in msg_rows],
            turn=row["turn"],
            created_at=row["created_at"],
            finished_at=row["finished_at"],
        )

    def get_scorecard(self, session_id: str) -> ScoreCard | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM scorecards WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        return ScoreCard.model_validate_json(row["payload_json"])

    def list_sessions(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, persona_id, trainee, status, turn, created_at, finished_at"
                " FROM sessions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def replay(self, session_id: str) -> list[dict]:
        """复盘回放：按顺序返回全部消息。"""
        session = self.get(session_id)
        if session is None:
            return []
        return [
            {"role": m.role, "text": m.text, "ts": m.ts} for m in session.messages
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _json_default(obj):  # pragma: no cover - 仅兜底
    return str(obj)


def _dumps(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, default=_json_default)
