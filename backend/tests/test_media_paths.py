from __future__ import annotations

from app.media_paths import MediaPathLeaseManager
from app.stream_gateway import PlaybackUrls
from app.video_sources import ResolvedVideoSource, VideoSourceSpec


class FakeGateway:
    def __init__(self) -> None:
        self.registered: list[tuple[str, str]] = []
        self.removed: list[str] = []
        self.fail_remove_once = False

    def register_path(self, path_name: str, source_url: str) -> None:
        self.registered.append((path_name, source_url))

    def remove_path(self, path_name: str) -> None:
        if self.fail_remove_once:
            self.fail_remove_once = False
            raise RuntimeError("temporary remove failure")
        self.removed.append(path_name)

    def playback_urls(self, path_name: str) -> PlaybackUrls:
        return PlaybackUrls(
            rtsp=f"rtsp://internal/{path_name}",
            webrtc=f"http://media/{path_name}/whep",
            hls=f"http://media/{path_name}/index.m3u8",
        )


class FakeAdapter:
    def resolve(self, spec: VideoSourceSpec) -> ResolvedVideoSource:
        return ResolvedVideoSource(spec.source_url)


def test_shared_path_is_removed_only_after_monitor_and_analysis_release(monkeypatch) -> None:
    gateway = FakeGateway()
    leases = MediaPathLeaseManager(gateway)
    monkeypatch.setattr("app.media_paths.get_video_source_adapter", lambda source_type: FakeAdapter())
    spec = VideoSourceSpec("cam-1", "北侧", "test", "private-source", "A1")

    first = leases.acquire("monitor:t1:cam-1", spec)
    second = leases.acquire("analysis:s1", spec)

    assert first == second
    assert len(gateway.registered) == 1
    leases.release("monitor:t1:cam-1")
    assert gateway.removed == []
    leases.release("analysis:s1")
    assert gateway.removed == [leases.path_for_source("cam-1")]


def test_last_owner_release_retries_gateway_removal_after_a_transient_failure(monkeypatch) -> None:
    gateway = FakeGateway()
    gateway.fail_remove_once = True
    leases = MediaPathLeaseManager(gateway)
    monkeypatch.setattr("app.media_paths.get_video_source_adapter", lambda source_type: FakeAdapter())
    spec = VideoSourceSpec("cam-1", "北侧", "test", "private-source", "A1")
    leases.acquire("monitor:ticket:cam-1", spec)

    try:
        leases.release("monitor:ticket:cam-1")
    except RuntimeError:
        pass
    leases.release("monitor:ticket:cam-1")

    assert gateway.removed == [leases.path_for_source("cam-1")]


def test_hikvision_path_passes_channel_metadata_and_releases_shared_bridge_lease(monkeypatch) -> None:
    gateway = FakeGateway()
    leases = MediaPathLeaseManager(gateway)
    acquired: list[tuple] = []
    released: list[tuple] = []
    monkeypatch.setattr("app.media_paths.hikvision_bridge_client.acquire", lambda *args: acquired.append(args) or {})
    monkeypatch.setattr("app.media_paths.hikvision_bridge_client.release", lambda *args: released.append(args) or {})
    spec = VideoSourceSpec(
        "hik-1", "海康通道", "hikvision", "hikvision://hik-1", "A1", True, "", False,
        {"profile_id": "profile-1", "channel_no": 4},
    )

    leases.acquire("monitor:one", spec)
    leases.acquire("analysis:one", spec)
    path_name = leases.path_for_source(spec.id)
    assert acquired == [("hik-1", f"media-path:{path_name}", path_name, "profile-1", 4)]

    leases.release("monitor:one")
    assert released == []
    leases.release("analysis:one")
    assert released == [("hik-1", f"media-path:{path_name}")]


def test_hikvision_release_failure_is_retried_and_never_blocks(monkeypatch) -> None:
    from app.hikvision_client import HikvisionBridgeError

    gateway = FakeGateway()
    leases = MediaPathLeaseManager(gateway)
    monkeypatch.setattr("app.media_paths.hikvision_bridge_client.acquire", lambda *args: {})
    calls: list[tuple] = []

    def failing_release(*args):
        calls.append(args)
        raise HikvisionBridgeError("bridge down")

    monkeypatch.setattr("app.media_paths.hikvision_bridge_client.release", failing_release)
    spec = VideoSourceSpec(
        "hik-1", "海康通道", "hikvision", "hikvision://hik-1", "A1", True, "", False,
        {"profile_id": "p1", "channel_no": 1},
    )
    leases.acquire("monitor:one", spec)

    leases.release("monitor:one")  # 不得向上抛异常

    assert len(calls) == 2  # 失败后自动重试一次
    reacquired: list[tuple] = []
    monkeypatch.setattr("app.media_paths.hikvision_bridge_client.acquire", lambda *args: reacquired.append(args) or {})
    leases.acquire("monitor:two", spec)  # 本地账本已清空，可立刻重新取流
    assert reacquired == [("hik-1", f"media-path:{leases.path_for_source('hik-1')}", leases.path_for_source("hik-1"), "p1", 1)]


def test_hikvision_heartbeat_renews_only_currently_held_paths(monkeypatch) -> None:
    gateway = FakeGateway()
    leases = MediaPathLeaseManager(gateway)
    renewals: list[tuple] = []
    monkeypatch.setattr("app.media_paths.hikvision_bridge_client.acquire", lambda *args: renewals.append(args) or {})
    monkeypatch.setattr("app.media_paths.hikvision_bridge_client.release", lambda *args: {})
    spec = VideoSourceSpec(
        "hik-1", "海康通道", "hikvision", "hikvision://hik-1", "A1", True, "", False,
        {"profile_id": "p1", "channel_no": 2},
    )
    leases.acquire("monitor:t1", spec)
    path_name = leases.path_for_source("hik-1")

    renewals.clear()
    leases._renew_hikvision_leases()
    assert renewals == [("hik-1", f"media-path:{path_name}", path_name, "p1", 2)]

    leases.release("monitor:t1")
    renewals.clear()
    leases._renew_hikvision_leases()  # 路径已释放则不再续约
    assert renewals == []
