from __future__ import annotations

import base64
import posixpath
import re
import threading
import uuid
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx
from fastapi.responses import Response

from .monitoring import MonitorManager


class MediaProxy:
    def __init__(self, *, monitor: MonitorManager, read_username: str = "", read_password: str = "", client: httpx.Client | None = None) -> None:
        self.monitor = monitor
        self.client = client or httpx.Client(timeout=10.0, follow_redirects=False)
        self.authorization = ""
        if read_username or read_password:
            token = base64.b64encode(f"{read_username}:{read_password}".encode("utf-8")).decode("ascii")
            self.authorization = f"Basic {token}"
        self._whep_sessions: dict[tuple[str, str, str], str] = {}
        self._lock = threading.RLock()
        self.monitor.add_ticket_release_listener(self._remove_ticket_sessions)

    def _remove_ticket_sessions(self, ticket_id: str) -> None:
        with self._lock:
            keys = [item for item in self._whep_sessions if item[0] == ticket_id]
            targets = [self._whep_sessions.pop(key) for key in keys]
        if targets:
            threading.Thread(target=self._delete_upstream_sessions, args=(targets,), daemon=True).start()

    def _delete_upstream_sessions(self, targets: list[str]) -> None:
        for target in targets:
            try:
                self._forward("DELETE", target)
            except httpx.HTTPError:
                pass

    def _acquire_urls_or_error(self, ticket_id: str, camera_id: str) -> tuple[object | None, Response | None]:
        urls = self.monitor.acquire_ticket_use(ticket_id, camera_id)
        if urls is not None:
            return urls, None
        return None, Response(status_code=404 if self.monitor.ticket_exists(ticket_id) else 401)

    def _forward(self, method: str, url: str, *, content: bytes = b"", content_type: str = "", follow_redirects: bool = False) -> httpx.Response:
        headers = {"Authorization": self.authorization} if self.authorization else {}
        if content_type:
            headers["Content-Type"] = content_type
        return self.client.request(method, url, content=content, headers=headers, follow_redirects=follow_redirects)

    @staticmethod
    def _response(upstream: httpx.Response, *, content: bytes | None = None, headers: dict[str, str] | None = None) -> Response:
        response_headers = {"content-type": upstream.headers.get("content-type", "application/octet-stream")}
        response_headers.update(headers or {})
        return Response(content=upstream.content if content is None else content, status_code=upstream.status_code, headers=response_headers)

    @staticmethod
    def _safe_asset(asset: str) -> tuple[str, str] | None:
        parsed = urlsplit(asset)
        if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path.startswith("/"):
            return None
        path = posixpath.normpath(parsed.path)
        if not path or path == "." or path == ".." or path.startswith("../"):
            return None
        return path, parsed.query

    @staticmethod
    def _valid_whep_session_location(target: str, location: str) -> str | None:
        resolved = urljoin(target, location)
        target_parts, resolved_parts = urlsplit(target), urlsplit(resolved)
        if (resolved_parts.scheme, resolved_parts.netloc) != (target_parts.scheme, target_parts.netloc):
            return None
        raw_path = resolved_parts.path
        decoded_path = unquote(raw_path)
        if (
            re.search(r"%(?:2e|2f|5c)", raw_path, re.IGNORECASE)
            or unquote(decoded_path) != decoded_path
            or "\\" in decoded_path
            or "//" in decoded_path
            or any(ord(character) < 32 or ord(character) == 127 for character in decoded_path)
        ):
            return None
        canonical_path = posixpath.normpath(decoded_path)
        expected_prefix = posixpath.normpath(unquote(target_parts.path)).rstrip("/") + "/"
        if canonical_path != decoded_path or not canonical_path.startswith(expected_prefix):
            return None
        return resolved

    def _rewrite_hls_reference(self, reference: str, current_asset: str, ticket_id: str, camera_id: str, hls_base: str) -> str:
        parsed = urlsplit(reference)
        proxy_prefix = f"/api/v1/media/{ticket_id}/{camera_id}/hls"
        if parsed.fragment:
            return f"{proxy_prefix}/__blocked__"
        base = urlsplit(hls_base)
        if parsed.scheme or parsed.netloc or parsed.path.startswith("/"):
            if (parsed.scheme and parsed.scheme != base.scheme) or (parsed.netloc and parsed.netloc != base.netloc):
                return f"{proxy_prefix}/__blocked__"
            base_directory = posixpath.dirname(unquote(base.path)).rstrip("/")
            absolute_path = unquote(parsed.path)
            if not absolute_path.startswith(base_directory + "/"):
                return f"{proxy_prefix}/__blocked__"
            normalized = posixpath.normpath(absolute_path[len(base_directory) + 1:])
        else:
            combined = posixpath.join(posixpath.dirname(current_asset), parsed.path)
            normalized = posixpath.normpath(combined)
        if not normalized or normalized == "." or normalized == ".." or normalized.startswith("../"):
            return f"{proxy_prefix}/__blocked__"
        prefix = f"/api/v1/media/{ticket_id}/{camera_id}/hls/{normalized}"
        return f"{prefix}?{parsed.query}" if parsed.query else prefix

    def _rewrite_playlist(self, body: str, current_asset: str, ticket_id: str, camera_id: str, hls_base: str) -> bytes:
        attribute = re.compile(r'URI=("|\')(.*?)(\1)')
        lines: list[str] = []
        for line in body.splitlines():
            if line.startswith("#"):
                lines.append(attribute.sub(lambda match: f'URI={match.group(1)}{self._rewrite_hls_reference(match.group(2), current_asset, ticket_id, camera_id, hls_base)}{match.group(3)}', line))
            elif line:
                lines.append(self._rewrite_hls_reference(line, current_asset, ticket_id, camera_id, hls_base))
            else:
                lines.append(line)
        encoded = "\n".join(lines).encode("utf-8")
        return encoded + (b"\n" if body.endswith("\n") else b"")

    def whep(self, ticket_id: str, camera_id: str, method: str, content: bytes = b"", content_type: str = "", session_id: str = "") -> Response:
        urls, error = self._acquire_urls_or_error(ticket_id, camera_id)
        if error:
            return error
        try:
            if method == "POST" and content_type.split(";", 1)[0].lower() != "application/sdp":
                return Response(status_code=415)
            with self._lock:
                target = urls.webrtc if not session_id else self._whep_sessions.get((ticket_id, camera_id, session_id), "")
            if not target:
                return Response(status_code=404)
            upstream = self._forward(method, target, content=content, content_type=content_type)
            location = upstream.headers.get("location")
            if location:
                resolved = self._valid_whep_session_location(urls.webrtc, location)
                if resolved is None:
                    return Response(status_code=502)
                opaque_id = uuid.uuid4().hex
                with self._lock:
                    self._whep_sessions[(ticket_id, camera_id, opaque_id)] = resolved
                return self._response(upstream, headers={"location": f"/api/v1/media/{ticket_id}/{camera_id}/whep/{opaque_id}"})
            return self._response(upstream)
        finally:
            if session_id:
                with self._lock:
                    self._whep_sessions.pop((ticket_id, camera_id, session_id), None)
            self.monitor.release_ticket_use(ticket_id)

    def hls(self, ticket_id: str, camera_id: str, asset: str) -> Response:
        urls, error = self._acquire_urls_or_error(ticket_id, camera_id)
        if error:
            return error
        try:
            parsed_asset = self._safe_asset(asset)
            if parsed_asset is None:
                return Response(status_code=404)
            path, query = parsed_asset
            target_parts = urlsplit(urljoin(urls.hls, path))
            target = urlunsplit((target_parts.scheme, target_parts.netloc, target_parts.path, query, ""))
            # MediaMTX uses one same-origin cookie-check redirect before serving
            # HLS. Follow it inside the authenticated backend proxy so browsers
            # never receive an unusable redirect to the private gateway URL.
            upstream = self._forward("GET", target, follow_redirects=True)
            content = upstream.content
            content_type = upstream.headers.get("content-type", "")
            if path.endswith(".m3u8") and upstream.status_code < 300:
                content = self._rewrite_playlist(upstream.text, path, ticket_id, camera_id, urls.hls)
                content_type = content_type or "application/vnd.apple.mpegurl"
            return self._response(upstream, content=content, headers={"content-type": content_type or "application/octet-stream"})
        finally:
            self.monitor.release_ticket_use(ticket_id)
