from .base import (
    ResolvedVideoSource,
    VideoSourceAdapter,
    VideoSourceSpec,
    VideoSourceUnavailable,
    redact_source_url,
)
from .factory import get_video_source_adapter
from .hikvision import HikvisionVideoSourceAdapter
from .rtsp import RtspVideoSourceAdapter

__all__ = [
    "HikvisionVideoSourceAdapter",
    "ResolvedVideoSource",
    "RtspVideoSourceAdapter",
    "VideoSourceAdapter",
    "VideoSourceSpec",
    "VideoSourceUnavailable",
    "get_video_source_adapter",
    "redact_source_url",
]
