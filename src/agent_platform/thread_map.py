"""thread_id → Agent 名的映射存储。

为什么需要持久化：
    HITL 审批可能挂起几小时甚至过夜，期间服务会重启 / 多副本间请求会漂移。
    如果映射只放在内存 dict，重启后 resume() 就只能遍历所有已注册 Agent
    逐个 get_state 反查——Agent 多了又慢又难看，而且没有任何实例能"认领"
    这个映射。落一张小表，恢复路径就变成 O(1) 的一次点查。

设计取舍：
    * 单文件小表（默认 .data/runtime/thread_map.sqlite），不复用 checkpointer
      的库文件——LangGraph 升级可能动它的 schema，映射表要有自己的生命周期；
    * postgres 后端场景：映射先落本地 SQLite（进程内 O(1)），跨副本共享映射
      属于生产替换点（换 PG 表或 Redis hash，接口不变）；
    * checkpointer_backend=memory（纯内存测试）时不落盘，行为与旧版一致。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from agent_platform.logging_config import get_logger

logger = get_logger(__name__)


class ThreadAgentMap:
    """thread_id → agent 名的映射（可落盘，线程安全）。"""

    def __init__(self, sqlite_path: Path | None = None) -> None:
        self._sqlite_path = sqlite_path
        self._lock = threading.Lock()
        self._memory: dict[str, str] = {}
        self._conn: Any = None
        if sqlite_path is not None:
            self._init_sqlite(sqlite_path)

    def _init_sqlite(self, path: Path) -> None:
        import sqlite3

        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS thread_agent_map (
                thread_id  TEXT PRIMARY KEY,
                agent      TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        self._conn.commit()
        logger.info("thread 映射已持久化：%s", path)

    def put(self, thread_id: str, agent: str) -> None:
        """记录/更新映射（invoke 时写；反查命中时也回写缓存）。"""
        with self._lock:
            self._memory[thread_id] = agent
            if self._conn is not None:
                self._conn.execute(
                    "INSERT INTO thread_agent_map(thread_id, agent, updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(thread_id) DO UPDATE SET agent=excluded.agent, updated_at=excluded.updated_at",
                    (thread_id, agent, time.time()),
                )
                self._conn.commit()

    def get(self, thread_id: str) -> str | None:
        """查映射。内存未命中时回源 SQLite（跨重启的恢复路径）。"""
        with self._lock:
            if thread_id in self._memory:
                return self._memory[thread_id]
            if self._conn is None:
                return None
            row = self._conn.execute(
                "SELECT agent FROM thread_agent_map WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        if row is not None:
            # 回源命中写回内存，后续查询 O(1)
            self._memory[thread_id] = str(row[0])
            return str(row[0])
        return None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
