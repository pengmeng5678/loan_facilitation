"""素材推广的持久化（SQLite）。

四张表对应四条业务主线：
    materials        素材库（含合规结论与质量分）—— "有哪些能发的素材"
    compliance_audit 合规审核流水            —— "拦截了多少、拦了什么"（可审计）
    activation_tasks 激活任务                —— "谁该在什么时候收到什么"
    touch_records    触达记录                —— 归因的唯一数据来源

为什么合规流水要单独一张表：监管来查"你们的素材审核记录"，要能一条条拿出来。
把它混在素材表里，就变成"素材删了记录也没了"，这是审计风险。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from agent_platform.logging_config import get_logger
from marketing.domain import (
    ActivationPlan,
    ActivationTask,
    CaseStudy,
    ComplianceReport,
    MaterialDraft,
    TouchRecord,
)

logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS materials (
    material_id  TEXT PRIMARY KEY,
    draft_id     TEXT,
    channel      TEXT NOT NULL,
    theme        TEXT,
    title        TEXT,
    body         TEXT,
    status       TEXT,
    quality      REAL,
    source       TEXT,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_materials_channel ON materials(channel, status);
CREATE TABLE IF NOT EXISTS compliance_audit (
    audit_id     TEXT PRIMARY KEY,
    ref_id       TEXT,
    stage        TEXT,
    passed       INTEGER,
    needs_human  INTEGER,
    severity     TEXT,
    findings_json TEXT,
    checked_rules INTEGER,
    text_preview TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ref ON compliance_audit(ref_id);
CREATE TABLE IF NOT EXISTS cases (
    case_id      TEXT PRIMARY KEY,
    scene        TEXT,
    path         TEXT,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activation_tasks (
    task_id      TEXT PRIMARY KEY,
    plan_id      TEXT,
    customer_id  TEXT,
    channel      TEXT,
    priority     REAL,
    status       TEXT,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activation_customer ON activation_tasks(customer_id);
CREATE TABLE IF NOT EXISTS touch_records (
    touch_id     TEXT PRIMARY KEY,
    material_id  TEXT,
    channel      TEXT,
    customer_id  TEXT,
    stage        TEXT,
    ts           TEXT
);
CREATE INDEX IF NOT EXISTS idx_touch_material ON touch_records(material_id);
"""


class MarketingStore:
    """素材推广仓储。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        logger.info("素材推广库就绪：%s", self.db_path)

    # ------------------------------------------------------------------ #
    # 素材
    # ------------------------------------------------------------------ #
    def save_material(self, draft: MaterialDraft) -> str:
        """保存素材，返回 material_id（同一条 draft 重复保存会复用原 id）。"""
        material_id = draft.material_id or f"mat_{draft.draft_id}"
        payload = draft.model_copy(update={"material_id": material_id})
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO materials("
                "material_id, draft_id, channel, theme, title, body, status, quality, source,"
                " payload_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    material_id, draft.draft_id, draft.channel, draft.theme, draft.title,
                    draft.body, draft.status, draft.quality_score, draft.source,
                    payload.model_dump_json(), draft.created_at,
                ),
            )
            self._conn.commit()
        return material_id

    def list_materials(self, *, channel: str = "", status: str = "", limit: int = 200) -> list[MaterialDraft]:
        clauses: list[str] = []
        params: list[object] = []
        if channel:
            clauses.append("channel = ?")
            params.append(channel)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT payload_json FROM materials{where} ORDER BY rowid DESC LIMIT ?",  # noqa: S608 - 子句由白名单拼装
                (*params, limit),
            ).fetchall()
        return [MaterialDraft.model_validate_json(r["payload_json"]) for r in rows]

    def material_count(self, status: str = "") -> int:
        with self._lock:
            if status:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM materials WHERE status = ?", (status,)
                ).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) AS n FROM materials").fetchone()
        return int(row["n"] or 0)

    # ------------------------------------------------------------------ #
    # 合规流水
    # ------------------------------------------------------------------ #
    def save_audit(
        self, report: ComplianceReport, *, ref_id: str, stage: str, text: str
    ) -> str:
        import uuid

        audit_id = f"aud_{uuid.uuid4().hex[:10]}"
        with self._lock:
            self._conn.execute(
                "INSERT INTO compliance_audit("
                "audit_id, ref_id, stage, passed, needs_human, severity, findings_json,"
                " checked_rules, text_preview, created_at) VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))",
                (
                    audit_id, ref_id, stage, 1 if report.passed else 0,
                    1 if report.needs_human else 0, report.max_severity,
                    json.dumps([f.model_dump() for f in report.findings], ensure_ascii=False),
                    report.checked_rules, text[:300],
                ),
            )
            self._conn.commit()
        return audit_id

    def audit_stats(self) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN passed = 0 THEN 1 ELSE 0 END) AS blocked,"
                " SUM(CASE WHEN needs_human = 1 THEN 1 ELSE 0 END) AS needs_human"
                " FROM compliance_audit"
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "blocked": int(row["blocked"] or 0),
            "needs_human": int(row["needs_human"] or 0),
        }

    def audits(self, *, ref_id: str = "", limit: int = 100) -> list[dict]:
        with self._lock:
            if ref_id:
                rows = self._conn.execute(
                    "SELECT * FROM compliance_audit WHERE ref_id = ? ORDER BY rowid DESC LIMIT ?",
                    (ref_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM compliance_audit ORDER BY rowid DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 案例
    # ------------------------------------------------------------------ #
    def save_case(self, case: CaseStudy) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO cases(case_id, scene, path, payload_json, created_at)"
                " VALUES (?,?,?,?,?)",
                (case.case_id, case.scene, case.path, case.model_dump_json(), case.created_at),
            )
            self._conn.commit()

    def list_cases(self, limit: int = 100) -> list[CaseStudy]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM cases ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [CaseStudy.model_validate_json(r["payload_json"]) for r in rows]

    # ------------------------------------------------------------------ #
    # 激活任务
    # ------------------------------------------------------------------ #
    def save_plan(self, plan: ActivationPlan) -> None:
        with self._lock:
            for task in plan.tasks:
                self._conn.execute(
                    "INSERT OR REPLACE INTO activation_tasks("
                    "task_id, plan_id, customer_id, channel, priority, status, payload_json, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        task.task_id, plan.plan_id, task.customer_id, task.channel,
                        task.priority, task.status, task.model_dump_json(), plan.created_at,
                    ),
                )
            self._conn.commit()

    def list_activation_tasks(self, limit: int = 100) -> list[ActivationTask]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM activation_tasks ORDER BY priority DESC LIMIT ?", (limit,)
            ).fetchall()
        return [ActivationTask.model_validate_json(r["payload_json"]) for r in rows]

    def touch_counts_by_customer(self) -> dict[str, int]:
        """各客户已被触达次数（激活防骚扰要读它）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT customer_id, COUNT(*) AS n FROM touch_records"
                " WHERE customer_id != '' GROUP BY customer_id"
            ).fetchall()
        return {r["customer_id"]: int(r["n"]) for r in rows}

    # ------------------------------------------------------------------ #
    # 触达记录
    # ------------------------------------------------------------------ #
    def save_touch(self, touch: TouchRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO touch_records("
                "touch_id, material_id, channel, customer_id, stage, ts) VALUES (?,?,?,?,?,?)",
                (touch.touch_id, touch.material_id, touch.channel, touch.customer_id, touch.stage, touch.ts),
            )
            self._conn.commit()

    def list_touches(self, *, material_id: str = "", limit: int = 2000) -> list[TouchRecord]:
        with self._lock:
            if material_id:
                rows = self._conn.execute(
                    "SELECT * FROM touch_records WHERE material_id = ? ORDER BY rowid DESC LIMIT ?",
                    (material_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM touch_records ORDER BY rowid DESC LIMIT ?", (limit,)
                ).fetchall()
        return [
            TouchRecord(
                touch_id=r["touch_id"], material_id=r["material_id"], channel=r["channel"],
                customer_id=r["customer_id"] or "", stage=r["stage"], ts=r["ts"] or "",
            )
            for r in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
