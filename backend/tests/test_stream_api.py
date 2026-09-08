from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


class StreamApiTests(unittest.TestCase):
    def test_remote_control_requires_configured_key_and_valid_header(self) -> None:
        with patch("app.main.settings", SimpleNamespace(control_api_key="secret")):
            with TestClient(app, client=("203.0.113.10", 50000)) as client:
                denied = client.get("/api/v1/video-sources")
                allowed = client.get("/api/v1/video-sources", headers={"X-Control-Key": "secret"})
                monitor_denied = client.get("/api/v1/monitor/cameras")
                media_denied = client.get("/api/v1/media/ticket/camera/hls/index.m3u8")
                proxy_headers = {"X-Control-Key": "secret", "X-Forwarded-For": "198.51.100.20"}
                monitor_via_proxy = client.get("/api/v1/monitor/cameras", headers=proxy_headers)
                media_via_proxy = client.get("/api/v1/media/ticket/camera/hls/index.m3u8", headers=proxy_headers)
        self.assertEqual(401, denied.status_code)
        self.assertEqual(200, allowed.status_code)
        self.assertEqual(401, monitor_denied.status_code)
        self.assertEqual(401, media_denied.status_code)
        self.assertEqual(200, monitor_via_proxy.status_code)
        self.assertEqual(404, media_via_proxy.status_code)
        self.assertNotIn("控制接口未授权", media_via_proxy.text)

    def test_remote_control_fails_closed_when_key_is_not_configured(self) -> None:
        with patch("app.main.settings", SimpleNamespace(control_api_key="")):
            with TestClient(app, client=("203.0.113.10", 50000)) as client:
                response = client.get("/api/v1/video-sources")
        self.assertEqual(503, response.status_code)
    def test_video_source_and_session_routes_delegate_to_manager(self) -> None:
        source = {"id": "cam-1", "name": "测试", "source_type": "rtsp", "source_url": "rtsp://10.0.0.8/live", "work_area": "A1", "enabled": True}
        session = {"session_id": "s-1", "inference_stream_id": "i-1", "source_id": "cam-1", "status": "running", "playback_urls": {"rtsp": "r", "webrtc": "w", "hls": "h"}}
        with (
            patch("app.main.stream_session_manager.add_source", return_value=source) as add,
            patch("app.main.stream_session_manager.start", return_value=session) as start,
            TestClient(app, client=("127.0.0.1", 50000)) as client,
        ):
            source_response = client.post("/api/v1/video-sources", json={"id": "cam-1", "name": "测试", "source_type": "rtsp", "source_url": "rtsp://u:p@10.0.0.8/live", "work_area": "A1"})
            session_response = client.post("/api/v1/stream-sessions", json={"source_id": "cam-1", "inference_fps": 2, "auto_email": True})
        self.assertEqual(200, source_response.status_code)
        self.assertEqual(200, session_response.status_code)
        self.assertEqual("cam-1", add.call_args.args[0].id)
        start.assert_called_once_with("cam-1", inference_fps=2.0, auto_email=True)

    def test_source_request_rejects_extra_severity(self) -> None:
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            response = client.post("/api/v1/video-sources", json={"id": "cam-1", "name": "测试", "source_type": "rtsp", "source_url": "rtsp://10.0.0.8/live", "work_area": "A1", "severity": "major"})
        self.assertEqual(422, response.status_code)


if __name__ == "__main__":
    unittest.main()
