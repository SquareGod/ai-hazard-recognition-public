from __future__ import annotations

import unittest
import hashlib

from app.stream_gateway import PlaybackUrls
from app.media_paths import MediaPathLeaseManager
from app.stream_sessions import StreamSessionManager, StreamSessionStartError
from app.video_sources import VideoSourceSpec


EXPECTED_PATH = f"cam-cam-1-{hashlib.sha256('cam-1'.encode('utf-8')).hexdigest()[:12]}"


class FakeGateway:
    def __init__(self) -> None:
        self.registered: list[tuple[str, str]] = []
        self.removed: list[str] = []

    def register_path(self, name: str, source_url: str) -> None:
        self.registered.append((name, source_url))

    def remove_path(self, name: str) -> None:
        self.removed.append(name)

    def playback_urls(self, name: str) -> PlaybackUrls:
        return PlaybackUrls(
            rtsp=f"rtsp://127.0.0.1:8554/{name}",
            webrtc=f"http://127.0.0.1:8889/{name}/whep",
            hls=f"http://127.0.0.1:8888/{name}/index.m3u8",
        )


class FakeInference:
    def __init__(self) -> None:
        self.started_with = ""
        self.stopped: list[str] = []
        self.raise_on_start = False
        self.ready = True
        self.raise_on_stop = False

    def start(self, camera_id: str, source_url: str, inference_fps: float, *, work_area: str, auto_email: bool) -> dict:
        if self.raise_on_start:
            raise RuntimeError("boom")
        self.started_with = source_url
        return {"stream_id": "infer-1", "status": "starting"}

    def wait_ready(self, stream_id: str, timeout: float) -> dict:
        if self.ready:
            return {"stream_id": stream_id, "status": "running"}
        return {"stream_id": stream_id, "status": "failed", "last_error": "无法打开视频流"}

    def stop(self, stream_id: str) -> dict:
        if self.raise_on_stop:
            raise RuntimeError("stop failed")
        self.stopped.append(stream_id)
        return {"stream_id": stream_id, "status": "stopping"}


def sample_spec() -> VideoSourceSpec:
    return VideoSourceSpec("cam-1", "一号摄像头", "rtsp", "rtsp://u:p@10.0.0.8/live", "A1工区")


class StreamSessionTests(unittest.TestCase):
    def test_start_registers_gateway_then_starts_inference_from_internal_rtsp(self) -> None:
        gateway, inference = FakeGateway(), FakeInference()
        manager = StreamSessionManager(gateway=gateway, inference=inference)
        manager.add_source(sample_spec())

        session = manager.start("cam-1", inference_fps=2, auto_email=True)

        self.assertEqual([(EXPECTED_PATH, sample_spec().source_url)], gateway.registered)
        self.assertEqual(f"rtsp://127.0.0.1:8554/{EXPECTED_PATH}", inference.started_with)
        self.assertEqual("infer-1", session["inference_stream_id"])
        self.assertTrue(session["playback_urls"]["hls"].endswith(f"/{EXPECTED_PATH}/index.m3u8"))
        self.assertNotIn("rtsp", session["playback_urls"])
        self.assertNotIn("u:p", str(session))

    def test_failed_inference_rolls_back_gateway_path(self) -> None:
        gateway, inference = FakeGateway(), FakeInference()
        inference.raise_on_start = True
        manager = StreamSessionManager(gateway=gateway, inference=inference)
        manager.add_source(sample_spec())

        with self.assertRaises(StreamSessionStartError):
            manager.start("cam-1", inference_fps=2, auto_email=False)

        self.assertEqual([EXPECTED_PATH], gateway.removed)

    def test_asynchronous_inference_failure_rolls_back_gateway_path(self) -> None:
        gateway, inference = FakeGateway(), FakeInference()
        inference.ready = False
        manager = StreamSessionManager(gateway=gateway, inference=inference)
        manager.add_source(sample_spec())

        with self.assertRaisesRegex(StreamSessionStartError, "无法打开视频流"):
            manager.start("cam-1", inference_fps=2, auto_email=False)

        self.assertEqual(["infer-1"], inference.stopped)
        self.assertEqual([EXPECTED_PATH], gateway.removed)

    def test_duplicate_start_reuses_single_active_session(self) -> None:
        gateway, inference = FakeGateway(), FakeInference()
        manager = StreamSessionManager(gateway=gateway, inference=inference)
        manager.add_source(sample_spec())

        first = manager.start("cam-1", inference_fps=2, auto_email=False)
        second = manager.start("cam-1", inference_fps=2, auto_email=False)

        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(1, len(gateway.registered))

    def test_stop_is_idempotent_and_removes_gateway_once(self) -> None:
        gateway, inference = FakeGateway(), FakeInference()
        manager = StreamSessionManager(gateway=gateway, inference=inference)
        manager.add_source(sample_spec())
        session = manager.start("cam-1", inference_fps=2, auto_email=False)

        first = manager.stop(session["session_id"])
        second = manager.stop(session["session_id"])

        self.assertEqual("stopped", first["status"])
        self.assertEqual("stopped", second["status"])
        self.assertEqual([EXPECTED_PATH], gateway.removed)
        self.assertEqual(["infer-1"], inference.stopped)

    def test_stopping_analysis_keeps_a_path_leased_by_monitoring(self) -> None:
        gateway, inference = FakeGateway(), FakeInference()
        leases = MediaPathLeaseManager(gateway)
        manager = StreamSessionManager(gateway=gateway, inference=inference, leases=leases)
        manager.add_source(sample_spec())
        session = manager.start("cam-1", inference_fps=2, auto_email=False)
        leases.acquire("monitor:ticket:cam-1", sample_spec())

        manager.stop(session["session_id"])

        self.assertEqual([], gateway.removed)
        leases.release("monitor:ticket:cam-1")
        self.assertEqual([EXPECTED_PATH], gateway.removed)

    def test_stop_failure_marks_failed_but_never_deadlocks_next_start(self) -> None:
        gateway, inference = FakeGateway(), FakeInference()
        manager = StreamSessionManager(gateway=gateway, inference=inference)
        manager.add_source(sample_spec())
        session = manager.start("cam-1", inference_fps=2, auto_email=False)
        inference.raise_on_stop = True

        failed = manager.stop(session["session_id"])
        self.assertEqual("failed", failed["status"])
        self.assertIn("算法任务停止失败", failed["last_error"])
        # 路径清理成功即在失败停止中完成，不能再被后续启动阻塞。
        self.assertEqual([EXPECTED_PATH], gateway.removed)

        # 残留会话（算法停止失败）不再把启动永久卡死：start自动清理后直接重建。
        inference.raise_on_stop = False
        restarted = manager.start("cam-1", inference_fps=2, auto_email=False)
        self.assertNotEqual(session["session_id"], restarted["session_id"])
        self.assertEqual("running", restarted["status"])

        stopped = manager.stop(restarted["session_id"])
        self.assertEqual("stopped", stopped["status"])
        self.assertEqual([EXPECTED_PATH, EXPECTED_PATH], gateway.removed)
        self.assertEqual(["infer-1"], inference.stopped)

    def test_non_ascii_source_ids_get_distinct_stable_paths(self) -> None:
        gateway, inference = FakeGateway(), FakeInference()
        manager = StreamSessionManager(gateway=gateway, inference=inference)
        first = VideoSourceSpec("一号", "一号", "rtsp", "rtsp://10.0.0.1/live", "A1")
        second = VideoSourceSpec("二号", "二号", "rtsp", "rtsp://10.0.0.2/live", "A1")
        manager.add_source(first)
        manager.add_source(second)

        manager.start("一号", inference_fps=2, auto_email=False)
        manager.start("二号", inference_fps=2, auto_email=False)

        self.assertNotEqual(gateway.registered[0][0], gateway.registered[1][0])

    def test_long_source_ids_keep_hash_suffix_after_path_truncation(self) -> None:
        gateway, inference = FakeGateway(), FakeInference()
        manager = StreamSessionManager(gateway=gateway, inference=inference)
        prefix = "camera-" + ("a" * 85)
        for source_id, host in ((prefix + "-one", "10.0.0.1"), (prefix + "-two", "10.0.0.2")):
            manager.add_source(VideoSourceSpec(source_id, source_id, "rtsp", f"rtsp://{host}/live", "A1"))
            manager.start(source_id, inference_fps=2, auto_email=False)

        first_path, second_path = gateway.registered[0][0], gateway.registered[1][0]
        self.assertNotEqual(first_path, second_path)
        self.assertLessEqual(len(first_path), 80)


    def test_stale_failed_session_is_auto_healed_before_new_start(self) -> None:
        """停止中途失败留下的残留会话，start必须自动清理，而不是永久拒绝启动。"""
        gateway, inference = FakeGateway(), FakeInference()
        manager = StreamSessionManager(gateway=gateway, inference=inference)
        manager.add_source(sample_spec())
        from app.stream_sessions import StreamSession
        from app.stream_gateway import PlaybackUrls as RawUrls

        stale = StreamSession(
            session_id="stale-1", source_id="cam-1", path_name=EXPECTED_PATH,
            inference_stream_id="infer-stale", status="failed",
            playback_urls=RawUrls(rtsp="rtsp://internal", webrtc="http://w", hls="http://h"),
        )
        manager._sessions["stale-1"] = stale
        manager._active_by_source["cam-1"] = "stale-1"

        session = manager.start("cam-1", inference_fps=2, auto_email=False)

        self.assertEqual("stopped", stale.status)
        self.assertEqual(["infer-stale"], inference.stopped)
        self.assertEqual("infer-1", session["inference_stream_id"])
        self.assertEqual(session["session_id"], manager._active_by_source["cam-1"])
        self.assertEqual([(EXPECTED_PATH, sample_spec().source_url)], gateway.registered)


if __name__ == "__main__":
    unittest.main()
