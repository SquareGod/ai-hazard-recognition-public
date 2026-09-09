from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


class ApiTests(unittest.TestCase):
    def test_health_and_catalog(self) -> None:
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            health = client.get("/api/v1/health")
            self.assertEqual(200, health.status_code)
            self.assertEqual(65, health.json()["catalog_size"])
            catalog = client.get("/api/v1/catalog")
            self.assertEqual(200, catalog.status_code)
            self.assertEqual(65, len(catalog.json()))

    def test_notification_status_does_not_expose_secrets(self) -> None:
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            response = client.get("/api/v1/notifications/status")
            self.assertEqual(200, response.status_code)
            self.assertIn("smtp_configured", response.json())
            self.assertNotIn("password", response.text.lower())

    def test_stream_request_accepts_reserved_notification_fields(self) -> None:
        from app.schemas import StreamStartRequest

        request = StreamStartRequest(
            camera_id="CAM-TEST",
            source_url="rtsp://127.0.0.1/test",
            work_area="示范工区",
            auto_email=True,
        )
        self.assertEqual("示范工区", request.work_area)
        self.assertFalse(hasattr(request, "severity"))
        self.assertTrue(request.auto_email)

    def test_legacy_http_stream_uses_compatibility_manager(self) -> None:
        state = {"stream_id": "legacy-1", "camera_id": "CAM", "status": "starting"}
        with (
            patch("app.main.stream_manager.start", return_value=state) as starter,
            TestClient(app, client=("127.0.0.1", 50000)) as client,
        ):
            response = client.post(
                "/api/v1/streams/start",
                json={"camera_id": "CAM", "source_url": "http://127.0.0.1/live.m3u8", "work_area": "A1"},
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual("legacy-1", response.json()["stream_id"])
        starter.assert_called_once()

    def test_batch_hazard_email_contains_all_findings(self) -> None:
        from app.notifications import build_hazard_summary_email

        subject, body = build_hazard_summary_email(
            job_id="job-test",
            hazard_names=["洞口未防护", "未佩戴安全帽"],
            severity="general",
            work_area="示范工区",
            deadline="24小时内",
        )
        self.assertIn("2项一般隐患", subject)
        self.assertIn("1. 洞口未防护", body)
        self.assertIn("2. 未佩戴安全帽", body)

    def test_manual_severity_is_rejected_by_public_requests(self) -> None:
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            stream = client.post(
                "/api/v1/streams/start",
                json={
                    "camera_id": "CAM-TEST",
                    "source_url": "rtsp://127.0.0.1/test",
                    "work_area": "示范工区",
                    "severity": "major",
                },
            )
            dispatch = client.post(
                "/api/v1/jobs/job-test/notifications/email",
                json={"work_area": "示范工区", "severity": "major"},
            )
        self.assertEqual(422, stream.status_code)
        self.assertEqual(422, dispatch.status_code)

    def test_email_dispatch_groups_by_model_severity(self) -> None:
        result = {
            "findings": [
                {"label_id": "H003", "name": "未佩戴安全帽", "final_status": "confirmed_hazard", "severity": "general"},
                {"label_id": "H015", "name": "预留洞口无盖板", "final_status": "confirmed_hazard", "severity": "major"},
            ]
        }
        with (
            patch("app.main.job_store.result", return_value=result),
            patch("app.main.resolve_recipients", return_value=[object()]),
            patch("app.main.send_email", return_value=[{"role": "test", "status": "sent"}]) as sender,
            TestClient(app) as client,
        ):
            response = client.post(
                "/api/v1/jobs/job-test/notifications/email",
                json={"work_area": "示范工区"},
            )
        self.assertEqual(200, response.status_code)
        dispatches = response.json()["dispatches"]
        self.assertEqual({"general", "major"}, {item["severity"] for item in dispatches})
        # One consolidated message per recipient role: three general roles and
        # two major roles, with safety director de-duplicated across severities.
        self.assertEqual(4, sender.call_count)


if __name__ == "__main__":
    unittest.main()
