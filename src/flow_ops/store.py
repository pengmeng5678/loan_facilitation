"""项目八的持久化（SQLite）。

八张表对应八条业务主线：

    leads           线索主表（状态机当前态）
    transitions     阶段流转流水（进度时间线与审计的数据源）
    appointments    到店邀约
    sop_alerts      SOP 逾期预警（主管待办）
    approvals       高风险动作人工审批（HITL）
    audit_log       全程审计留痕（监管/追责用）
    idempotency_keys 写操作幂等记录（重复操作零事故）
    locks           分布式锁租约（防并发重复）

**为什么 SQLite 而不是直接调 CRM**：CRM/企微 OpenAPI 是外部系统，
项目内的流程引擎必须有一个自己能控的事务边界与查询能力。
对接真实系统时，把本 Store 的方法改成先写本地再同步上游（outbox 模式），
表结构与接口不用变 —— 这就是"可替换基础设施"的含义。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from agent_platform.logging_config import get_logger
from flow_ops.domain import (
    Appointment,
    ApprovalRequest,
    ApprovalStatus,
    AuditEntry,
    Lead,
    LeadStage,
    SopAlert,
    StageTransition,
)

logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    lead_id         TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL,
    customer_name   TEXT NOT NULL,
    phone_masked    TEXT NOT NULL,
    source          TEXT NOT NULL,
    owner           TEXT NOT NULL,
    stage           TEXT NOT NULL,
    human_stage     TEXT,
    lost_reason     TEXT,
    note            TEXT,
    stage_entered_at REAL NOT NULL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_leads_customer ON leads(customer_id);
CREATE INDEX IF NOT EXISTS idx_leads_stage ON leads(stage);
CREATE INDEX IF NOT EXISTS idx_leads_owner ON leads(owner, stage);

CREATE TABLE IF NOT EXISTS transitions (
    transition_id TEXT PRIMARY KEY,
    lead_id       TEXT NOT NULL,
    from_stage    TEXT NOT NULL,
    to_stage      TEXT NOT NULL,
    actor         TEXT NOT NULL,
    reason        TEXT,
    ts            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transitions_lead ON transitions(lead_id, ts);

CREATE TABLE IF NOT EXISTS appointments (
    appointment_id TEXT PRIMARY KEY,
    lead_id        TEXT NOT NULL,
    store          TEXT NOT NULL,
    scheduled_at   REAL NOT NULL,
    status         TEXT NOT NULL,
    note           TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_appt_lead ON appointments(lead_id, status);

CREATE TABLE IF NOT EXISTS sop_alerts (
    alert_id         TEXT PRIMARY KEY,
    lead_id          TEXT NOT NULL,
    customer_name    TEXT NOT NULL,
    owner            TEXT NOT NULL,
    stage            TEXT NOT NULL,
    action           TEXT NOT NULL,
    overdue_minutes  INTEGER NOT NULL,
    created_at       REAL NOT NULL,
    acknowledged     INTEGER NOT NULL DEFAULT 0,
    acknowledged_by  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_alerts_ack ON sop_alerts(acknowledged, created_at);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id      TEXT PRIMARY KEY,
    action           TEXT NOT NULL,
    lead_id          TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    requester        TEXT NOT NULL,
    reason           TEXT,
    status           TEXT NOT NULL,
    created_at       REAL NOT NULL,
    decided_at       REAL,
    decided_by       TEXT,
    decision_comment TEXT,
    expires_at       REAL,
    executed_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at);

CREATE TABLE IF NOT EXISTS audit_log (
    entry_id     TEXT PRIMARY KEY,
    ts           REAL NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    target_type  TEXT NOT NULL,
    target_id    TEXT NOT NULL,
    detail       TEXT,
    approval_id  TEXT NOT NULL DEFAULT '',
    result       TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target_type, target_id, ts);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, ts);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    idem_key    TEXT PRIMARY KEY,
    action      TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'done',
    owner       TEXT,
    lease_expires_at REAL
);
CREATE INDEX IF NOT EXISTS idx_idem_expires ON idempotency_keys(expires_at);

CREATE TABLE IF NOT EXISTS locks (
    lock_key   TEXT PRIMARY KEY,
    owner      TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
"""


def _row_to_lead(row: sqlite3.Row) -> Lead:
    from flow_ops.domain import HumanStage

    return Lead(
        lead_id=row["lead_id"],
        customer_id=row["customer_id"],
        customer_name=row["customer_name"],
        phone_masked=row["phone_masked"],
        source=row["source"],
        owner=row["owner"],
        stage=LeadStage(row["stage"]),
        human_stage=HumanStage(row["human_stage"]) if row["human_stage"] else None,
        lost_reason=row["lost_reason"] or "",
        note=row["note"] or "",
        stage_entered_at=row["stage_entered_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        payload=json.loads(row["payload_json"] or "{}"),
    )


def _row_to_approval(row: sqlite3.Row) -> ApprovalRequest:
    return ApprovalRequest(
        approval_id=row["approval_id"],
        action=row["action"],
        lead_id=row["lead_id"],
        payload=json.loads(row["payload_json"] or "{}"),
        requester=row["requester"],
        reason=row["reason"] or "",
        status=ApprovalStatus(row["status"]),
        created_at=row["created_at"],
        decided_at=row["decided_at"] or 0.0,
        decided_by=row["decided_by"] or "",
        decision_comment=row["decision_comment"] or "",
        expires_at=row["expires_at"] or 0.0,
        executed_at=row["executed_at"] or 0.0,
    )


class FlowStore:
    """SQLite 存储。所有方法都是短事务，调用方不需要管理连接。

    线程安全：sqlite3 连接带 check_same_thread=False + 一把线程锁。
    单进程多线程（FastAPI 线程池）足够；多进程部署时把 SQLite 换成
    PostgreSQL，SQL 语句不用改（都是标准语法）。
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")  # 读写不互斥，测试并行更稳
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()
        logger.info("流程库就绪：%s", self.db_path)

    def _migrate(self) -> None:
        """轻量列迁移：老库缺列就补，不重写表、不丢数据。

        `CREATE TABLE IF NOT EXISTS` 对已存在的表不生效，所以新增列必须
        单独 ALTER；用 PRAGMA 判断而不是 try/except 吞错 —— 迁移失败应当
        在启动期暴露，而不是等到某次查询报"no such column"。
        """
        wanted = {
            "approvals": {"executed_at": "REAL"},
            "idempotency_keys": {
                "status": "TEXT NOT NULL DEFAULT 'done'",
                "owner": "TEXT",
                "lease_expires_at": "REAL",
            },
        }
        for table, columns in wanted.items():
            existing = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, sql_type in columns.items():
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
                    logger.info("流程库迁移：%s 新增列 %s", table, name)

    def close(self) -> None:
        """关闭连接。Windows 上删临时目录前必须调用，否则句柄占用。"""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover - 关闭失败不影响退出
                pass

    def __enter__(self) -> FlowStore:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 线索
    # ------------------------------------------------------------------ #
    def insert_lead(self, lead: Lead) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO leads
                   (lead_id, customer_id, customer_name, phone_masked, source, owner,
                    stage, human_stage, lost_reason, note, stage_entered_at,
                    created_at, updated_at, payload_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lead.lead_id, lead.customer_id, lead.customer_name, lead.phone_masked,
                    lead.source, lead.owner, lead.stage.value,
                    lead.human_stage.value if lead.human_stage else None,
                    lead.lost_reason, lead.note, lead.stage_entered_at,
                    lead.created_at, lead.updated_at,
                    json.dumps(lead.payload, ensure_ascii=False),
                ),
            )
            self._conn.commit()

    def update_lead(self, lead: Lead) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE leads SET stage=?, human_stage=?, lost_reason=?, note=?,
                   stage_entered_at=?, updated_at=?, payload_json=? WHERE lead_id=?""",
                (
                    lead.stage.value,
                    lead.human_stage.value if lead.human_stage else None,
                    lead.lost_reason, lead.note, lead.stage_entered_at, lead.updated_at,
                    json.dumps(lead.payload, ensure_ascii=False), lead.lead_id,
                ),
            )
            self._conn.commit()

    def get_lead(self, lead_id: str) -> Lead | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM leads WHERE lead_id=?", (lead_id,)).fetchone()
        return _row_to_lead(row) if row else None

    def find_lead_by_customer(self, customer_id: str) -> Lead | None:
        """按客户查最新一条线索（同一客户重复留资时用于合并提示）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM leads WHERE customer_id=? ORDER BY created_at DESC LIMIT 1",
                (customer_id,),
            ).fetchone()
        return _row_to_lead(row) if row else None

    def list_leads(
        self,
        *,
        stage: str = "",
        owner: str = "",
        limit: int = 100,
    ) -> list[Lead]:
        sql = "SELECT * FROM leads WHERE 1=1"
        params: list[Any] = []
        if stage:
            sql += " AND stage=?"
            params.append(stage)
        if owner:
            sql += " AND owner=?"
            params.append(owner)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_lead(r) for r in rows]

    def count_by_stage(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT stage, COUNT(*) AS n FROM leads GROUP BY stage"
            ).fetchall()
        return {r["stage"]: r["n"] for r in rows}

    # ------------------------------------------------------------------ #
    # 阶段流转
    # ------------------------------------------------------------------ #
    def insert_transition(self, tr: StageTransition) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO transitions
                   (transition_id, lead_id, from_stage, to_stage, actor, reason, ts)
                   VALUES (?,?,?,?,?,?,?)""",
                (tr.transition_id, tr.lead_id, tr.from_stage, tr.to_stage,
                 tr.actor, tr.reason, tr.ts),
            )
            self._conn.commit()

    def list_transitions(self, lead_id: str) -> list[StageTransition]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM transitions WHERE lead_id=? ORDER BY ts ASC", (lead_id,)
            ).fetchall()
        return [
            StageTransition(
                transition_id=r["transition_id"], lead_id=r["lead_id"],
                from_stage=r["from_stage"], to_stage=r["to_stage"],
                actor=r["actor"], reason=r["reason"] or "", ts=r["ts"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    # 邀约
    # ------------------------------------------------------------------ #
    def insert_appointment(self, appt: Appointment) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO appointments
                   (appointment_id, lead_id, store, scheduled_at, status, note, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (appt.appointment_id, appt.lead_id, appt.store, appt.scheduled_at,
                 appt.status, appt.note, appt.created_at, appt.updated_at),
            )
            self._conn.commit()

    def update_appointment(self, appt: Appointment) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE appointments SET status=?, note=?, updated_at=? WHERE appointment_id=?",
                (appt.status, appt.note, appt.updated_at, appt.appointment_id),
            )
            self._conn.commit()

    def find_active_appointment(self, lead_id: str) -> Appointment | None:
        """取未完成的邀约（booked / confirmed）。到店确认与幂等都靠它。"""
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM appointments WHERE lead_id=? AND status IN ('booked','confirmed')
                   ORDER BY scheduled_at ASC LIMIT 1""",
                (lead_id,),
            ).fetchone()
        if not row:
            return None
        return Appointment(
            appointment_id=row["appointment_id"], lead_id=row["lead_id"],
            store=row["store"], scheduled_at=row["scheduled_at"], status=row["status"],
            note=row["note"] or "", created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def list_appointments(self, lead_id: str) -> list[Appointment]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM appointments WHERE lead_id=? ORDER BY scheduled_at DESC",
                (lead_id,),
            ).fetchall()
        return [
            Appointment(
                appointment_id=r["appointment_id"], lead_id=r["lead_id"], store=r["store"],
                scheduled_at=r["scheduled_at"], status=r["status"], note=r["note"] or "",
                created_at=r["created_at"], updated_at=r["updated_at"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    # SOP 预警
    # ------------------------------------------------------------------ #
    def insert_alert(self, alert: SopAlert) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO sop_alerts
                   (alert_id, lead_id, customer_name, owner, stage, action,
                    overdue_minutes, created_at, acknowledged, acknowledged_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (alert.alert_id, alert.lead_id, alert.customer_name, alert.owner,
                 alert.stage, alert.action, alert.overdue_minutes, alert.created_at,
                 int(alert.acknowledged), alert.acknowledged_by),
            )
            self._conn.commit()

    def list_alerts(self, *, include_acknowledged: bool = False, limit: int = 100) -> list[SopAlert]:
        sql = "SELECT * FROM sop_alerts"
        if not include_acknowledged:
            sql += " WHERE acknowledged=0"
        sql += " ORDER BY created_at DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (limit,)).fetchall()
        return [
            SopAlert(
                alert_id=r["alert_id"], lead_id=r["lead_id"], customer_name=r["customer_name"],
                owner=r["owner"], stage=r["stage"], action=r["action"],
                overdue_minutes=r["overdue_minutes"], created_at=r["created_at"],
                acknowledged=bool(r["acknowledged"]), acknowledged_by=r["acknowledged_by"],
            )
            for r in rows
        ]

    def acknowledge_alert(self, alert_id: str, *, by: str, now: float) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE sop_alerts SET acknowledged=1, acknowledged_by=? WHERE alert_id=?",
                (by, alert_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def find_open_alert(self, lead_id: str, action: str) -> SopAlert | None:
        """查某线索某动作是否已有未处理预警（避免同一件事反复轰炸主管）。"""
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM sop_alerts
                   WHERE lead_id=? AND action=? AND acknowledged=0
                   ORDER BY created_at DESC LIMIT 1""",
                (lead_id, action),
            ).fetchone()
        if not row:
            return None
        return SopAlert(
            alert_id=row["alert_id"], lead_id=row["lead_id"], customer_name=row["customer_name"],
            owner=row["owner"], stage=row["stage"], action=row["action"],
            overdue_minutes=row["overdue_minutes"], created_at=row["created_at"],
            acknowledged=bool(row["acknowledged"]), acknowledged_by=row["acknowledged_by"],
        )

    def find_recent_alert(self, lead_id: str, action: str, *, since_ts: float) -> SopAlert | None:
        """查某线索某动作在 since_ts 之后是否已有预警（不论是否认领）。

        认领的语义是"我知道了，在处理中"，不是"这事完了" ——
        所以认领之后要给一个宽限期（通常 2×SLA）再允许升级预警，
        否则主管认领得越多，被轰炸得越频繁，很快就学会忽略预警。
        """
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM sop_alerts
                   WHERE lead_id=? AND action=? AND created_at>=?
                   ORDER BY created_at DESC LIMIT 1""",
                (lead_id, action, since_ts),
            ).fetchone()
        if not row:
            return None
        return SopAlert(
            alert_id=row["alert_id"], lead_id=row["lead_id"], customer_name=row["customer_name"],
            owner=row["owner"], stage=row["stage"], action=row["action"],
            overdue_minutes=row["overdue_minutes"], created_at=row["created_at"],
            acknowledged=bool(row["acknowledged"]), acknowledged_by=row["acknowledged_by"],
        )

    # ------------------------------------------------------------------ #
    # 审批
    # ------------------------------------------------------------------ #
    def insert_approval(self, appr: ApprovalRequest) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO approvals
                   (approval_id, action, lead_id, payload_json, requester, reason,
                    status, created_at, decided_at, decided_by, decision_comment, expires_at,
                    executed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (appr.approval_id, appr.action, appr.lead_id,
                 json.dumps(appr.payload, ensure_ascii=False), appr.requester, appr.reason,
                 appr.status.value, appr.created_at, appr.decided_at or None,
                 appr.decided_by, appr.decision_comment, appr.expires_at or None,
                 appr.executed_at or None),
            )
            self._conn.commit()

    def update_approval(self, appr: ApprovalRequest) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE approvals SET status=?, decided_at=?, decided_by=?,
                   decision_comment=?, executed_at=? WHERE approval_id=?""",
                (appr.status.value, appr.decided_at or None, appr.decided_by,
                 appr.decision_comment, appr.executed_at or None, appr.approval_id),
            )
            self._conn.commit()

    def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
            ).fetchone()
        return _row_to_approval(row) if row else None

    def list_approvals(self, *, status: str = "", limit: int = 100) -> list[ApprovalRequest]:
        sql = "SELECT * FROM approvals"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_approval(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 审计
    # ------------------------------------------------------------------ #
    def insert_audit(self, entry: AuditEntry) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO audit_log
                   (entry_id, ts, actor, action, target_type, target_id, detail,
                    approval_id, result)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (entry.entry_id, entry.ts, entry.actor, entry.action, entry.target_type,
                 entry.target_id, entry.detail, entry.approval_id, entry.result),
            )
            self._conn.commit()

    def list_audit(
        self,
        *,
        target_type: str = "",
        target_id: str = "",
        action: str = "",
        limit: int = 200,
    ) -> list[AuditEntry]:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        params: list[Any] = []
        if target_type:
            sql += " AND target_type=?"
            params.append(target_type)
        if target_id:
            sql += " AND target_id=?"
            params.append(target_id)
        if action:
            sql += " AND action=?"
            params.append(action)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            AuditEntry(
                entry_id=r["entry_id"], ts=r["ts"], actor=r["actor"], action=r["action"],
                target_type=r["target_type"], target_id=r["target_id"], detail=r["detail"] or "",
                approval_id=r["approval_id"] or "", result=r["result"] or "ok",
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    # 幂等键
    # ------------------------------------------------------------------ #
    def get_idempotent(self, key: str, *, now: float) -> dict[str, Any] | None:
        """查幂等记录。过期视为不存在（并顺手清理）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM idempotency_keys WHERE idem_key=?", (key,)
            ).fetchone()
            if row and row["expires_at"] > now:
                return json.loads(row["result_json"])
            if row:
                self._conn.execute(
                    "DELETE FROM idempotency_keys WHERE idem_key=?", (key,)
                )
                self._conn.commit()
        return None

    def put_idempotent(self, key: str, *, action: str, result: dict[str, Any],
                       now: float, ttl_hours: int) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO idempotency_keys
                   (idem_key, action, result_json, created_at, expires_at,
                    status, owner, lease_expires_at)
                   VALUES (?,?,?,?,?,'done',NULL,NULL)""",
                (key, action, json.dumps(result, ensure_ascii=False),
                 now, now + ttl_hours * 3600),
            )
            self._conn.commit()

    def claim_idempotent(
        self,
        key: str,
        *,
        action: str,
        owner: str,
        now: float,
        lease_seconds: int,
        ttl_hours: int,
    ) -> tuple[str, dict[str, Any] | None]:
        """原子抢占幂等键，返回 (状态, 缓存结果)。

        状态语义：
            done    —— 已执行完且未过期，result 是首次结果（幂等命中）
            claimed —— 本次抢到执行权（写入 processing + 租约）
            busy    —— 另一进程正在执行且租约未到期

        为什么必须落到库里的原子操作：原来的 `threading.Lock` 只在单进程内
        生效，两个 uvicorn worker 仍会同时穿透缓存各执行一次。
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM idempotency_keys WHERE idem_key=?", (key,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO idempotency_keys
                       (idem_key, action, result_json, created_at, expires_at,
                        status, owner, lease_expires_at)
                       VALUES (?,?,?,?,?,'processing',?,?)""",
                    (key, action, "{}", now, now + ttl_hours * 3600,
                     owner, now + lease_seconds),
                )
                return "claimed", None
            if row["status"] == "done" and row["expires_at"] > now:
                return "done", json.loads(row["result_json"] or "{}")
            if row["status"] == "processing" and (row["lease_expires_at"] or 0) > now:
                return "busy", None
            # 租约过期（持有者崩溃）：CAS 抢占，防止两个进程同时接管
            cur = self._conn.execute(
                """UPDATE idempotency_keys
                   SET owner=?, lease_expires_at=?, status='processing', expires_at=?
                   WHERE idem_key=? AND (lease_expires_at IS NULL OR lease_expires_at<=?)""",
                (owner, now + lease_seconds, now + ttl_hours * 3600, key, now),
            )
            return ("claimed", None) if cur.rowcount == 1 else ("busy", None)

    def complete_idempotent(
        self, key: str, *, owner: str, result: dict[str, Any], now: float, ttl_hours: int
    ) -> None:
        """执行成功后落结果（仅限持有租约者，owner 不匹配不会覆盖）。"""
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE idempotency_keys
                   SET status='done', result_json=?, owner=NULL, lease_expires_at=NULL,
                       expires_at=?
                   WHERE idem_key=? AND owner=?""",
                (json.dumps(result, ensure_ascii=False), now + ttl_hours * 3600, key, owner),
            )

    def release_idempotent(self, key: str, *, owner: str) -> None:
        """执行失败时释放占位，让同一请求可以安全重试。"""
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM idempotency_keys WHERE idem_key=? AND owner=? AND status='processing'",
                (key, owner),
            )

    def count_idempotent_hits(self) -> int:
        """幂等命中总数（= 被拦下来的重复操作数）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM audit_log WHERE result='idempotent_hit'"
            ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------ #
    # 分布式锁
    # ------------------------------------------------------------------ #
    def try_acquire_lock(self, key: str, *, owner: str, ttl_seconds: int, now: float) -> bool:
        """尝试加锁。过期锁可被抢占（owner 不同才算抢占成功）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM locks WHERE lock_key=?", (key,)
            ).fetchone()
            if row and row["expires_at"] > now and row["owner"] != owner:
                return False
            self._conn.execute(
                """INSERT OR REPLACE INTO locks (lock_key, owner, acquired_at, expires_at)
                   VALUES (?,?,?,?)""",
                (key, owner, now, now + ttl_seconds),
            )
            self._conn.commit()
            return True

    def release_lock(self, key: str, *, owner: str) -> bool:
        """释放锁。owner 不匹配不能释放（防止 A 把 B 的锁放了）。"""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM locks WHERE lock_key=? AND owner=?", (key, owner)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def renew_lock(self, key: str, *, owner: str, ttl_seconds: int, now: float) -> bool:
        """续约：owner 匹配才续期，返回是否续上（锁没了 / 不是自己的 → False）。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE locks SET expires_at=? WHERE lock_key=? AND owner=?",
                (now + ttl_seconds, key, owner),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------ #
    # 统计
    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, Any]:
        with self._lock:
            counts = {
                t: self._conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                for t in (
                    "leads", "transitions", "appointments", "sop_alerts",
                    "approvals", "audit_log", "idempotency_keys", "locks",
                )
            }
        counts["db_path"] = str(self.db_path)
        counts["db_size_kb"] = round(self.db_path.stat().st_size / 1024, 1) \
            if self.db_path.exists() else 0
        return counts


def now_ts() -> float:
    """统一取时间（测试可 monkeypatch）。"""
    return time.time()
