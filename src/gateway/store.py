"""网关持久化（SQLite）。

存两样东西：
    cost_records —— 成本台账，支撑"按会话/按任务追溯花了多少钱"（跨进程重启仍在）
    eval_runs    —— 评测运行记录，支撑"这次和上次比是变好还是变差"

为什么用 SQLite：零依赖、开箱即用，且接口就是最普通的 SQL —— 生产换
PostgreSQL / ClickHouse 时改动极小（成本数据量大时其实更适合列存）。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from agent_platform.logging_config import get_logger
from gateway.domain import CostRecord, EvalReport

logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_records (
    record_id     TEXT PRIMARY KEY,
    run_id        TEXT,
    thread_id     TEXT,
    scope         TEXT,
    task          TEXT,
    tier          TEXT,
    model         TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens  INTEGER,
    cost          REAL,
    baseline_cost REAL,
    saved         REAL,
    cache_status  TEXT,
    cache_policy  TEXT,
    trimmed_tokens INTEGER,
    fallbacks     INTEGER,
    latency_ms    REAL,
    ok            INTEGER,
    error         TEXT,
    ts            TEXT
);
CREATE INDEX IF NOT EXISTS idx_cost_thread ON cost_records(thread_id);
CREATE INDEX IF NOT EXISTS idx_cost_task ON cost_records(task);
CREATE TABLE IF NOT EXISTS eval_runs (
    eval_id       TEXT PRIMARY KEY,
    scene         TEXT,
    total         INTEGER,
    passed        INTEGER,
    failed        INTEGER,
    score         REAL,
    judge_source  TEXT,
    duration_ms   REAL,
    token_cost    REAL,
    payload_json  TEXT,
    created_at    TEXT
);
"""


class GatewayStore:
    """网关仓储。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False：FastAPI 线程池会跨线程使用同一连接
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        logger.info("网关仓储就绪：%s", self.db_path)

    # ------------------------------------------------------------------ #
    # 成本记录
    # ------------------------------------------------------------------ #
    def add_cost(self, rec: CostRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cost_records("
                "record_id, run_id, thread_id, scope, task, tier, model,"
                " prompt_tokens, completion_tokens, total_tokens, cost, baseline_cost, saved,"
                " cache_status, cache_policy, trimmed_tokens, fallbacks, latency_ms, ok, error, ts"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rec.record_id, rec.run_id, rec.thread_id, rec.scope, rec.task, rec.tier, rec.model,
                    rec.prompt_tokens, rec.completion_tokens, rec.total_tokens,
                    rec.cost, rec.baseline_cost, rec.saved,
                    rec.cache_status, rec.cache_policy, rec.trimmed_tokens, rec.fallbacks,
                    rec.latency_ms, 1 if rec.ok else 0, rec.error, rec.ts,
                ),
            )
            self._conn.commit()

    def costs(self, *, thread_id: str | None = None, limit: int = 200) -> list[CostRecord]:
        with self._lock:
            if thread_id:
                rows = self._conn.execute(
                    "SELECT * FROM cost_records WHERE thread_id = ? ORDER BY rowid DESC LIMIT ?",
                    (thread_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM cost_records ORDER BY rowid DESC LIMIT ?", (limit,)
                ).fetchall()
        return [_row_to_cost(r) for r in rows]

    def cost_summary(self) -> dict[str, float]:
        """全局汇总（口径：所有已记录调用）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS calls, COALESCE(SUM(cost),0) AS cost,"
                " COALESCE(SUM(baseline_cost),0) AS baseline, COALESCE(SUM(saved),0) AS saved,"
                " COALESCE(SUM(total_tokens),0) AS tokens"
                " FROM cost_records"
            ).fetchone()
        calls = int(row["calls"] or 0)
        baseline = float(row["baseline"] or 0.0)
        saved = float(row["saved"] or 0.0)
        return {
            "calls": calls,
            "cost": round(float(row["cost"] or 0.0), 6),
            "baseline_cost": round(baseline, 6),
            "saved": round(saved, 6),
            "saved_percent": round(saved / baseline * 100, 2) if baseline > 0 else 0.0,
            "tokens": int(row["tokens"] or 0),
        }

    # ------------------------------------------------------------------ #
    # 评测运行
    # ------------------------------------------------------------------ #
    def add_eval_run(self, report: EvalReport) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO eval_runs("
                "eval_id, scene, total, passed, failed, score, judge_source,"
                " duration_ms, token_cost, payload_json, created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    report.eval_id, report.scene, report.total, report.passed, report.failed,
                    report.score, report.judge_source, report.duration_ms, report.token_cost,
                    report.model_dump_json(), report.created_at,
                ),
            )
            self._conn.commit()

    def eval_runs(self, *, scene: str | None = None, limit: int = 20) -> list[dict]:
        with self._lock:
            if scene:
                rows = self._conn.execute(
                    "SELECT eval_id, scene, total, passed, failed, score, judge_source,"
                    " duration_ms, created_at FROM eval_runs WHERE scene = ?"
                    " ORDER BY rowid DESC LIMIT ?",
                    (scene, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT eval_id, scene, total, passed, failed, score, judge_source,"
                    " duration_ms, created_at FROM eval_runs ORDER BY rowid DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    def latest_eval_report(self, scene: str = "all") -> EvalReport | None:
        """取最近一次评测的**完整报告**（含逐条用例结果）。

        为什么要完整报告而不是摘要：CI 准入需要比对"具体哪条用例新失败了"，
        只有摘要的话，退化时无法定位到用例。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM eval_runs WHERE scene = ? ORDER BY rowid DESC LIMIT 1",
                (scene,),
            ).fetchone()
        if row is None:
            return None
        try:
            return EvalReport.model_validate_json(row["payload_json"])
        except Exception:  # noqa: BLE001 - 历史数据格式变更时不影响新评测
            logger.warning("历史评测报告解析失败，忽略该基线", exc_info=True)
            return None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_cost(row: sqlite3.Row) -> CostRecord:
    return CostRecord(
        record_id=row["record_id"],
        run_id=row["run_id"] or "",
        thread_id=row["thread_id"] or "",
        scope=row["scope"] or "",
        task=row["task"] or "",
        tier=row["tier"] or "medium",
        model=row["model"] or "",
        prompt_tokens=int(row["prompt_tokens"] or 0),
        completion_tokens=int(row["completion_tokens"] or 0),
        total_tokens=int(row["total_tokens"] or 0),
        cost=float(row["cost"] or 0.0),
        baseline_cost=float(row["baseline_cost"] or 0.0),
        saved=float(row["saved"] or 0.0),
        cache_status=row["cache_status"] or "miss",
        cache_policy=row["cache_policy"] or "semantic",
        trimmed_tokens=int(row["trimmed_tokens"] or 0),
        fallbacks=int(row["fallbacks"] or 0),
        latency_ms=float(row["latency_ms"] or 0.0),
        ok=bool(row["ok"]),
        error=row["error"],
        ts=row["ts"] or "",
    )
