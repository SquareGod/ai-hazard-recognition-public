from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.auth import AuthStore
from app.main import app
from app.workflow_store import WorkflowStore


class AuthAndGroupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "workflow.sqlite3"
        self.auth = AuthStore(self.db)
        self.workflow = WorkflowStore(self.db)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_password_is_argon2id_and_first_login_requires_change(self) -> None:
        user, temporary = self.auth.create_user(
            name="张安全", email="safety@example.com", role="safety_officer", work_area="示范工区"
        )
        self.assertNotIn("password", user)
        db = sqlite3.connect(self.db)
        try:
            password_hash = db.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()[0]
        finally:
            db.close()
        self.assertTrue(password_hash.startswith("$argon2id$"))
        logged_in, _token, _csrf = self.auth.authenticate(user["email"], temporary)
        self.assertTrue(logged_in["must_change_password"])
        self.auth.change_password(user["id"], temporary, "Changed-Password-2026")
        changed, _token, _csrf = self.auth.authenticate(user["email"], "Changed-Password-2026")
        self.assertFalse(changed["must_change_password"])

    def test_one_image_creates_one_order_with_independent_children(self) -> None:
        findings = [
            {"label_id": "H001", "name": "未佩戴安全帽", "final_status": "confirmed_hazard", "severity": "general", "evidence": ["头部未见安全帽"], "source_frame_ids": ["frame-1"]},
            {"label_id": "H015", "name": "洞口无盖板", "final_status": "confirmed_hazard", "severity": "major", "evidence": ["洞口敞开"], "source_frame_ids": ["frame-1"]},
        ]
        children = self.workflow.create_from_findings(job_id="job-1", work_area="示范工区", findings=findings, before_image="evidence.jpg")
        self.assertEqual(2, len(children))
        self.assertEqual(1, len({item["group_id"] for item in children}))
        group = self.workflow.get_group(children[0]["group_id"])
        self.assertEqual(2, len(group["hazards"]))
        self.assertEqual(1, len({item["group_order_id"] for item in group["hazards"]}))
        db = sqlite3.connect(self.db)
        try:
            order_count = db.execute("SELECT COUNT(*) FROM rectifications r JOIN hazards h ON h.id=r.hazard_id WHERE h.group_id=? AND r.record_type='order'", (group["id"],)).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(1, order_count)
        first = self.workflow.verify(children[0]["id"], "安全员", False, "误报", expected_version=1)
        second = self.workflow.verify(children[1]["id"], "安全总监", True, expected_version=1)
        self.assertEqual("已误报", first["status"])
        self.assertEqual("待整改", second["status"])
        self.assertNotEqual("已闭合", self.workflow.get_group(group["id"])["status"])

    def test_notification_read_state_is_per_user(self) -> None:
        safety, _ = self.auth.create_user(name="张安全", email="a@example.com", role="safety_officer", work_area="示范工区")
        director, _ = self.auth.create_user(name="赵总监", email="b@example.com", role="safety_director", work_area="全部工区")
        child = self.workflow.create_from_findings(job_id="job-2", work_area="示范工区", findings=[{"label_id": "H001", "name": "未佩戴安全帽", "final_status": "confirmed_hazard", "severity": "general", "evidence": ["可见"]}])[0]
        self.workflow.record_email_status([child["id"]], [{"role": "safety_officer", "status": "sent"}, {"role": "safety_director", "status": "sent"}])
        note_id = self.workflow.notifications_for_user(safety)[0]["id"]
        self.workflow.mark_notification_read_for_user(note_id, safety)
        self.assertEqual(0, self.workflow.unread_count_for_user(safety))
        self.assertEqual(1, self.workflow.unread_count_for_user(director))

    def test_login_csrf_and_optimistic_verification_route(self) -> None:
        user, temporary = self.auth.create_user(name="张安全", email="route@example.com", role="safety_officer", work_area="示范工区")
        self.auth.change_password(user["id"], temporary, "Changed-Password-2026")
        child = self.workflow.create_from_findings(job_id="job-route", work_area="示范工区", findings=[{"label_id": "H001", "name": "未佩戴安全帽", "final_status": "confirmed_hazard", "severity": "general", "evidence": ["可见"]}])[0]
        with patch("app.auth.auth_store", self.auth), patch("app.main.auth_store", self.auth), patch("app.main.workflow_store", self.workflow), TestClient(app) as client:
            login = client.post("/api/v1/auth/login", json={"email": "route@example.com", "password": "Changed-Password-2026"})
            self.assertEqual(200, login.status_code)
            csrf = login.json()["csrf_token"]
            groups = client.get("/api/v1/hazard-groups")
            self.assertEqual(200, groups.status_code)
            url = f"/api/v1/hazard-groups/{child['group_id']}/hazards/{child['id']}/verify"
            conflict = client.post(url, headers={"X-CSRF-Token": csrf}, json={"passed": True, "version": 99})
            self.assertEqual(409, conflict.status_code)
            saved = client.post(url, headers={"X-CSRF-Token": csrf}, json={"passed": True, "version": 1, "description_correct": True})
            self.assertEqual(200, saved.status_code)
            self.assertEqual("待整改", saved.json()["hazard"]["status"])


if __name__ == "__main__":
    unittest.main()
