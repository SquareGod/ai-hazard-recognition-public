from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Protocol

from .media_paths import MediaPathLeaseManager
from .stream_gateway import PlaybackUrls
from .video_sources import VideoSourceSpec


class MonitorError(RuntimeError):
    pass


class Timer(Protocol):
    daemon: bool

    def start(self) -> None: ...
    def cancel(self) -> None: ...


@dataclass
class PlaybackTicket:
    ticket_id: str
    expires_at: float
    cameras: dict[str, PlaybackUrls]
    active_uses: int = 0
    closing: bool = False


class MonitorManager:
    TICKET_TTL_SECONDS = 300
    MAX_CAMERAS = 4

    def __init__(self, *, leases: MediaPathLeaseManager, source_lookup: Callable[[str], VideoSourceSpec | None], source_list: Callable[[], list[VideoSourceSpec]] | None = None, timer_factory: Callable[[float, Callable[[], None]], Timer] = threading.Timer) -> None:
        self.leases = leases
        self.source_lookup = source_lookup
        self.source_list = source_list or (lambda: [])
        self._tickets: dict[str, PlaybackTicket] = {}
        self._lock = threading.RLock()
        self._timer_factory = timer_factory
        self._expiry_timer: Timer | None = None
        self._release_listeners: list[Callable[[str], None]] = []

    @staticmethod
    def _camera_public(ticket_id: str, camera_id: str, spec: VideoSourceSpec) -> dict[str, str]:
        prefix = f"/api/v1/media/{ticket_id}/{camera_id}"
        return {"id": spec.id, "name": spec.name, "work_area": spec.work_area, "status": "online", "webrtc": f"{prefix}/whep", "hls": f"{prefix}/hls/index.m3u8"}

    def _expire_locked(self) -> None:
        for ticket_id in [item.ticket_id for item in self._tickets.values() if item.expires_at <= time.time()]:
            self._retire_locked(ticket_id)

    def _finalize_locked(self, ticket_id: str, ticket: PlaybackTicket) -> None:
        self._tickets.pop(ticket_id, None)
        for camera_id in ticket.cameras:
            self.leases.release(f"monitor:{ticket_id}:{camera_id}")
        for callback in self._release_listeners:
            callback(ticket_id)

    def _retire_locked(self, ticket_id: str) -> PlaybackTicket | None:
        ticket = self._tickets.get(ticket_id)
        if ticket is None or ticket.closing:
            return None
        ticket.closing = True
        if ticket.active_uses == 0:
            self._finalize_locked(ticket_id, ticket)
        return ticket

    def _schedule_expiry_locked(self) -> None:
        if self._expiry_timer is not None:
            self._expiry_timer.cancel()
            self._expiry_timer = None
        expiries = [item.expires_at for item in self._tickets.values() if not item.closing]
        if expiries:
            timer = self._timer_factory(max(0.0, min(expiries) - time.time()), self._on_expiry_timer)
            timer.daemon = True
            timer.start()
            self._expiry_timer = timer

    def _on_expiry_timer(self) -> None:
        with self._lock:
            self._expire_locked()
            self._schedule_expiry_locked()

    def add_ticket_release_listener(self, callback: Callable[[str], None]) -> None:
        with self._lock:
            self._release_listeners.append(callback)

    def shutdown(self) -> None:
        with self._lock:
            if self._expiry_timer is not None:
                self._expiry_timer.cancel()
                self._expiry_timer = None
            # Application shutdown must release every media-path lease even if a
            # browser request is still being torn down. Otherwise the Hikvision
            # bridge can keep pulling an NVR channel after the backend exits.
            for ticket_id, ticket in list(self._tickets.items()):
                ticket.closing = True
                ticket.active_uses = 0
                self._finalize_locked(ticket_id, ticket)

    def acquire_ticket_use(self, ticket_id: str, camera_id: str) -> PlaybackUrls | None:
        with self._lock:
            self._expire_locked()
            ticket = self._tickets.get(ticket_id)
            if ticket is None or ticket.closing:
                return None
            urls = ticket.cameras.get(camera_id)
            if urls is not None:
                ticket.active_uses += 1
            return urls

    def release_ticket_use(self, ticket_id: str) -> None:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                return
            ticket.active_uses -= 1
            if ticket.closing and ticket.active_uses == 0:
                self._finalize_locked(ticket_id, ticket)
            self._schedule_expiry_locked()

    def _release_locked(self, ticket_id: str) -> PlaybackTicket | None:
        ticket = self._retire_locked(ticket_id)
        self._schedule_expiry_locked()
        return ticket

    def create_ticket(self, camera_ids: list[str], purpose: str) -> dict:
        unique_ids = list(dict.fromkeys(camera_ids))
        if not unique_ids or len(unique_ids) > self.MAX_CAMERAS:
            raise MonitorError("一次最多监看四路摄像头")
        specs: list[VideoSourceSpec] = []
        for camera_id in unique_ids:
            spec = self.source_lookup(camera_id)
            if spec is None or not spec.enabled:
                raise MonitorError("摄像头不存在或不可用")
            specs.append(spec)
        ticket_id, urls = uuid.uuid4().hex, {}
        try:
            for spec in specs:
                urls[spec.id] = self.leases.acquire(f"monitor:{ticket_id}:{spec.id}", spec)
        except Exception:
            for camera_id in urls:
                self.leases.release(f"monitor:{ticket_id}:{camera_id}")
            raise
        with self._lock:
            self._expire_locked()
            self._tickets[ticket_id] = PlaybackTicket(ticket_id, time.time() + self.TICKET_TTL_SECONDS, urls)
            self._schedule_expiry_locked()
        return {"ticket_id": ticket_id, "expires_in": self.TICKET_TTL_SECONDS, "purpose": purpose, "cameras": [self._camera_public(ticket_id, spec.id, spec) for spec in specs]}

    def release_ticket(self, ticket_id: str) -> dict | None:
        with self._lock:
            ticket = self._release_locked(ticket_id)
        return {"ticket_id": ticket_id, "released": True} if ticket else None

    def get_ticket_camera(self, ticket_id: str, camera_id: str) -> PlaybackUrls | None:
        with self._lock:
            self._expire_locked()
            ticket = self._tickets.get(ticket_id)
            return ticket.cameras.get(camera_id) if ticket and not ticket.closing else None

    def ticket_exists(self, ticket_id: str) -> bool:
        with self._lock:
            self._expire_locked()
            ticket = self._tickets.get(ticket_id)
            return ticket is not None and not ticket.closing

    def list_cameras(self, *, work_area: str = "", limit: int = 4, offset: int = 0) -> dict:
        limit, offset = max(1, min(limit, self.MAX_CAMERAS)), max(0, offset)
        enabled_sources = [item for item in self.source_list() if item.enabled]
        work_areas = sorted({item.work_area for item in enabled_sources if item.work_area})
        sources = [item for item in enabled_sources if not work_area or item.work_area == work_area]
        return {"items": [{"id": item.id, "name": item.name, "work_area": item.work_area, "status": "online", "enabled": item.enabled} for item in sources[offset:offset + limit]], "total": len(sources), "limit": limit, "offset": offset, "work_areas": work_areas}

    def camera_status(self, camera_ids: list[str]) -> dict:
        return {"items": [{"id": camera_id, "status": "online"} for camera_id in list(dict.fromkeys(camera_ids))[:self.MAX_CAMERAS] if self.source_lookup(camera_id) is not None]}
