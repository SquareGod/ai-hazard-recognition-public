from __future__ import annotations

from app.config import settings
from app.streams import StreamManager, StreamState


def _state(mode: str, started_at: float) -> StreamState:
    return StreamState("s", "cam-1", "rtsp://127.0.0.1/live", 2.0, "A1工区", True, mode, 300, started_at=started_at)


def test_first_inspection_waits_for_warmup_then_repeats_by_interval():
    """接入即首审：第一次VLM审核在热身期(默认5秒)后到达，而不是首帧或一个完整巡检周期后。"""
    state = _state("realtime", started_at=1000.0)
    interval, first_due = StreamManager._initial_inspection_clock(state)
    assert interval == 300.0
    assert first_due == 1000.0 - 300.0 + settings.stream_first_inspection_delay_sec
    # 热身不超过一个巡检周期：间隔本身很短时不放大等待。
    short = StreamState("s2", "cam-2", "rtsp://127.0.0.1/live", 2.0, "A1工区", True, "realtime", 3, started_at=2000.0)
    short_interval, short_due = StreamManager._initial_inspection_clock(short)
    assert short_interval == 3.0
    assert short_due == 2000.0 - 3.0 + 3.0


def test_test_mode_uses_test_interval_clock():
    state = _state("test", started_at=3000.0)
    interval, first_due = StreamManager._initial_inspection_clock(state)
    assert interval == float(settings.test_stream_interval_sec)
    warmup = min(settings.stream_first_inspection_delay_sec, float(settings.test_stream_interval_sec))
    assert first_due == 3000.0 - interval + warmup
