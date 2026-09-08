from __future__ import annotations

from .base import VideoSourceAdapter, VideoSourceUnavailable
from .hikvision import HikvisionVideoSourceAdapter
from .rtsp import RtspVideoSourceAdapter


def get_video_source_adapter(source_type: str) -> VideoSourceAdapter:
    normalized = source_type.strip().lower()
    if normalized == "rtsp":
        return RtspVideoSourceAdapter()
    if normalized in {"hikvision", "hcnetsdk"}:
        return HikvisionVideoSourceAdapter()
    raise VideoSourceUnavailable("不支持的视频源类型，请使用rtsp或hikvision")
