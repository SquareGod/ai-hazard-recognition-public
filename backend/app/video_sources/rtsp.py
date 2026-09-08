from __future__ import annotations

from urllib.parse import urlsplit

from .base import ResolvedVideoSource, VideoSourceAdapter, VideoSourceSpec, VideoSourceUnavailable


class RtspVideoSourceAdapter(VideoSourceAdapter):
    def resolve(self, spec: VideoSourceSpec) -> ResolvedVideoSource:
        parsed = urlsplit(spec.source_url)
        if parsed.scheme.lower() not in {"rtsp", "rtsps"} or not parsed.hostname:
            raise VideoSourceUnavailable("RTSP视频源地址无效，请检查协议、主机和通道路径")
        return ResolvedVideoSource(
            source_url=spec.source_url,
            requires_transcode=False,
            metadata={"source_type": "rtsp"},
        )
