"""语音会话存储（SQLite）。

为什么语音会话必须落库，而不是留在内存里：
    1. **RTC 回调是无状态的**：云端每次 POST 过来都是一个新的 HTTP 请求，
       历史轮次只能自己存（模型侧也配了 HistoryLength，但不能依赖它兜底）；
    2. **排障要能回放**：客户投诉"AI 说错了"，得能调出那一轮的原文 + 延迟 +
       引用了哪条政策；
    3. **质检抽样**：语音对话没法像文本那样全文检索，落库后才能做关键词审计。

表结构刻意保持扁平（session / turn 两张表），因为语音链路写多读少，
不需要复杂关系；真要接数据中台，把这两张表同步过去即可。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from agent_platform.logging_config import get_logger
from voice_service.domain import LatencyBreakdown, VoiceSession, VoiceTurn

logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_sessions (
    session_id  TEXT PRIMARY KEY,
    scene_id    TEXT NOT NULL,
    room_id     TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    bot_user_id TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'running',
    started_at  INTEGER NOT NULL DEFAULT 0,
    ended_at    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_voice_sessions_room ON voice_sessions(room_id, status);

CREATE TABLE IF NOT EXISTS voice_turns (
    session_id     TEXT NOT NULL,
    round_id       INTEGER NOT NULL,
    user_text      TEXT NOT NULL DEFAULT '',
    answer_text    TEXT NOT NULL DEFAULT '',
    refused        INTEGER NOT NULL DEFAULT 0,
    needs_human    INTEGER NOT NULL DEFAULT 0,
    interrupted    INTEGER NOT NULL DEFAULT 0,
    citations      TEXT NOT NULL DEFAULT '[]',
    prompt_version TEXT NOT NULL DEFAULT '',
    latency        TEXT NOT NULL DEFAULT '{}',
    at             INTEGER NOT NULL DEFAULT 0,
    emotion        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (session_id, round_id)
);
"""


class VoiceSessionStore:
    """会话与轮次存储。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # 为什么用 threading.Lock 而不是 asyncio.Lock：调用方既有 async 路径
        #（api.py 的 SSE 回调经 run_in_threadpool 在线程池里跑 stream_reply），
        # 也有纯线程路径（FastAPI 同步路由、打断收口），线程锁才能同时护住两类入口。
        # sqlite3 连接虽以 check_same_thread=False 允许跨线程共享，
        # 但"查轮次号 → INSERT → COMMIT"是三条语句，必须整体串行，
        # 否则并发回调会拿到同一个轮次号，INSERT OR REPLACE 把先落库的覆盖掉。
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        logger.info("语音会话库就绪：%s", path)

    def close(self) -> None:
        """关闭数据库连接。

        进程退出、测试结束、以及 Windows 下删除临时目录前都必须调用 ——
        Windows 不允许删除仍被打开的文件句柄。
        """
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - 已关闭时重复调用不应报错
            logger.debug("语音会话库关闭时忽略异常", exc_info=True)

    # ------------------------------------------------------------------ #
    # 会话
    # ------------------------------------------------------------------ #
    def create(
        self, *, scene_id: str, room_id: str, user_id: str, bot_user_id: str = ""
    ) -> VoiceSession:
        session = VoiceSession(
            session_id=f"vs_{uuid.uuid4().hex[:12]}",
            scene_id=scene_id,
            room_id=room_id,
            user_id=user_id,
            bot_user_id=bot_user_id,
            status="running",
            started_at=int(time.time()),
        )
        with self._lock:  # 写路径统一串行：共享连接上的事务边界不能被并发请求交错
            self._conn.execute(
                "INSERT OR REPLACE INTO voice_sessions"
                "(session_id, scene_id, room_id, user_id, bot_user_id, status, started_at, ended_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    session.session_id,
                    session.scene_id,
                    session.room_id,
                    session.user_id,
                    session.bot_user_id,
                    session.status,
                    session.started_at,
                    0,
                ),
            )
            self._conn.commit()
        return session

    def stop(self, session_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE voice_sessions SET status='stopped', ended_at=? WHERE session_id=?",
                (int(time.time()), session_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def get(self, session_id: str) -> VoiceSession | None:
        row = self._conn.execute(
            "SELECT * FROM voice_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return self._to_session(row)

    def find_running_by_room(self, room_id: str) -> VoiceSession | None:
        row = self._conn.execute(
            "SELECT * FROM voice_sessions WHERE room_id=? AND status='running'"
            " ORDER BY started_at DESC LIMIT 1",
            (room_id,),
        ).fetchone()
        return self._to_session(row) if row else None

    def latest_running(self) -> VoiceSession | None:
        """最近一场还在进行的会话（RTC 回调没带 room_id 时的兜底定位）。"""
        row = self._conn.execute(
            "SELECT * FROM voice_sessions WHERE status='running' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return self._to_session(row) if row else None

    def list_sessions(self, *, limit: int = 20) -> list[VoiceSession]:
        rows = self._conn.execute(
            "SELECT * FROM voice_sessions ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._to_session(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 轮次
    # ------------------------------------------------------------------ #
    def append_turn(self, session_id: str, turn: VoiceTurn) -> int:
        """落库一轮对话，轮次号由存储层在同一临界区内原子分配并返回。

        为什么不让调用方带着轮次号直接落库：实时链路上 ASR 回调、LLM 回调、
        打断收口可能并发写同一场会话，各自先查 next_round() 再落库会撞号
        （都算出同一个"下一轮"），INSERT OR REPLACE 会把先落库的那轮覆盖掉
        —— 客户历史凭空少一轮。把"分配轮次号 + 写入 + 提交"收进同一把锁，
        落库顺序即轮次顺序；LLM 生成不进临界区，锁只护写路径，不拖慢流式吐字。
        传入的 turn.round_id 仅作为生成期的临时展示值，以本方法返回值为准。
        """
        with self._lock:
            round_id = self._next_round(session_id)
            self._conn.execute(
                "INSERT OR REPLACE INTO voice_turns"
                "(session_id, round_id, user_text, answer_text, refused, needs_human, interrupted,"
                " citations, prompt_version, latency, at, emotion) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    round_id,
                    turn.user_text,
                    turn.answer_text,
                    int(turn.refused),
                    int(turn.needs_human),
                    int(turn.interrupted),
                    json.dumps(turn.citations, ensure_ascii=False),
                    turn.prompt_version,
                    json.dumps(turn.latency.model_dump(mode="json"), ensure_ascii=False),
                    turn.at,
                    turn.emotion,
                ),
            )
            self._conn.commit()
            return round_id

    def turns(self, session_id: str) -> list[VoiceTurn]:
        rows = self._conn.execute(
            "SELECT * FROM voice_turns WHERE session_id=? ORDER BY round_id", (session_id,)
        ).fetchall()
        return [self._to_turn(r) for r in rows]

    def next_round(self, session_id: str) -> int:
        """查询下一轮次号。与写路径共用一把锁，读到的 MAX 不会被在写的轮次干扰。"""
        with self._lock:
            return self._next_round(session_id)

    def _next_round(self, session_id: str) -> int:
        """轮次号分配（调用方必须已持有 _lock：锁不可重入，避免自死锁）。"""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(round_id), 0) AS m FROM voice_turns WHERE session_id=?",
            (session_id,),
        ).fetchone()
        return int(row["m"]) + 1

    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, Any]:
        sessions = self._conn.execute("SELECT COUNT(*) AS c FROM voice_sessions").fetchone()["c"]
        running = self._conn.execute(
            "SELECT COUNT(*) AS c FROM voice_sessions WHERE status='running'"
        ).fetchone()["c"]
        turns = self._conn.execute("SELECT COUNT(*) AS c FROM voice_turns").fetchone()["c"]
        refused = self._conn.execute(
            "SELECT COUNT(*) AS c FROM voice_turns WHERE refused=1"
        ).fetchone()["c"]
        handoff = self._conn.execute(
            "SELECT COUNT(*) AS c FROM voice_turns WHERE needs_human=1"
        ).fetchone()["c"]
        return {
            "sessions": int(sessions),
            "running": int(running),
            "turns": int(turns),
            "refused": int(refused),
            "handoff": int(handoff),
        }

    # ------------------------------------------------------------------ #
    def _to_session(self, row: sqlite3.Row) -> VoiceSession:
        session = VoiceSession(
            session_id=row["session_id"],
            scene_id=row["scene_id"],
            room_id=row["room_id"],
            user_id=row["user_id"],
            bot_user_id=row["bot_user_id"],
            status=row["status"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
        )
        session.turns = self.turns(session.session_id)
        return session

    @staticmethod
    def _to_turn(row: sqlite3.Row) -> VoiceTurn:
        try:
            latency = LatencyBreakdown(**json.loads(row["latency"] or "{}"))
        except Exception:  # noqa: BLE001
            latency = LatencyBreakdown()
        return VoiceTurn(
            round_id=row["round_id"],
            user_text=row["user_text"],
            answer_text=row["answer_text"],
            refused=bool(row["refused"]),
            needs_human=bool(row["needs_human"]),
            interrupted=bool(row["interrupted"]),
            citations=json.loads(row["citations"] or "[]"),
            prompt_version=row["prompt_version"],
            latency=latency,
            at=row["at"],
            emotion=row["emotion"],
        )
