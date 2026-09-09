from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.workflow_store import WorkflowStore


class WorkflowBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = WorkflowStore(Path(self.temp.name) / "workflow.sqlite3")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def finding(label_id: str = "H003", severity: str = "general") -> dict:
        return {
            "label_id": label_id,
            "name": "未佩戴安全帽",
            "final_status": "confirmed_hazard",
            "severity": severity,
            "severity_name": "重大隐患" if severity == "major" else "一般隐患",
            "evidence": ["检测到人员头部未见安全帽"],
        }

    def test_job_and_stream_retries_are_idempotent_and_create_order(self) -> None:
        first = self.store.create_from_findings(
            job_id="job-1",
            work_area="示范工区",
            findings=[self.finding()],
            source_key_prefix="stream:s-1:frame-1",
            before_image="before.jpg",
        )
        second = self.store.create_from_findings(
            job_id="job-1",
            work_area="示范工区",
            findings=[self.finding()],
            source_key_prefix="stream:s-1:frame-1",
        )
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(1, len(self.store.list()))
        item = first[0]
        self.assertTrue(item["id"].startswith("HZ-"))
        self.assertEqual("order", item["rectification_order"]["record_type"])
        self.assertEqual([], item["rectification_replies"])
        self.assertEqual("pending", item["notifications"][0]["delivery_status"])

    def test_verification_false_positive_and_read_state(self) -> None:
        item = self.store.create_from_findings(job_id="job-2", work_area="示范工区", findings=[self.finding()])[0]
        verified = self.store.verify(item["id"], "安全员", True)
        self.assertEqual("待整改", verified["status"])
        self.assertEqual("安全员", verified["verified_by"])
        other = self.store.create_from_findings(job_id="job-3", work_area="示范工区", findings=[self.finding("H004")])[0]
        false_positive = self.store.verify(other["id"], "安全员", False, "现场已佩戴")
        self.assertEqual("已误报", false_positive["status"])
        self.assertEqual("现场已佩戴", false_positive["false_positive_reason"])
        notification = self.store.notifications()[0]
        self.assertFalse(notification["read"])
        self.assertTrue(self.store.mark_notification_read(notification["id"])["read"])
        self.assertEqual(1, self.store.unread_count())

    def test_rectification_review_major_confirmation_and_email_status(self) -> None:
        item = self.store.create_from_findings(job_id="job-4", work_area="示范工区", findings=[self.finding(severity="major")])[0]
        self.assertEqual("待核实", item["status"])
        self.store.verify(item["id"], "安全员", True)
        updated = self.store.submit_rectification(item["id"], "已加装防护并复查", "工区负责人", "after.jpg")
        self.assertEqual("待复核", updated["status"])
        self.assertEqual("order", updated["rectification_order"]["record_type"])
        self.assertEqual("reply", updated["rectification_replies"][0]["record_type"])
        reviewed = self.store.review(item["id"], True, "安全总监", "整改合格")
        self.assertEqual("待重大确认", reviewed["status"])
        closed = self.store.confirm_major(item["id"], "项目经理")
        self.assertEqual("已闭合", closed["status"])
        self.store.record_email_status([item["id"]], [{"role": "项目经理", "status": "sent", "email": "p***@example.com"}])
        final = self.store.get(item["id"])
        self.assertEqual("sent", final["notifications"][0]["delivery_status"])
        self.assertEqual("p***@example.com", final["notifications"][0]["delivery_records"][0]["email"])

    def test_public_workflow_routes_use_persistent_store(self) -> None:
        item = self.store.create_from_findings(job_id="job-api", work_area="示范工区", findings=[self.finding()])[0]
        user = {"id": "USR-test", "name": "管理员", "role": "system_admin", "work_area": "全部工区", "enabled": True, "must_change_password": False}
        with patch("app.main.workflow_store", self.store), patch("app.main.require_session", return_value=user), TestClient(__import__("app.main", fromlist=["app"]).app, client=("127.0.0.1", 50000)) as client:
            verified = client.post(f"/api/v1/hazards/{item['id']}/verify", json={"passed": True, "verifier": "安全员"})
            self.assertEqual(200, verified.status_code)
            reply = client.post(f"/api/v1/hazards/{item['id']}/rectifications", json={"description": "已完成整改", "submitted_by": "工区负责人", "after_image": "after.jpg"})
            self.assertEqual(200, reply.status_code)
            reviewed = client.post(f"/api/v1/hazards/{item['id']}/reviews", json={"passed": True, "reviewer": "安全员", "comment": "合格"})
            self.assertEqual(200, reviewed.status_code)
            note_id = self.store.notifications()[0]["id"]
            read = client.post(f"/api/v1/notifications/{note_id}/read")
            self.assertEqual(200, read.status_code)
            hazards = client.get("/api/v1/hazards")
        self.assertEqual("已闭合", hazards.json()[0]["status"])


if __name__ == "__main__":
    unittest.main()
