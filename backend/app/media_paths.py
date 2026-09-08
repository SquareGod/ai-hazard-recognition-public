from __future__ import annotations

import hashlib
import threading
import time

from .stream_gateway import MediaMTXGateway, PlaybackUrls, normalize_path_name
from .video_sources import VideoSourceSpec, get_video_source_adapter
from .hikvision_client import HikvisionBridgeError, hikvision_bridge_client
from .logging_config import logger


class MediaPathLeaseManager:
    """Shares one MediaMTX path among monitor and analysis owners."""

    def __init__(self, gateway: MediaMTXGateway) -> None:
        self.gateway = gateway
        self._owner_paths: dict[str, str] = {}
        self._path_owners: dict[str, set[str]] = {}
        self._path_specs: dict[str, VideoSourceSpec] = {}
        self._lock = threading.RLock()
        self._heartbeat_thread: threading.Thread | None = None

    @staticmethod
    def path_for_source(source_id: str) -> str:
        stable_suffix = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:12]
        readable_prefix = normalize_path_name(f"cam-{source_id}")[:60].rstrip("-")
        return normalize_path_name(f"{readable_prefix}-{stable_suffix}")

    def _ensure_heartbeat(self) -> None:
        """海康租约从首个acquire起定期向桥接续心跳，配合桥接侧TTL回收防泄漏。"""
        if self._heartbeat_thread is not None:
            return
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, name="hik-lease-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        from .config import settings

        while True:
            time.sleep(settings.hikvision_heartbeat_interval_sec)
            try:
                self._renew_hikvision_leases()
            except Exception:
                logger.exception("海康租约心跳续约线程异常")

    def _renew_hikvision_leases(self) -> None:
        with self._lock:
            held = [(path_name, spec) for path_name, spec in self._path_specs.items()
                    if spec.source_type in {"hikvision", "hcnetsdk"}]
        for path_name, spec in held:
            profile_id = str(spec.metadata.get("profile_id") or "")
            channel_no = int(spec.metadata.get("channel_no") or 0)
            if not profile_id or channel_no <= 0:
                continue
            try:
                hikvision_bridge_client.acquire(spec.id, f"media-path:{path_name}", path_name, profile_id, channel_no)
            except HikvisionBridgeError as exc:
                logger.warning("海康租约心跳续约失败 path=%s channel=%s：%s", path_name, spec.id, exc)

    def acquire(self, owner_id: str, spec: VideoSourceSpec) -> PlaybackUrls:
        path_name = self.path_for_source(spec.id)
        with self._lock:
            current_path = self._owner_paths.get(owner_id)
            if current_path is not None and current_path != path_name:
                raise ValueError("媒体路径所有者已绑定其他视频源")
            owners = self._path_owners.setdefault(path_name, set())
            if not owners:
                if spec.source_type in {"hikvision", "hcnetsdk"}:
                    try:
                        profile_id = str(spec.metadata.get("profile_id") or "")
                        channel_no = int(spec.metadata.get("channel_no") or 0)
                        if not profile_id or channel_no <= 0:
                            raise HikvisionBridgeError("海康视频源缺少NVR配置或通道号，请重新同步通道")
                        hikvision_bridge_client.acquire(
                            spec.id, f"media-path:{path_name}", path_name, profile_id, channel_no
                        )
                    except HikvisionBridgeError as exc:
                        self._path_owners.pop(path_name, None)
                        raise ValueError(str(exc)) from exc
                    self._path_specs[path_name] = spec
                    self._ensure_heartbeat()
                else:
                    resolved = get_video_source_adapter(spec.source_type).resolve(spec)
                    self.gateway.register_path(path_name, resolved.source_url)
                    self._path_specs[path_name] = spec
            owners.add(owner_id)
            self._owner_paths[owner_id] = path_name
            return self.gateway.playback_urls(path_name)

    def release(self, owner_id: str) -> None:
        with self._lock:
            path_name = self._owner_paths.get(owner_id)
            if path_name is None:
                return
            owners = self._path_owners[path_name]
            owners.discard(owner_id)
            if owners:
                self._owner_paths.pop(owner_id, None)
                return
            spec = self._path_specs.pop(path_name, None)
            if spec and spec.source_type in {"hikvision", "hcnetsdk"}:
                last_error: HikvisionBridgeError | None = None
                for attempt in range(2):
                    try:
                        hikvision_bridge_client.release(spec.id, f"media-path:{path_name}")
                        last_error = None
                        break
                    except HikvisionBridgeError as exc:
                        last_error = exc
                        if attempt == 0:
                            time.sleep(0.5)
                if last_error is not None:
                    # 桥接侧有租约心跳回收兜底；本地账本照常删除，绝不阻塞后续重试。
                    logger.warning("释放海康桥接租约失败(已重试) path=%s channel=%s：%s", path_name, spec.id, last_error)
            else:
                self.gateway.remove_path(path_name)
            self._owner_paths.pop(owner_id, None)
            del self._path_owners[path_name]
