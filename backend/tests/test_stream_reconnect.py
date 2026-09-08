from __future__ import annotations

import threading
import time
import unittest

import numpy as np

from app.streams import StreamManager


class _Capture:
    def __init__(self, frames: list[tuple[bool, object | None]]) -> None:
        self._frames = iter(frames)
        self.released = False

    def isOpened(self) -> bool:
        return True

    def read(self) -> tuple[bool, object | None]:
        return next(self._frames, (False, None))

    def release(self) -> None:
        self.released = True


class _OpenRaisesCapture(_Capture):
    def isOpened(self) -> bool:
        raise RuntimeError("open check failed")


class _ReadRaisesCapture(_Capture):
    def read(self) -> tuple[bool, object | None]:
        raise RuntimeError("read failed")


class _StopDuringOpenCapture(_Capture):
    def __init__(self) -> None:
        super().__init__([])
        self.open_checked = threading.Event()
        self.resume_open = threading.Event()
        self.release_started = threading.Event()
        self.resume_release = threading.Event()

    def isOpened(self) -> bool:
        self.open_checked.set()
        self.resume_open.wait(1.0)
        return True

    def release(self) -> None:
        self.release_started.set()
        self.resume_release.wait(1.0)
        super().release()


class _Detector:
    def detect(self, frame: object, mode: str, output_dir: object) -> list[object]:
        return []


class StreamReconnectTests(unittest.TestCase):
    def _wait_for(self, predicate: object, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("timed out waiting for stream state")

    def test_reopens_capture_after_read_failure_and_resets_backoff_on_frame(self) -> None:
        first = _Capture([(False, None)])
        frame_read = threading.Event()
        second = _Capture([(True, np.zeros((2, 2, 3), dtype=np.uint8)), (False, None)])
        captures = iter([first, second])
        waits: list[float] = []
        reconnecting = threading.Event()
        resume_reconnect = threading.Event()

        def interruptible_wait(stop_event: threading.Event, delay: float) -> bool:
            waits.append(delay)
            if len(waits) == 1:
                reconnecting.set()
                resume_reconnect.wait(1.0)
                return stop_event.is_set()
            stop_event.set()
            return True

        manager = StreamManager(
            capture_factory=lambda source: next(captures),
            interruptible_wait=interruptible_wait,
        )
        manager._detector = _Detector()
        original_read = second.read

        def read_second() -> tuple[bool, object | None]:
            result = original_read()
            if result[0]:
                frame_read.set()
            return result

        second.read = read_second
        stream = manager.start("camera-1", "rtsp://example/live", 1.0, work_area="A1", auto_email=False)

        self.assertTrue(reconnecting.wait(1.0))
        self.assertEqual("reconnecting", manager.list()[0]["connection_status"])
        resume_reconnect.set()
        self.assertTrue(frame_read.wait(1.0))
        self._wait_for(lambda: manager.list()[0]["status"] == "stopped")
        state = manager.list()[0]
        self.assertTrue(first.released)
        self.assertTrue(second.released)
        self.assertEqual([1, 1], waits)
        self.assertIsNotNone(state["last_frame_at"])
        self.assertEqual(1, state["reconnect_count"])
        self.assertEqual("stream_read_failed", state["last_error_code"])

    def test_stop_interrupts_reconnect_wait_without_waiting_for_delay(self) -> None:
        reconnect_waiting = threading.Event()
        first = _Capture([(False, None)])

        def interruptible_wait(stop_event: threading.Event, delay: float) -> bool:
            reconnect_waiting.set()
            return stop_event.wait(delay)

        manager = StreamManager(
            capture_factory=lambda source: first,
            interruptible_wait=interruptible_wait,
        )
        manager._detector = _Detector()
        stream = manager.start("camera-1", "rtsp://example/live", 1.0, work_area="A1", auto_email=False)

        self.assertTrue(reconnect_waiting.wait(1.0))
        started = time.monotonic()
        manager.stop(stream["stream_id"])
        self._wait_for(lambda: manager.list()[0]["status"] == "stopped", timeout=0.2)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertTrue(first.released)

    def test_releases_capture_when_open_check_raises_and_reconnects(self) -> None:
        first = _OpenRaisesCapture([])
        second = _Capture([(False, None)])
        captures = iter([first, second])
        second_opened = threading.Event()
        waits = 0

        def capture_factory(source: object) -> _Capture:
            capture = next(captures)
            if capture is second:
                second_opened.set()
            return capture

        def interruptible_wait(stop_event: threading.Event, delay: float) -> bool:
            nonlocal waits
            waits += 1
            if waits > 1:
                stop_event.set()
            return stop_event.is_set()

        manager = StreamManager(capture_factory=capture_factory, interruptible_wait=interruptible_wait)
        manager._detector = _Detector()
        stream = manager.start("camera-1", "rtsp://example/live", 1.0, work_area="A1", auto_email=False)

        self.assertTrue(second_opened.wait(1.0))
        self._wait_for(lambda: manager.list()[0]["status"] == "stopped")
        self.assertTrue(first.released)
        self.assertTrue(second.released)
        self.assertEqual(1, manager.list()[0]["reconnect_count"])

    def test_reconnects_after_read_exception_without_failing_stream(self) -> None:
        first = _ReadRaisesCapture([])
        second = _Capture([(True, np.zeros((2, 2, 3), dtype=np.uint8))])
        captures = iter([first, second])
        frame_read = threading.Event()
        waits = 0

        original_read = second.read

        def read_second() -> tuple[bool, object | None]:
            result = original_read()
            if result[0]:
                frame_read.set()
            return result

        second.read = read_second

        def interruptible_wait(stop_event: threading.Event, delay: float) -> bool:
            nonlocal waits
            waits += 1
            if waits > 1:
                stop_event.set()
            return stop_event.is_set()

        manager = StreamManager(
            capture_factory=lambda source: next(captures),
            interruptible_wait=interruptible_wait,
        )
        manager._detector = _Detector()
        stream = manager.start("camera-1", "rtsp://example/live", 1.0, work_area="A1", auto_email=False)

        self.assertTrue(frame_read.wait(1.0))
        self._wait_for(lambda: manager.list()[0]["status"] == "stopped")
        state = manager.list()[0]
        self.assertTrue(first.released)
        self.assertTrue(second.released)
        self.assertNotEqual("failed", state["status"])
        self.assertEqual(1, state["reconnect_count"])
        self.assertEqual("stream_read_failed", state["last_error_code"])
        self.assertEqual("取流中断，正在重试", state["last_error"])

    def test_stop_during_open_does_not_publish_connected_or_running(self) -> None:
        capture = _StopDuringOpenCapture()
        manager = StreamManager(capture_factory=lambda source: capture)
        manager._detector = _Detector()
        stream = manager.start("camera-1", "rtsp://example/live", 1.0, work_area="A1", auto_email=False)

        self.assertTrue(capture.open_checked.wait(1.0))
        manager.stop(stream["stream_id"])
        capture.resume_open.set()
        self.assertTrue(capture.release_started.wait(1.0))
        state = manager.list()[0]
        self.assertEqual("stopping", state["status"])
        self.assertEqual("connecting", state["connection_status"])
        capture.resume_release.set()
        self._wait_for(lambda: manager.list()[0]["status"] == "stopped")


if __name__ == "__main__":
    unittest.main()
