from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import settings
from .storage import now_iso
from .projects import project_store, current_project


class WorkflowStore:
    """Persistent hazard, notification and rectification state.

    The front end treats this as the source of truth; no close-loop state lives
    only in browser memory.
    """
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (settings.data_dir / "workflow.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS hazard_groups (id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, job_id TEXT, order_id TEXT NOT NULL, work_area TEXT NOT NULL, point TEXT NOT NULL DEFAULT '', source_type TEXT NOT NULL DEFAULT 'upload', camera_id TEXT, frame_id TEXT, before_image TEXT, annotated_image TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS hazards (id TEXT PRIMARY KEY, job_id TEXT, label_id TEXT NOT NULL, name TEXT NOT NULL, severity TEXT NOT NULL, work_area TEXT NOT NULL, status TEXT NOT NULL, evidence TEXT NOT NULL, before_image TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, source_key TEXT, verified_by TEXT, verified_at TEXT, false_positive_reason TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS rectifications (id TEXT PRIMARY KEY, hazard_id TEXT NOT NULL, description TEXT NOT NULL, submitted_by TEXT NOT NULL, after_image TEXT, submitted_at TEXT NOT NULL, review_result TEXT, reviewer TEXT, review_comment TEXT, record_type TEXT NOT NULL DEFAULT 'reply')")
            db.execute("CREATE TABLE IF NOT EXISTS notifications (id TEXT PRIMARY KEY, hazard_id TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, read_at TEXT, created_at TEXT NOT NULL, delivery_status TEXT NOT NULL DEFAULT 'pending', delivery_records TEXT NOT NULL DEFAULT '[]')")
            db.execute("CREATE TABLE IF NOT EXISTS notification_reads (notification_id TEXT NOT NULL, user_id TEXT NOT NULL, read_at TEXT NOT NULL, PRIMARY KEY(notification_id,user_id))")
            db.execute("CREATE TABLE IF NOT EXISTS hazard_audit_events (id TEXT PRIMARY KEY, hazard_id TEXT NOT NULL, action TEXT NOT NULL, actor_id TEXT, actor_name TEXT NOT NULL, before_value TEXT NOT NULL DEFAULT '{}', after_value TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)")
            self._ensure_columns(db)
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_hazards_source_key ON hazards(source_key) WHERE source_key IS NOT NULL")
            db.execute("CREATE INDEX IF NOT EXISTS idx_hazards_group ON hazards(group_id)")
            self._migrate_legacy_groups(db)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _ensure_columns(db: sqlite3.Connection) -> None:
        """Upgrade the original baseline database without destroying user data."""
        existing = {row[1] for row in db.execute("PRAGMA table_info(hazards)")}
        for name, definition in {
            "source_key": "TEXT",
            "verified_by": "TEXT",
            "verified_at": "TEXT",
            "false_positive_reason": "TEXT",
            "group_id": "TEXT",
            "original_name": "TEXT",
            "original_evidence": "TEXT",
            "original_severity": "TEXT",
            "version": "INTEGER NOT NULL DEFAULT 1",
            "assigned_to": "TEXT",
        }.items():
            if name not in existing:
                db.execute(f"ALTER TABLE hazards ADD COLUMN {name} {definition}")
        existing = {row[1] for row in db.execute("PRAGMA table_info(notifications)")}
        for name, definition in {
            "delivery_status": "TEXT NOT NULL DEFAULT 'pending'",
            "delivery_records": "TEXT NOT NULL DEFAULT '[]'",
            "group_id": "TEXT",
            "recipient_role": "TEXT",
            "notification_type": "TEXT NOT NULL DEFAULT 'initial'",
        }.items():
            if name not in existing:
                db.execute(f"ALTER TABLE notifications ADD COLUMN {name} {definition}")
        existing = {row[1] for row in db.execute("PRAGMA table_info(rectifications)")}
        if "record_type" not in existing:
            db.execute("ALTER TABLE rectifications ADD COLUMN record_type TEXT NOT NULL DEFAULT 'reply'")

    @staticmethod
    def _migrate_legacy_groups(db: sqlite3.Connection) -> None:
        rows = db.execute("SELECT * FROM hazards WHERE group_id IS NULL OR group_id='' ORDER BY created_at").fetchall()
        groups: dict[str, str] = {}
        for row in rows:
            # Exact job+image matches are safe to merge. Records without both
            # fields keep a private group so unrelated historical data is never merged.
            key = f"legacy:{row['job_id']}:{row['before_image']}" if row["job_id"] and row["before_image"] else f"legacy:{row['id']}"
            group_id = groups.get(key)
            if not group_id:
                group_id = f"HG-{uuid.uuid4().hex[:12]}"
                groups[key] = group_id
                db.execute(
                    "INSERT OR IGNORE INTO hazard_groups(id,source_key,job_id,order_id,work_area,before_image,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (group_id, key, row["job_id"], f"ZGD-{uuid.uuid4().hex[:12]}", row["work_area"], row["before_image"], row["created_at"], row["updated_at"]),
                )
            db.execute(
                "UPDATE hazards SET group_id=?,original_name=COALESCE(original_name,name),original_evidence=COALESCE(original_evidence,evidence),original_severity=COALESCE(original_severity,severity),version=COALESCE(version,1) WHERE id=?",
                (group_id, row["id"]),
            )
            db.execute("UPDATE notifications SET group_id=COALESCE(group_id,?) WHERE hazard_id=?", (group_id, row["id"]))

    def create_from_findings(
        self,
        *,
        job_id: str,
        work_area: str,
        findings: list[dict[str, Any]],
        source_key_prefix: str | None = None,
        before_image: str | None = None,
    ) -> list[dict[str, Any]]:
        """Create the server-owned hazard/order/notification records.

        Upload jobs and stream events call this same method. ``source_key`` makes
        retries idempotent, so a reconnect or repeated email action cannot create
        duplicate ledger rows.
        """
        created_ids: list[str] = []
        project_id = project_store.owner("job", job_id) or project_store.owner("stream", job_id) or current_project.get()
        with self._connect() as db:
            for finding in findings:
                if (finding.get("final_status") or finding.get("status")) != "confirmed_hazard": continue
                label_id = str(finding.get("label_id", ""))
                frame_ids = finding.get("source_frame_ids") or []
                frame_id = str(frame_ids[0]) if frame_ids else "image"
                group_key = f"{source_key_prefix or f'job:{job_id}'}:{frame_id}"
                group = db.execute("SELECT * FROM hazard_groups WHERE source_key=?", (group_key,)).fetchone()
                if group:
                    group_id = str(group["id"])
                    if before_image and not group["before_image"]:
                        db.execute("UPDATE hazard_groups SET before_image=?,updated_at=? WHERE id=?", (before_image, now_iso(), group_id))
                else:
                    group_id, group_now = f"HG-{uuid.uuid4().hex[:12]}", now_iso()
                    db.execute(
                        "INSERT INTO hazard_groups(id,source_key,job_id,order_id,work_area,frame_id,before_image,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (group_id, group_key, job_id, f"ZGD-{uuid.uuid4().hex[:12]}", work_area, frame_id, before_image, group_now, group_now),
                    )
                source_key = f"{group_key}:{label_id}"
                project_store.bind("group", group_id, project_id)
                existing = db.execute("SELECT id FROM hazards WHERE source_key=?", (source_key,)).fetchone()
                if existing:
                    created_ids.append(str(existing[0]))
                    continue
                ident, now = f"HZ-{uuid.uuid4().hex[:12]}", now_iso()
                raw_evidence = finding.get("evidence", [])
                evidence = raw_evidence if isinstance(raw_evidence, str) else "\n".join(str(x) for x in raw_evidence)
                name, severity = finding.get("name", "未命名隐患"), finding.get("severity", "general")
                db.execute(
                    "INSERT INTO hazards (id,job_id,label_id,name,severity,work_area,status,evidence,before_image,created_at,updated_at,source_key,verified_by,verified_at,false_positive_reason,group_id,original_name,original_evidence,original_severity,version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                    (ident, job_id, label_id, name, severity, work_area, "待核实", evidence, before_image, now, now, source_key, None, None, None, group_id, name, evidence, severity),
                )
                # The parent group owns the canonical order number. Keep one
                # compatibility order row (bound to the first child only), not
                # one duplicated order per finding.
                has_order = db.execute("SELECT 1 FROM rectifications r JOIN hazards h ON h.id=r.hazard_id WHERE h.group_id=? AND r.record_type='order'", (group_id,)).fetchone()
                if not has_order:
                    order_id = f"RC-{uuid.uuid4().hex[:12]}"
                    order_text = "请核实并整改本证据图中的全部有效隐患"
                    db.execute("INSERT INTO rectifications (id,hazard_id,description,submitted_by,after_image,submitted_at,review_result,reviewer,review_comment,record_type) VALUES(?,?,?,?,?,?,?,?,?,?)", (order_id, ident, order_text, "系统", None, now, None, None, None, "order"))
                if not db.execute("SELECT 1 FROM notifications WHERE group_id=? AND notification_type='initial'", (group_id,)).fetchone():
                    note_id = f"NT-{uuid.uuid4().hex[:12]}"
                    db.execute("INSERT INTO notifications (id,hazard_id,title,body,read_at,created_at,delivery_status,delivery_records,group_id,notification_type) VALUES(?,?,?,?,?,?,?,?,?,?)", (note_id, ident, "发现待核实隐患", f"{work_area}发现一组隐患，已生成整改单，请核实图片和描述。", None, now, "pending", "[]", group_id, "initial"))
                created_ids.append(ident)
                project_store.bind("hazard", ident, project_id)
        return [item for ident in created_ids if (item := self.get(ident)) is not None]

    def record_email_status(self, hazard_ids: list[str], records: list[dict[str, Any]]) -> None:
        """Persist delivery state while retaining only already-masked results."""
        statuses = {str(item.get("status")) for item in records}
        overall = "sent" if records and statuses == {"sent"} else "failed" if "sent" not in statuses else "partial"
        payload = json.dumps(records, ensure_ascii=False)
        with self._connect() as db:
            marks = ",".join("?" for _ in hazard_ids)
            if not marks:
                return
            group_ids = [row[0] for row in db.execute(f"SELECT DISTINCT group_id FROM hazards WHERE id IN ({marks})", hazard_ids).fetchall() if row[0]]
            if group_ids:
                group_marks = ",".join("?" for _ in group_ids)
                db.execute(f"UPDATE notifications SET delivery_status=?,delivery_records=? WHERE notification_type='initial' AND group_id IN ({group_marks})", (overall, payload, *group_ids))
            else:
                db.execute(f"UPDATE notifications SET delivery_status=?,delivery_records=? WHERE hazard_id IN ({marks})", (overall, payload, *hazard_ids))

    def add_group_notification(self, group_id: str, *, title: str, body: str, notification_type: str, records: list[dict[str, Any]]) -> dict[str, Any]:
        now, ident = now_iso(), f"NT-{uuid.uuid4().hex[:12]}"
        statuses = {str(item.get("status")) for item in records}
        overall = "sent" if records and statuses == {"sent"} else "failed" if "sent" not in statuses else "partial"
        with self._connect() as db:
            hazard = db.execute("SELECT id FROM hazards WHERE group_id=? ORDER BY created_at LIMIT 1", (group_id,)).fetchone()
            if not hazard:
                raise ValueError("隐患组不存在")
            db.execute(
                "INSERT INTO notifications(id,hazard_id,title,body,read_at,created_at,delivery_status,delivery_records,group_id,notification_type) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ident, hazard["id"], title, body, None, now, overall, json.dumps(records, ensure_ascii=False), group_id, notification_type),
            )
        with self._connect() as db:
            row = db.execute("SELECT * FROM notifications WHERE id=?", (ident,)).fetchone()
        return self._notification_dict(row)

    def get(self, hazard_id: str) -> dict[str, Any] | None:
        with self._connect() as db: row = db.execute("SELECT * FROM hazards WHERE id=?", (hazard_id,)).fetchone()
        return self._hydrate(dict(row)) if row else None

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as db: rows = db.execute("SELECT * FROM hazards ORDER BY created_at DESC").fetchall()
        return [self._hydrate(dict(row)) for row in rows]

    def submit_rectification(self, hazard_id: str, description: str, submitted_by: str, after_image: str | None) -> dict[str, Any] | None:
        ident, now = f"RC-{uuid.uuid4().hex[:12]}", now_iso()
        with self._connect() as db:
            item = db.execute("SELECT * FROM hazards WHERE id=? AND status NOT IN ('已闭合','已误报')", (hazard_id,)).fetchone()
            if not item: return None
            db.execute("INSERT INTO rectifications (id,hazard_id,description,submitted_by,after_image,submitted_at,review_result,reviewer,review_comment,record_type) VALUES(?,?,?,?,?,?,?,?,?,?)", (ident, hazard_id, description, submitted_by, after_image, now, None, None, None, "reply"))
            db.execute("UPDATE hazards SET status='待复核',updated_at=?,version=version+1 WHERE id=?", (now, hazard_id))
            self._insert_audit(db, hazard_id, "submit_rectification", None, submitted_by, {"status": item["status"], "version": item["version"]}, {"status": "待复核", "description": description, "after_image": bool(after_image), "version": int(item["version"] or 1) + 1}, now)
            if item["group_id"]:
                db.execute("UPDATE hazard_groups SET updated_at=? WHERE id=?", (now, item["group_id"]))
        return self.get(hazard_id)

    def verify(self, hazard_id: str, verifier: str, passed: bool, reason: str = "", *, expected_version: int | None = None, corrected_name: str | None = None, corrected_evidence: str | None = None, corrected_severity: str | None = None, actor_id: str | None = None) -> dict[str, Any] | None:
        with self._connect() as db:
            item = db.execute("SELECT * FROM hazards WHERE id=?", (hazard_id,)).fetchone()
            if not item:
                return None
            if expected_version is not None and int(item["version"] or 1) != expected_version:
                raise RuntimeError("记录已被其他人员修改，请刷新后重试")
            now = now_iso()
            if passed:
                status, false_reason = "待整改", None
            else:
                status, false_reason = "已误报", reason or "现场核实未发现该隐患"
            name = corrected_name or item["name"]
            evidence = corrected_evidence or item["evidence"]
            severity = corrected_severity or item["severity"]
            before = {"name": item["name"], "evidence": item["evidence"], "severity": item["severity"], "status": item["status"], "version": item["version"]}
            after = {"name": name, "evidence": evidence, "severity": severity, "status": status, "version": int(item["version"] or 1) + 1}
            db.execute("UPDATE hazards SET name=?,evidence=?,severity=?,status=?,verified_by=?,verified_at=?,false_positive_reason=?,updated_at=?,version=version+1 WHERE id=?", (name, evidence, severity, status, verifier, now, false_reason, now, hazard_id))
            self._insert_audit(db, hazard_id, "verify" if passed else "mark_false_positive", actor_id, verifier, before, after, now)
            if item["group_id"]:
                db.execute("UPDATE hazard_groups SET updated_at=? WHERE id=?", (now, item["group_id"]))
        return self.get(hazard_id)

    def groups(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM hazard_groups ORDER BY created_at DESC")]
        return [item for ident in ids if (item := self.get_group(ident)) is not None]

    def get_group(self, group_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM hazard_groups WHERE id=?", (group_id,)).fetchone()
            if not row:
                return None
            hazard_ids = [item[0] for item in db.execute("SELECT id FROM hazards WHERE group_id=? ORDER BY created_at", (group_id,))]
            notes = db.execute("SELECT * FROM notifications WHERE group_id=? ORDER BY created_at", (group_id,)).fetchall()
        value = dict(row)
        value["hazards"] = [item for ident in hazard_ids if (item := self.get(ident)) is not None]
        value["notifications"] = [self._notification_dict(item) for item in notes]
        statuses = {item["status"] for item in value["hazards"]}
        if value["hazards"] and statuses <= {"已闭合", "已误报", "误报/已作废"}:
            value["status"] = "已闭合"
        elif "待核实" in statuses:
            value["status"] = "待核实"
        elif statuses & {"待复核", "待重大确认"}:
            value["status"] = "待复核"
        else:
            value["status"] = "整改中"
        return value

    def audit_events(self, hazard_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM hazard_audit_events WHERE hazard_id=? ORDER BY created_at", (hazard_id,)).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["before_value"] = json.loads(value["before_value"] or "{}")
            value["after_value"] = json.loads(value["after_value"] or "{}")
            values.append(value)
        return values

    def review(self, hazard_id: str, passed: bool, reviewer: str, comment: str) -> dict[str, Any] | None:
        with self._connect() as db:
            item = db.execute("SELECT * FROM hazards WHERE id=?", (hazard_id,)).fetchone()
            if not item: return None
            status = "待重大确认" if passed and item["severity"] == "major" else "已闭合" if passed else "复核退回"
            now = now_iso()
            db.execute("UPDATE hazards SET status=?,updated_at=?,version=version+1 WHERE id=?", (status, now, hazard_id))
            db.execute("UPDATE rectifications SET review_result=?,reviewer=?,review_comment=? WHERE id=(SELECT id FROM rectifications WHERE hazard_id=? ORDER BY submitted_at DESC LIMIT 1)", ("通过" if passed else "退回", reviewer, comment, hazard_id))
            self._insert_audit(db, hazard_id, "review_passed" if passed else "review_rejected", None, reviewer, {"status": item["status"], "version": item["version"]}, {"status": status, "comment": comment, "version": int(item["version"] or 1) + 1}, now)
            if item["group_id"]:
                db.execute("UPDATE hazard_groups SET updated_at=? WHERE id=?", (now, item["group_id"]))
        return self.get(hazard_id)

    def confirm_major(self, hazard_id: str, reviewer: str) -> dict[str, Any] | None:
        with self._connect() as db:
            item = db.execute("SELECT * FROM hazards WHERE id=? AND status='待重大确认'", (hazard_id,)).fetchone()
            if not item: return None
            now = now_iso()
            db.execute("UPDATE hazards SET status='已闭合',updated_at=?,version=version+1 WHERE id=?", (now, hazard_id))
            self._insert_audit(db, hazard_id, "major_confirmed", None, reviewer, {"status": item["status"], "version": item["version"]}, {"status": "已闭合", "version": int(item["version"] or 1) + 1}, now)
            if item["group_id"]:
                db.execute("UPDATE hazard_groups SET updated_at=? WHERE id=?", (now, item["group_id"]))
        return self.get(hazard_id)

    def notifications_for_user(self, user: dict[str, Any], *, mark_read: bool = False) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM notifications ORDER BY created_at DESC").fetchall()
            visible: list[sqlite3.Row] = []
            for row in rows:
                if project_store.owner("group", row["group_id"]) != current_project.get():
                    continue
                value = self._notification_dict(row)
                roles = {str(record.get("role")) for record in value.get("delivery_records", [])}
                if user["role"] == "system_admin" or user["role"] in roles or (not roles and self._notification_matches_role(db, row, user["role"])):
                    visible.append(row)
            if mark_read and visible:
                now = now_iso()
                db.executemany("INSERT OR REPLACE INTO notification_reads(notification_id,user_id,read_at) VALUES(?,?,?)", [(row["id"], user["id"], now) for row in visible])
            reads = {row[0]: row[1] for row in db.execute("SELECT notification_id,read_at FROM notification_reads WHERE user_id=?", (user["id"],))}
        return [self._notification_dict(row, read_at=reads.get(row["id"])) for row in visible]

    def mark_notification_read_for_user(self, notification_id: str, user: dict[str, Any]) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM notifications WHERE id=?", (notification_id,)).fetchone()
            if not row:
                return None
            if project_store.owner("group", row["group_id"]) != current_project.get():
                return None
            value = self._notification_dict(row)
            roles = {str(record.get("role")) for record in value.get("delivery_records", [])}
            if user["role"] != "system_admin" and user["role"] not in roles and not (not roles and self._notification_matches_role(db, row, user["role"])):
                return None
            read_at = now_iso()
            db.execute("INSERT OR REPLACE INTO notification_reads(notification_id,user_id,read_at) VALUES(?,?,?)", (notification_id, user["id"], read_at))
        return self._notification_dict(row, read_at=read_at)

    def unread_count_for_user(self, user: dict[str, Any]) -> int:
        return sum(1 for item in self.notifications_for_user(user) if not item["read"])

    # Legacy helpers are kept for API compatibility and migration tests.
    def notifications(self, *, mark_read: bool = False) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM notifications ORDER BY created_at DESC").fetchall()
            if mark_read:
                db.execute("UPDATE notifications SET read_at=? WHERE read_at IS NULL", (now_iso(),))
        return [self._notification_dict(row) for row in rows]

    def mark_notification_read(self, notification_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            db.execute("UPDATE notifications SET read_at=? WHERE id=?", (now_iso(), notification_id))
            row = db.execute("SELECT * FROM notifications WHERE id=?", (notification_id,)).fetchone()
        return self._notification_dict(row) if row else None

    def unread_count(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM notifications WHERE read_at IS NULL").fetchone()[0])

    @staticmethod
    def _notification_matches_role(db: sqlite3.Connection, row: sqlite3.Row, role: str) -> bool:
        hazard = db.execute("SELECT severity FROM hazards WHERE id=?", (row["hazard_id"],)).fetchone()
        allowed = {"project_manager", "safety_director"} if hazard and hazard["severity"] == "major" else {"safety_officer", "safety_director", "work_area_manager"}
        return role in allowed

    @staticmethod
    def _insert_audit(db: sqlite3.Connection, hazard_id: str, action: str, actor_id: str | None, actor_name: str, before: dict[str, Any], after: dict[str, Any], created_at: str) -> None:
        db.execute("INSERT INTO hazard_audit_events(id,hazard_id,action,actor_id,actor_name,before_value,after_value,created_at) VALUES(?,?,?,?,?,?,?,?)", (f"AUD-{uuid.uuid4().hex[:12]}", hazard_id, action, actor_id, actor_name, json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), created_at))

    def _hydrate(self, value: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM rectifications WHERE hazard_id=? ORDER BY submitted_at", (value["id"],)).fetchall()
        value["rectifications"] = [dict(row) for row in rows]
        value["rectification_order"] = next((item for item in value["rectifications"] if item.get("record_type") == "order"), None)
        value["rectification_replies"] = [item for item in value["rectifications"] if item.get("record_type") == "reply"]
        with self._connect() as db:
            notes = db.execute("SELECT * FROM notifications WHERE hazard_id=? OR (group_id IS NOT NULL AND group_id=?) ORDER BY created_at", (value["id"], value.get("group_id"))).fetchall()
        value["notifications"] = [self._notification_dict(row) for row in notes]
        value["audit_events"] = self.audit_events(value["id"])
        if value.get("group_id"):
            with self._connect() as db:
                group = db.execute("SELECT order_id FROM hazard_groups WHERE id=?", (value["group_id"],)).fetchone()
            value["group_order_id"] = group["order_id"] if group else None
        value["severity_name"] = "重大隐患" if value["severity"] == "major" else "一般隐患"
        return value

    @staticmethod
    def _notification_dict(row: sqlite3.Row | None, *, read_at: str | None = None) -> dict[str, Any]:
        if row is None:
            return {}
        value = dict(row)
        try:
            value["delivery_records"] = json.loads(value.get("delivery_records") or "[]")
        except json.JSONDecodeError:
            value["delivery_records"] = []
        if read_at is not None:
            value["read_at"] = read_at
        value["read"] = bool(value.get("read_at"))
        return value


workflow_store = WorkflowStore()
