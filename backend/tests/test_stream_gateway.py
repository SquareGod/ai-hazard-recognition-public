from __future__ import annotations

import unittest

from app.stream_gateway import MediaMTXGateway, MediaMTXGatewayError, normalize_path_name


class RecordingTransport:
    def __init__(self, status: int = 200, payload: dict | None = None) -> None:
        self.status = status
        self.payload = payload or {"items": []}
        self.calls: list[tuple[str, str, dict | None]] = []

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        self.calls.append((method, path, payload))
        return self.status, self.payload


class StreamGatewayTests(unittest.TestCase):
    def test_register_path_uses_v3_api_and_safe_description_redacts_credentials(self) -> None:
        transport = RecordingTransport()
        gateway = MediaMTXGateway(
            "http://127.0.0.1:9997",
            username="api",
            password="secret",
            transport=transport,
        )
        source = "rtsp://user:camera-secret@10.0.0.8/live?token=private"

        gateway.register_path("cam-a1", source)

        self.assertEqual("POST", transport.calls[0][0])
        self.assertEqual("/v3/config/paths/add/cam-a1", transport.calls[0][1])
        self.assertEqual(source, transport.calls[0][2]["source"])
        safe = gateway.safe_description(transport.calls[0][2])
        self.assertNotIn("camera-secret", safe)
        self.assertNotIn("private", safe)

    def test_playback_urls_are_stable_and_path_is_normalized(self) -> None:
        gateway = MediaMTXGateway(
            "http://127.0.0.1:9997",
            rtsp_public_base="rtsp://127.0.0.1:8554",
            webrtc_public_base="http://127.0.0.1:8889",
            hls_public_base="http://127.0.0.1:8888",
            transport=RecordingTransport(),
        )
        urls = gateway.playback_urls("CAM A1_01")
        self.assertEqual("cam-a1-01", normalize_path_name("CAM A1_01"))
        self.assertEqual("rtsp://127.0.0.1:8554/cam-a1-01", urls.rtsp)
        self.assertEqual("http://127.0.0.1:8889/cam-a1-01/whep", urls.webrtc)
        self.assertEqual("http://127.0.0.1:8888/cam-a1-01/index.m3u8", urls.hls)
        self.assertNotIn("rtsp", urls.public_dict())

    def test_internal_rtsp_url_can_carry_algorithm_only_credentials(self) -> None:
        gateway = MediaMTXGateway(
            "http://127.0.0.1:9997",
            rtsp_public_base="rtsp://127.0.0.1:8554",
            rtsp_username="algorithm",
            rtsp_password="read-secret",
            transport=RecordingTransport(),
        )

        urls = gateway.playback_urls("cam-a1")

        self.assertEqual("rtsp://algorithm:read-secret@127.0.0.1:8554/cam-a1", urls.rtsp)
        self.assertNotIn("read-secret", str(urls.public_dict()))

    def test_remove_404_is_idempotent_but_other_errors_are_stable(self) -> None:
        missing = MediaMTXGateway("http://127.0.0.1:9997", transport=RecordingTransport(404))
        missing.remove_path("cam-a1")

        broken = MediaMTXGateway("http://127.0.0.1:9997", transport=RecordingTransport(500))
        with self.assertRaisesRegex(MediaMTXGatewayError, "流媒体网关操作失败"):
            broken.register_path("cam-a1", "rtsp://user:secret@10.0.0.8/live")

    def test_list_paths_returns_items(self) -> None:
        gateway = MediaMTXGateway(
            "http://127.0.0.1:9997",
            transport=RecordingTransport(payload={"items": [{"name": "cam-a1"}]}),
        )
        self.assertEqual([{"name": "cam-a1"}], gateway.list_paths())


if __name__ == "__main__":
    unittest.main()
