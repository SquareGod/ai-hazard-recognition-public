from __future__ import annotations

import base64
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .video_sources import redact_source_url


class MediaMTXGatewayError(RuntimeError):
    """Stable, credential-safe error returned by the stream gateway."""


class GatewayTransport(Protocol):
    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]: ...


class UrllibGatewayTransport:
    def __init__(self, base_url: str, username: str = "", password: str = "", timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.authorization = ""
        if username or password:
            token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            self.authorization = f"Basic {token}"

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.authorization:
            headers["Authorization"] = self.authorization
        request = Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                return response.status, json.loads(raw) if raw else {}
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {}
            return exc.code, parsed
        except (URLError, TimeoutError, OSError) as exc:
            raise MediaMTXGatewayError("无法连接流媒体网关，请检查MediaMTX服务和管理接口") from exc


@dataclass(frozen=True)
class PlaybackUrls:
    rtsp: str
    webrtc: str
    hls: str

    def public_dict(self) -> dict[str, str]:
        # The RTSP address is an internal algorithm endpoint and must never be
        # returned to browsers.
        return {"webrtc": self.webrtc, "hls": self.hls}


def normalize_path_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii").lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    if not normalized:
        raise MediaMTXGatewayError("视频流路径名称无效，请使用字母或数字")
    return normalized[:80]


class MediaMTXGateway:
    def __init__(
        self,
        control_base_url: str,
        *,
        username: str = "",
        password: str = "",
        rtsp_public_base: str = "rtsp://127.0.0.1:8554",
        rtsp_username: str = "",
        rtsp_password: str = "",
        webrtc_public_base: str = "http://127.0.0.1:8889",
        hls_public_base: str = "http://127.0.0.1:8888",
        transport: GatewayTransport | None = None,
    ) -> None:
        self.control_base_url = control_base_url.rstrip("/")
        self.rtsp_public_base = self._with_rtsp_credentials(
            rtsp_public_base.rstrip("/"), rtsp_username, rtsp_password
        )
        self.webrtc_public_base = webrtc_public_base.rstrip("/")
        self.hls_public_base = hls_public_base.rstrip("/")
        self.transport = transport or UrllibGatewayTransport(
            self.control_base_url, username=username, password=password
        )

    @staticmethod
    def _with_rtsp_credentials(base_url: str, username: str, password: str) -> str:
        if not username and not password:
            return base_url
        parsed = urlsplit(base_url)
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        userinfo = f"{quote(username, safe='')}:{quote(password, safe='')}@"
        return urlunsplit((parsed.scheme, f"{userinfo}{host}", parsed.path, "", ""))

    def register_path(self, path_name: str, source_url: str) -> None:
        name = normalize_path_name(path_name)
        payload = {"source": source_url, "sourceOnDemand": True}
        status, _ = self.transport.request("POST", f"/v3/config/paths/add/{quote(name)}", payload)
        if status == 409:
            status, _ = self.transport.request("PATCH", f"/v3/config/paths/patch/{quote(name)}", payload)
        if status < 200 or status >= 300:
            raise MediaMTXGatewayError(f"流媒体网关操作失败（HTTP {status}），请检查网关配置")

    def remove_path(self, path_name: str) -> None:
        name = normalize_path_name(path_name)
        status, _ = self.transport.request("DELETE", f"/v3/config/paths/delete/{quote(name)}")
        if status == 404:
            return
        if status < 200 or status >= 300:
            raise MediaMTXGatewayError(f"流媒体网关操作失败（HTTP {status}），请检查网关配置")

    def list_paths(self) -> list[dict[str, Any]]:
        status, payload = self.transport.request("GET", "/v3/paths/list")
        if status < 200 or status >= 300:
            raise MediaMTXGatewayError(f"流媒体网关操作失败（HTTP {status}），请检查网关配置")
        items = payload.get("items", [])
        return items if isinstance(items, list) else []

    def playback_urls(self, path_name: str) -> PlaybackUrls:
        name = normalize_path_name(path_name)
        return PlaybackUrls(
            rtsp=f"{self.rtsp_public_base}/{name}",
            webrtc=f"{self.webrtc_public_base}/{name}/whep",
            hls=f"{self.hls_public_base}/{name}/index.m3u8",
        )

    @staticmethod
    def safe_description(payload: dict[str, Any]) -> str:
        safe: dict[str, Any] = {}
        for key, value in payload.items():
            safe[key] = redact_source_url(value) if isinstance(value, str) and "://" in value else value
        return json.dumps(safe, ensure_ascii=False, sort_keys=True)
