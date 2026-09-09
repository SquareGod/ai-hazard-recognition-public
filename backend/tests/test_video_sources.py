from __future__ import annotations

import unittest

from app.streams import redact_source

from app.video_sources import (
    HikvisionVideoSourceAdapter,
    RtspVideoSourceAdapter,
    VideoSourceSpec,
    VideoSourceUnavailable,
    get_video_source_adapter,
)


class VideoSourceAdapterTests(unittest.TestCase):
    def test_legacy_stream_redaction_removes_query_and_fragment_secrets(self) -> None:
        redacted = redact_source("rtsp://user:pass@10.0.0.8/live?token=secret#private")
        self.assertEqual("rtsp://***@10.0.0.8/live", redacted)

    def test_rtsp_adapter_preserves_url_but_public_dict_redacts_credentials(self) -> None:
        spec = VideoSourceSpec(
            id="cam-1",
            name="测试摄像头",
            source_type="rtsp",
            source_url="rtsp://user:secret@10.0.0.8/live?token=private",
            work_area="示范工区",
        )

        resolved = RtspVideoSourceAdapter().resolve(spec)

        self.assertEqual(spec.source_url, resolved.source_url)
        self.assertFalse(resolved.requires_transcode)
        self.assertEqual({"source_type": "rtsp"}, resolved.metadata)
        public_url = spec.public_dict()["source_url"]
        self.assertNotIn("user", public_url)
        self.assertNotIn("secret", public_url)
        self.assertNotIn("private", public_url)
        self.assertEqual("rtsp://***@10.0.0.8/live", public_url)

    def test_rtsp_adapter_rejects_non_rtsp_url_without_exposing_credentials(self) -> None:
        spec = VideoSourceSpec(
            id="cam-2",
            name="错误协议",
            source_type="rtsp",
            source_url="http://user:secret@10.0.0.8/live",
            work_area="示范工区",
        )

        with self.assertRaises(VideoSourceUnavailable) as caught:
            RtspVideoSourceAdapter().resolve(spec)

        message = str(caught.exception)
        self.assertIn("RTSP", message)
        self.assertNotIn("user", message)
        self.assertNotIn("secret", message)

    def test_hikvision_adapter_fails_with_actionable_reserved_message(self) -> None:
        spec = VideoSourceSpec(
            id="cam-hik-1",
            name="海康通道",
            source_type="hikvision",
            source_url="hikvision://admin:secret@10.0.0.9/1",
            work_area="A2工区",
        )

        with self.assertRaises(VideoSourceUnavailable) as caught:
            HikvisionVideoSourceAdapter().resolve(spec)

        message = str(caught.exception)
        self.assertIn("HCNetSDK", message)
        self.assertIn("RTSP", message)
        self.assertNotIn("admin", message)
        self.assertNotIn("secret", message)

    def test_factory_returns_adapter_and_unknown_type_fails_in_chinese(self) -> None:
        self.assertIsInstance(get_video_source_adapter("rtsp"), RtspVideoSourceAdapter)
        self.assertIsInstance(get_video_source_adapter("hikvision"), HikvisionVideoSourceAdapter)

        with self.assertRaises(VideoSourceUnavailable) as caught:
            get_video_source_adapter("onvif")

        self.assertIn("不支持的视频源类型", str(caught.exception))

    def test_factory_error_does_not_echo_credential_like_unknown_type(self) -> None:
        with self.assertRaises(VideoSourceUnavailable) as caught:
            get_video_source_adapter("rtsp://user:secret@10.0.0.8/live")

        message = str(caught.exception)
        self.assertIn("不支持的视频源类型", message)
        self.assertNotIn("user", message)
        self.assertNotIn("secret", message)

    def test_public_dict_keeps_non_sensitive_fields_and_default_enabled(self) -> None:
        spec = VideoSourceSpec(
            id="cam-3",
            name="无凭据摄像头",
            source_type="rtsp",
            source_url="rtsp://10.0.0.10/live",
            work_area="A3工区",
        )

        self.assertEqual(
            {
                "id": "cam-3",
                "name": "无凭据摄像头",
                "source_type": "rtsp",
                "source_url": "rtsp://10.0.0.10/live",
                "work_area": "A3工区",
                "enabled": True,
            },
            spec.public_dict(),
        )


if __name__ == "__main__":
    unittest.main()
