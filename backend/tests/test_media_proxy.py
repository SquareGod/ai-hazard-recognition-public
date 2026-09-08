from __future__ import annotations

import base64
import threading

import httpx
from fastapi.testclient import TestClient

from app.media_proxy import MediaProxy
from app.stream_gateway import PlaybackUrls


class FakeMonitor:
    def __init__(self, urls: PlaybackUrls | None) -> None:
        self.urls = urls
        self.valid = urls is not None
        self.release_listeners = []

    def get_ticket_camera(self, ticket_id: str, camera_id: str) -> PlaybackUrls | None:
        return self.urls if ticket_id == "ticket" and camera_id == "cam-1" else None

    def ticket_exists(self, ticket_id: str) -> bool:
        return self.valid and ticket_id == "ticket"

    def acquire_ticket_use(self, ticket_id: str, camera_id: str) -> PlaybackUrls | None:
        return self.get_ticket_camera(ticket_id, camera_id)

    def release_ticket_use(self, ticket_id: str) -> None:
        pass

    def add_ticket_release_listener(self, callback) -> None:
        self.release_listeners.append(callback)

    def release_ticket(self, ticket_id: str) -> None:
        self.valid = False
        for callback in self.release_listeners:
            callback(ticket_id)


class AtomicMonitor(FakeMonitor):
    def __init__(self, urls: PlaybackUrls) -> None:
        super().__init__(urls)
        self.active_uses = 0
        self.closing = False
        self.lease_released = False

    def acquire_ticket_use(self, ticket_id: str, camera_id: str) -> PlaybackUrls | None:
        if self.closing or ticket_id != "ticket" or camera_id != "cam-1":
            return None
        self.active_uses += 1
        return self.urls

    def release_ticket_use(self, ticket_id: str) -> None:
        self.active_uses -= 1
        if self.closing and self.active_uses == 0:
            self.lease_released = True

    def release_ticket(self, ticket_id: str) -> None:
        self.closing = True
        if self.active_uses == 0:
            self.lease_released = True


def test_whep_proxy_injects_server_credentials_and_rewrites_location() -> None:
    seen: dict[str, str] = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["authorization"]
        return httpx.Response(201, headers={"content-type": "application/sdp", "location": "https://upstream/whep/session/private"}, content=b"answer")

    proxy = MediaProxy(
        monitor=FakeMonitor(PlaybackUrls("internal", "https://upstream/whep", "https://upstream/index.m3u8")),
        read_username="***",
        read_password="***",
        client=httpx.Client(transport=httpx.MockTransport(upstream)),
    )

    response = proxy.whep("ticket", "cam-1", "POST", b"offer", "application/sdp")

    assert response.status_code == 201
    assert response.body == b"answer"
    assert response.headers["content-type"] == "application/sdp"
    assert response.headers["location"].startswith("/api/v1/media/ticket/cam-1/whep/")
    assert "upstream" not in response.headers["location"]
    assert seen["authorization"] == "Basic " + base64.b64encode(b"***:***").decode()


def test_hls_preserves_binary_content_type_and_rejects_unavailable_camera() -> None:
    calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "video/mp2t"}, content=b"segment")

    proxy = MediaProxy(
        monitor=FakeMonitor(PlaybackUrls("internal", "https://upstream/whep", "https://upstream/index.m3u8")),
        client=httpx.Client(transport=httpx.MockTransport(upstream)),
    )
    segment = proxy.hls("ticket", "cam-1", "chunk.ts")
    invalid_ticket = proxy.hls("missing", "cam-1", "chunk.ts")
    wrong_camera = proxy.hls("ticket", "cam-2", "chunk.ts")

    assert segment.status_code == 200
    assert segment.body == b"segment"
    assert segment.headers["content-type"] == "video/mp2t"
    assert invalid_ticket.status_code == 401
    assert wrong_camera.status_code == 404
    assert calls == 1


def test_whep_rejects_cross_origin_location_without_forwarding_delete() -> None:
    methods: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(201, headers={"location": "https://unexpected/session"}, content=b"answer")

    proxy = MediaProxy(
        monitor=FakeMonitor(PlaybackUrls("internal", "https://upstream/camera/whep", "https://upstream/camera/index.m3u8")),
        client=httpx.Client(transport=httpx.MockTransport(upstream)),
    )

    created = proxy.whep("ticket", "cam-1", "POST", b"offer", "application/sdp")
    deleted = proxy.whep("ticket", "cam-1", "DELETE", session_id="untrusted")

    assert created.status_code == 502
    assert deleted.status_code == 404
    assert methods == ["POST"]


def test_whep_location_rejects_encoded_traversal_and_accepts_canonical_opaque_session() -> None:
    rejected_locations = [
        "/camera/whep/session/%2e%2e/%2e%2e/admin",
        "/camera/whep/session/%2E%2E/%2E%2E/admin",
        "/camera/whep/session/%2e%2E%2fadmin",
        "/camera/whep/session%2f..%5cadmin",
        "/camera/whep/session/%00opaque",
        "/camera/whep//session/opaque",
    ]
    for location in rejected_locations:
        methods: list[str] = []

        def upstream(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            return httpx.Response(201, headers={"location": location})

        proxy = MediaProxy(
            monitor=FakeMonitor(PlaybackUrls("internal", "https://upstream/camera/whep", "https://upstream/camera/index.m3u8")),
            client=httpx.Client(transport=httpx.MockTransport(upstream)),
        )
        created = proxy.whep("ticket", "cam-1", "POST", b"offer", "application/sdp")
        deleted = proxy.whep("ticket", "cam-1", "DELETE", session_id="untrusted")

        assert created.status_code == 502
        assert deleted.status_code == 404
        assert methods == ["POST"]

    methods = []

    def valid_upstream(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(201, headers={"location": "/camera/whep/session/opaque-123"})
        assert request.url.path == "/camera/whep/session/opaque-123"
        return httpx.Response(204)

    proxy = MediaProxy(
        monitor=FakeMonitor(PlaybackUrls("internal", "https://upstream/camera/whep", "https://upstream/camera/index.m3u8")),
        client=httpx.Client(transport=httpx.MockTransport(valid_upstream)),
    )
    created = proxy.whep("ticket", "cam-1", "POST", b"offer", "application/sdp")
    opaque_id = created.headers["location"].rsplit("/", 1)[-1]
    deleted = proxy.whep("ticket", "cam-1", "DELETE", session_id=opaque_id)

    assert created.status_code == 201
    assert deleted.status_code == 204
    assert methods == ["POST", "DELETE"]


def test_ticket_release_removes_its_whep_session_mappings() -> None:
    deleted = threading.Event()

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted.set()
            return httpx.Response(204)
        return httpx.Response(201, headers={"location": "https://upstream/whep/session"})

    monitor = FakeMonitor(PlaybackUrls("internal", "https://upstream/whep", "https://upstream/index.m3u8"))
    proxy = MediaProxy(
        monitor=monitor,
        client=httpx.Client(transport=httpx.MockTransport(upstream)),
    )
    proxy.whep("ticket", "cam-1", "POST", b"offer", "application/sdp")

    monitor.release_ticket("ticket")

    assert proxy._whep_sessions == {}
    assert deleted.wait(1)


def test_hls_rewrites_nested_playlists_and_uri_attributes_with_safe_queries() -> None:
    responses = {
        "/root/master.m3u8": b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nnested/media.m3u8?token=ok\n",
        "/root/nested/media.m3u8?token=ok": b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="../keys/key.bin?key=ok"\n#EXT-X-MAP:URI="init/map.mp4?map=ok"\npart.ts?part=ok\n',
    }

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/vnd.apple.mpegurl"}, content=responses[request.url.raw_path.decode()])

    proxy = MediaProxy(
        monitor=FakeMonitor(PlaybackUrls("internal", "https://upstream/camera/whep", "https://upstream/root/index.m3u8")),
        client=httpx.Client(transport=httpx.MockTransport(upstream)),
    )

    master = proxy.hls("ticket", "cam-1", "master.m3u8")
    media = proxy.hls("ticket", "cam-1", "nested/media.m3u8?token=ok")

    assert b"/api/v1/media/ticket/cam-1/hls/nested/media.m3u8?token=ok" in master.body
    assert b'URI="/api/v1/media/ticket/cam-1/hls/keys/key.bin?key=ok"' in media.body
    assert b'URI="/api/v1/media/ticket/cam-1/hls/nested/init/map.mp4?map=ok"' in media.body
    assert b"/api/v1/media/ticket/cam-1/hls/nested/part.ts?part=ok" in media.body


def test_hls_rewrites_same_origin_absolute_urls_and_blocks_external_urls() -> None:
    playlist = (
        "#EXTM3U\n"
        "https://upstream/root/segment.ts?token=secret\n"
        "#EXT-X-KEY:METHOD=AES-128,URI=\"https://evil.example/key.bin?credential=leak\"\n"
    )
    proxy = MediaProxy(
        monitor=FakeMonitor(PlaybackUrls("internal", "https://upstream/camera/whep", "https://upstream/root/index.m3u8")),
        client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={"content-type": "application/vnd.apple.mpegurl"}, text=playlist))),
    )

    response = proxy.hls("ticket", "cam-1", "index.m3u8")

    assert b"/api/v1/media/ticket/cam-1/hls/segment.ts?token=secret" in response.body
    assert b"evil.example" not in response.body
    assert b"credential=leak" not in response.body


def test_proxy_holds_ticket_use_until_blocking_upstream_request_completes() -> None:
    entered, unblock = threading.Event(), threading.Event()

    def upstream(request: httpx.Request) -> httpx.Response:
        entered.set()
        assert unblock.wait(1)
        return httpx.Response(200, content=b"segment")

    monitor = AtomicMonitor(PlaybackUrls("internal", "https://upstream/camera/whep", "https://upstream/camera/index.m3u8"))
    proxy = MediaProxy(monitor=monitor, client=httpx.Client(transport=httpx.MockTransport(upstream)))
    thread = threading.Thread(target=lambda: proxy.hls("ticket", "cam-1", "segment.ts"))
    thread.start()
    assert entered.wait(1)

    monitor.release_ticket("ticket")
    assert monitor.active_uses == 1
    assert not monitor.lease_released
    unblock.set()
    thread.join(1)

    assert monitor.lease_released


def test_media_routes_use_proxy_response_without_disclosing_upstream() -> None:
    from app.main import app

    proxy = MediaProxy(
        monitor=FakeMonitor(PlaybackUrls("internal", "https://upstream/whep", "https://upstream/index.m3u8")),
        client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, headers={"content-type": "application/vnd.apple.mpegurl"}, content=b"#EXTM3U"))),
    )
    from unittest.mock import patch

    with patch("app.main.media_proxy", proxy), TestClient(app, client=("127.0.0.1", 50000)) as client:
        response = client.get("/api/v1/media/ticket/cam-1/hls/index.m3u8")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert "upstream" not in response.text
