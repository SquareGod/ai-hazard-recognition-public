from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.monitoring import MonitorManager
from app.stream_gateway import PlaybackUrls
from app.video_sources import VideoSourceSpec


class FakeLeases:
    def __init__(self) -> None:
        self.released: list[str] = []

    def acquire(self, owner_id: str, spec: VideoSourceSpec) -> PlaybackUrls:
        return PlaybackUrls("internal", "https://media.example/whep", "https://media.example/index.m3u8")

    def release(self, owner_id: str) -> None:
        self.released.append(owner_id)


class ManualTimer:
    def __init__(self, delay: float, callback) -> None:
        self.delay = delay
        self.callback = callback
        self.daemon = False
        self.cancelled = False

    def start(self) -> None:
        pass

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.callback()


def source(source_id: str, *, work_area: str = "A1", enabled: bool = True) -> VideoSourceSpec:
    return VideoSourceSpec(source_id, f"摄像头 {source_id}", "rtsp", "private-source", work_area, enabled)


def test_ticket_expiry_and_release_only_affect_monitor_lease(monkeypatch) -> None:
    now = 1_000.0
    monkeypatch.setattr("app.monitoring.time.time", lambda: now)
    leases = FakeLeases()
    manager = MonitorManager(leases=leases, source_lookup=lambda source_id: source(source_id) if source_id == "cam-1" else None)

    ticket = manager.create_ticket(["cam-1"], "monitor-wall")
    assert "private-source" not in str(ticket)
    assert "https://media.example" not in str(ticket)
    assert manager.release_ticket(ticket["ticket_id"]) is not None
    assert leases.released == [f"monitor:{ticket['ticket_id']}:cam-1"]
    expired = manager.create_ticket(["cam-1"], "monitor-wall")
    now += 301
    assert manager.get_ticket_camera(expired["ticket_id"], "cam-1") is None
    assert f"monitor:{expired['ticket_id']}:cam-1" in leases.released


def test_monitor_routes_cap_directory_and_protect_ticket_details() -> None:
    from app.main import app

    cameras = [source(f"cam-{number}") for number in range(1, 19)]
    manager = MonitorManager(
        leases=FakeLeases(),
        source_lookup=lambda source_id: next((item for item in cameras if item.id == source_id), None),
        source_list=lambda: cameras,
    )
    with patch("app.main.monitor_manager", manager), TestClient(app, client=("127.0.0.1", 50000)) as client:
        directory = client.get("/api/v1/monitor/cameras?limit=99&offset=0")
        missing = client.post("/api/v1/monitor/playback-tickets", json={"camera_ids": ["missing"], "purpose": "monitor-wall"})
        ticket = client.post("/api/v1/monitor/playback-tickets", json={"camera_ids": ["cam-1"], "purpose": "monitor-wall"})
        status = client.get("/api/v1/monitor/cameras/status?ids=cam-1,cam-2,cam-3,cam-4,cam-5")

    assert directory.status_code == 200
    assert len(directory.json()["items"]) == 16
    assert missing.status_code == 404
    assert ticket.status_code == 200
    assert "private-source" not in ticket.text
    assert "https://media.example" not in ticket.text
    assert ticket.json()["cameras"][0]["webrtc"].startswith("/api/v1/media/")
    assert status.status_code == 200
    assert len(status.json()["items"]) == 5


def test_camera_directory_excludes_disabled_sources_and_lists_enabled_work_areas() -> None:
    cameras = [
        source("cam-a1", work_area="A1工区"),
        source("cam-a2", work_area="A2工区"),
        source("cam-disabled", work_area="停用工区", enabled=False),
    ]
    manager = MonitorManager(
        leases=FakeLeases(),
        source_lookup=lambda source_id: next((item for item in cameras if item.id == source_id), None),
        source_list=lambda: cameras,
    )

    directory = manager.list_cameras(limit=4, offset=0)

    assert [item["id"] for item in directory["items"]] == ["cam-a1", "cam-a2"]
    assert directory["total"] == 2
    assert directory["work_areas"] == sorted({"A1工区", "A2工区"})


def test_ticket_timer_releases_expired_monitor_lease_without_a_followup_request(monkeypatch) -> None:
    now = 1_000.0
    timers: list[ManualTimer] = []
    monkeypatch.setattr("app.monitoring.time.time", lambda: now)
    leases = FakeLeases()
    manager = MonitorManager(
        leases=leases,
        source_lookup=lambda source_id: source(source_id) if source_id == "cam-1" else None,
        timer_factory=lambda delay, callback: timers.append(ManualTimer(delay, callback)) or timers[-1],
    )
    ticket = manager.create_ticket(["cam-1"], "monitor-wall")

    now += 301
    timers[-1].fire()

    assert f"monitor:{ticket['ticket_id']}:cam-1" in leases.released
    manager.shutdown()
    assert timers[-1].cancelled
