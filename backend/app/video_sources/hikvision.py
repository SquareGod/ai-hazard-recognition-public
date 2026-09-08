from __future__ import annotations

from .base import ResolvedVideoSource, VideoSourceAdapter, VideoSourceSpec, VideoSourceUnavailable


class HikvisionVideoSourceAdapter(VideoSourceAdapter):
    def resolve(self, spec: VideoSourceSpec) -> ResolvedVideoSource:
        if not spec.source_url.startswith("hikvision://") or "@" in spec.source_url:
            raise VideoSourceUnavailable("HCNetSDK海康通道标识无效，请先在“设备接入”中同步NVR通道；临时源可使用RTSP")
        # The Windows bridge actively publishes this path.  MediaMTX must not try
        # to pull a private SDK URL itself.
        return ResolvedVideoSource(spec.source_url, requires_transcode=True, metadata={"bridge": True})
