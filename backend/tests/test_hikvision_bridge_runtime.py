from __future__ import annotations

import io
import queue
import time
from pathlib import Path

import pytest

from hikvision_bridge.sdk_runtime import (
    BoundedFrameQueue, FakePublisher, FakeSdk, FfmpegPublisher, NET_DVR_STREAMDATA,
    NET_DVR_SYSHEAD, PlayCtrlDecoder, StreamPipeline, YuvFrame, yv12_to_i420,
)


def test_bounded_queue_keeps_latest_frame_without_blocking():
    frames = [YuvFrame(bytes([i]) * 6, 2, 2) for i in range(4)]
    q = BoundedFrameQueue(2)
    for frame in frames:
        q.put_latest(frame)
    assert q.dropped == 2
    assert q.get().data[0] == 2
    assert q.get().data[0] == 3


class FakePlayCtrl:
    DECCBFUNWIN = staticmethod(lambda fn: fn)

    def __init__(self):
        self.calls = []
        self.callback = None

    def PlayM4_GetPort(self, port):
        port._obj.value = 7; self.calls.append("get"); return 1

    def PlayM4_SetStreamOpenMode(self, port, mode): self.calls.append(("mode", port, mode)); return 1
    def PlayM4_OpenStream(self, port, buf, size, cap): self.calls.append("open"); return 1
    def PlayM4_SetDecodeEngine(self, port, engine): self.calls.append("engine"); return 1

    def PlayM4_SetDecCallBackExMend(self, port, cb, user, size, reserved):
        self.callback = cb; self.calls.append("callback"); return 1

    def PlayM4_Play(self, port, wnd): self.calls.append("play"); return 1
    def PlayM4_InputData(self, port, buf, size): self.calls.append("input"); return 1
    def PlayM4_Stop(self, port): self.calls.append("stop"); return 1
    def PlayM4_CloseStream(self, port): self.calls.append("close"); return 1
    def PlayM4_FreePort(self, port): self.calls.append("free"); return 1


def test_playctrl_decoder_follows_official_order_and_emits_yuv():
    playctrl = FakePlayCtrl(); got = []
    decoder = PlayCtrlDecoder(playctrl, got.append)
    decoder.on_sdk_data(NET_DVR_SYSHEAD, b"header")
    assert playctrl.calls[:6] == ["get", ("mode", 7, 0), "open", "engine", "callback", "play"]
    from hikvision_bridge.sdk_runtime import _FrameInfo
    info = _FrameInfo(2, 2, 0, 3, 10, 1)
    raw = (b"x" * 6)
    buffer = (type("Buf", (), {})())
    import ctypes
    cbuf = ctypes.create_string_buffer(raw)
    playctrl.callback(7, cbuf, len(raw), ctypes.pointer(info), None, None)
    decoder.on_sdk_data(NET_DVR_STREAMDATA, b"payload")
    assert got and got[0].data == yv12_to_i420(raw, 2, 2)
    assert "input" in playctrl.calls
    decoder.close()
    assert playctrl.calls[-3:] == ["stop", "close", "free"]


def test_ffmpeg_publisher_builds_h264_rtsp_command_and_closes_process():
    class FakeProc:
        def __init__(self): self.stdin = io.BytesIO(); self.terminated = False
        def poll(self): return None
        def terminate(self): self.terminated = True
        def wait(self, timeout): return 0
    commands = []
    proc = FakeProc()
    publisher = FfmpegPublisher("ffmpeg-test", "rtsp://127.0.0.1:8554", lambda cmd, **kw: (commands.append(cmd) or proc))
    frame = YuvFrame(b"x" * 6, 2, 2, 10)
    publisher.start("nvr-1/ch-1", frame)
    assert commands and "libx264" in commands[0] and commands[0][-1].endswith("nvr-1/ch-1")
    publisher.stop()
    assert proc.terminated


def test_pipeline_uses_one_sdk_preview_and_one_publisher_for_a_stream():
    sdk = FakeSdk(); publisher = FakePublisher()
    class Decoder:
        def __init__(self, on_frame): self.on_frame = on_frame
        def close(self): pass
    pipeline = StreamPipeline({"host": "fake", "port": 1}, "unused-test-secret", 1, "fake/1", sdk, publisher, Decoder)
    pipeline.start()
    import time; time.sleep(0.05)
    assert sdk.opens == 1
    pipeline.stop()
    assert publisher.stopped


def test_bridge_reaps_pipelines_whose_owners_heartbeat_expired():
    """全部owner心跳超时的管线被回收停止，心跳新鲜的管线保留。"""
    from hikvision_bridge import app as bridge_app

    class FakePipeline:
        def __init__(self): self.stopped = False
        def stop(self): self.stopped = True

    stale_pipeline, fresh_pipeline = FakePipeline(), FakePipeline()
    saved_pipelines, saved_leases = dict(bridge_app._pipelines), dict(bridge_app._leases)
    bridge_app._pipelines.clear(); bridge_app._leases.clear()
    bridge_app._pipelines["ch-stale"] = stale_pipeline
    bridge_app._pipelines["ch-fresh"] = fresh_pipeline
    bridge_app._leases["ch-stale"] = {"media-path:stale": time.time() - bridge_app.LEASE_TTL - 5}
    bridge_app._leases["ch-fresh"] = {"media-path:fresh": time.time()}
    try:
        bridge_app._reap_expired_leased_pipelines()
        assert stale_pipeline.stopped
        assert not fresh_pipeline.stopped
        assert "ch-stale" not in bridge_app._pipelines
        assert "ch-fresh" in bridge_app._pipelines
        assert "ch-stale" not in bridge_app._leases
    finally:
        bridge_app._pipelines.clear(); bridge_app._leases.clear()
        bridge_app._pipelines.update(saved_pipelines)
        bridge_app._leases.update({key: dict(value) for key, value in saved_leases.items()})


def test_pipeline_keeps_last_decoded_frame_for_snapshot_fast_path():
    """管线在推流时保留最近一帧，供缩略图快路径零成本取图；停止后清空。"""
    sdk = FakeSdk(); publisher = FakePublisher()
    class Decoder:
        def __init__(self, on_frame): self.on_frame = on_frame
        def close(self): pass
    pipeline = StreamPipeline({"host": "fake", "port": 1}, "unused-test-secret", 1, "fake/1", sdk, publisher, Decoder)
    pipeline.start()
    time.sleep(0.05)
    assert pipeline.last_frame is None
    frame = YuvFrame(b"y" * 6, 2, 2, 10)
    pipeline._on_frame(frame)
    deadline = time.time() + 2
    while time.time() < deadline and pipeline.last_frame is None:
        time.sleep(0.02)
    assert pipeline.last_frame is frame
    pipeline.stop()
    assert pipeline.last_frame is None


def test_yuv_frame_to_jpeg_encodes_via_short_lived_ffmpeg(monkeypatch):
    from hikvision_bridge import sdk_runtime as rt

    captured = {}

    class FakeCompleted:
        returncode = 0
        stdout = b"jpeg-bytes"
        stderr = b""

    def fake_run(command, input, stdout, stderr, timeout):
        captured["command"] = command
        captured["input"] = input
        return FakeCompleted()

    monkeypatch.setattr(rt.subprocess, "run", fake_run)
    out = rt.yuv_frame_to_jpeg(YuvFrame(b"x" * 6, 2, 2, 10), "ffmpeg-test")
    assert out == b"jpeg-bytes"
    assert captured["command"][0] == "ffmpeg-test"
    assert "-frames:v" in captured["command"] and "mjpeg" in captured["command"]
    assert captured["input"] == b"x" * 6


def test_yuv_frame_to_jpeg_surfaces_ffmpeg_failure(monkeypatch):
    from hikvision_bridge import sdk_runtime as rt

    class FakeFailed:
        returncode = 1
        stdout = b""
        stderr = b"boom"

    monkeypatch.setattr(rt.subprocess, "run", lambda *a, **k: FakeFailed())
    with pytest.raises(RuntimeError, match="boom"):
        rt.yuv_frame_to_jpeg(YuvFrame(b"x" * 6, 2, 2, 10), "ffmpeg-test")
