from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .config import settings
from .media_paths import MediaPathLeaseManager
from .stream_gateway import MediaMTXGateway, PlaybackUrls
from .streams import StreamManager, stream_manager
from .video_sources import VideoSourceSpec, VideoSourceUnavailable, get_video_source_adapter


class StreamSessionStartError(RuntimeError):
    pass


@dataclass
class StreamSession:
    session_id: str
    source_id: str
    path_name: str
    inference_stream_id: str
    playback_urls: PlaybackUrls
    status: str = "running"
    started_at: float = 0.0
    last_error: str | None = None
    inference_stopped: bool = False
    path_removed: bool = False
    analysis_mode: str = "realtime"

    def public(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "source_id": self.source_id,
            "inference_stream_id": self.inference_stream_id,
            "status": self.status,
            "started_at": self.started_at,
            "last_error": self.last_error,
            "playback_urls": self.playback_urls.public_dict(),
            "analysis_mode": self.analysis_mode,
        }


class StreamSessionManager:
    def __init__(self, *, gateway: MediaMTXGateway, inference: StreamManager, leases: MediaPathLeaseManager | None = None) -> None:
        self.gateway = gateway
        self.leases = leases or MediaPathLeaseManager(gateway)
        self.inference = inference
        self._sources: dict[str, VideoSourceSpec] = {}
        self._sessions: dict[str, StreamSession] = {}
        self._active_by_source: dict[str, str] = {}
        self._lock = threading.RLock()

    def _public(self, session: StreamSession) -> dict[str, Any]:
        result = session.public()
        states = self.inference.list() if hasattr(self.inference, "list") else []
        state = next((item for item in states if item.get("stream_id") == session.inference_stream_id), None)
        if state:
            result.update({key: value for key, value in state.items() if key not in {"stream_id", "source_url", "work_area", "auto_email"}})
        return result

    def add_source(self, spec: VideoSourceSpec) -> dict[str, Any]:
        get_video_source_adapter(spec.source_type).resolve(spec)
        with self._lock:
            self._sources[spec.id] = spec
        return spec.public_dict()

    def list_sources(self) -> list[dict[str, Any]]:
        with self._lock:
            return [item.public_dict() for item in self._sources.values()]

    def get_source(self, source_id: str) -> VideoSourceSpec | None:
        with self._lock:
            return self._sources.get(source_id)

    def source_specs(self) -> list[VideoSourceSpec]:
        with self._lock:
            return list(self._sources.values())

    def test_source(self, source_id: str) -> dict[str, Any] | None:
        spec = self._sources.get(source_id)
        if spec is None:
            return None
        resolved = get_video_source_adapter(spec.source_type).resolve(spec)
        return {"ok": True, "source": spec.public_dict(), "requires_transcode": resolved.requires_transcode}

    def start(self, source_id: str, *, inference_fps: float, auto_email: bool, analysis_mode: str = "realtime", inspection_interval_sec: int = 300) -> dict[str, Any]:
        with self._lock:
            active_id = self._active_by_source.get(source_id)
            if active_id:
                active = self._sessions.get(active_id)
                if active and active.status in {"starting", "running"}:
                    return self._public(active)
                if active and not (active.inference_stopped and active.path_removed):
                    # 上次会话停止中途失败留下的残留：自动清理后继续启动，
                    # 不再抛错——否则用户既无法启动也看不到可停止的任务，永久卡死。
                    if not active.inference_stopped:
                        try:
                            self.inference.stop(active.inference_stream_id)
                            active.inference_stopped = True
                        except Exception:
                            pass
                    if not active.path_removed:
                        try:
                            self.leases.release(f"analysis:{active.session_id}")
                            active.path_removed = True
                        except Exception:
                            pass
                    active.status = "stopped"
                    active.last_error = "上次残留会话已自动清理"
                    self._active_by_source.pop(source_id, None)
            spec = self._sources.get(source_id)
            if spec is None:
                raise StreamSessionStartError("视频源不存在，请先创建设备视频源")
            if not spec.enabled:
                raise StreamSessionStartError("视频源已停用，无法启动分析")
            try:
                get_video_source_adapter(spec.source_type).resolve(spec)
            except VideoSourceUnavailable as exc:
                raise StreamSessionStartError(str(exc)) from exc
            session_id = uuid.uuid4().hex
            owner_id = f"analysis:{session_id}"
            try:
                playback = self.leases.acquire(owner_id, spec)
            except ValueError as exc:
                raise StreamSessionStartError(str(exc)) from exc
            path_name = self.leases.path_for_source(spec.id)
            inference_stream_id = ""
            try:
                effective_fps = 1 / 3 if analysis_mode == "test" else inference_fps
                inference_kwargs: dict[str, Any] = {"work_area": spec.work_area, "auto_email": auto_email}
                if analysis_mode != "realtime":
                    inference_kwargs.update({"analysis_mode": analysis_mode, "inspection_interval_sec": inspection_interval_sec})
                inference_state = self.inference.start(spec.id, playback.rtsp, effective_fps, **inference_kwargs)
                inference_stream_id = str(inference_state["stream_id"])
                ready = self.inference.wait_ready(inference_stream_id, timeout=settings.stream_start_ready_timeout_sec)
                if ready.get("status") != "running":
                    raise StreamSessionStartError(str(ready.get("last_error") or "视频流启动失败"))
            except Exception as exc:
                if inference_stream_id:
                    try:
                        self.inference.stop(inference_stream_id)
                    except Exception:
                        pass
                self.leases.release(owner_id)
                if isinstance(exc, StreamSessionStartError):
                    raise
                raise StreamSessionStartError("算法任务启动失败，流媒体路径已回滚") from exc
            session = StreamSession(
                session_id=session_id,
                source_id=source_id,
                path_name=path_name,
                inference_stream_id=inference_stream_id,
                playback_urls=playback,
                status="running",
                started_at=time.time(),
                analysis_mode=analysis_mode,
            )
            self._sessions[session.session_id] = session
            self._active_by_source[source_id] = session.session_id
            return self._public(session)

    def stop(self, session_id: str) -> dict[str, Any] | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        with self._lock:
            if session.status == "stopped":
                return self._public(session)
            session.status = "stopping"
            errors: list[str] = []
            if not session.inference_stopped:
                try:
                    self.inference.stop(session.inference_stream_id)
                    session.inference_stopped = True
                except Exception:
                    errors.append("算法任务停止失败")
            if not session.path_removed:
                try:
                    self.leases.release(f"analysis:{session.session_id}")
                    session.path_removed = True
                except Exception:
                    errors.append("流媒体路径清理失败")
            if errors:
                session.status = "failed"
                session.last_error = "；".join(errors)
            else:
                session.status = "stopped"
                session.last_error = None
            # 无论停止是否完全成功都清除映射：残留会话由 start() 自动清理，
            # 绝不能让 _active_by_source 指向已死会话导致后续启动被永久拒绝。
            if self._active_by_source.get(session.source_id) == session.session_id:
                self._active_by_source.pop(session.source_id, None)
        return self._public(session)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._public(session) for session in self._sessions.values()]

    def stop_by_inference(self, inference_stream_id: str) -> dict[str, Any] | None:
        with self._lock:
            session_id = next(
                (item.session_id for item in self._sessions.values() if item.inference_stream_id == inference_stream_id),
                None,
            )
        return self.stop(session_id) if session_id else None


def build_default_stream_session_manager() -> StreamSessionManager:
    gateway = MediaMTXGateway(
        settings.mediamtx_control_url,
        username=settings.mediamtx_api_user,
        password=settings.mediamtx_api_password,
        rtsp_public_base=settings.mediamtx_rtsp_base,
        rtsp_username=settings.mediamtx_read_user,
        rtsp_password=settings.mediamtx_read_password,
        webrtc_public_base=settings.mediamtx_webrtc_base,
        hls_public_base=settings.mediamtx_hls_base,
    )
    return StreamSessionManager(gateway=gateway, inference=stream_manager)


stream_session_manager = build_default_stream_session_manager()
