from __future__ import annotations

from typing import Any
import httpx
from .config import settings

class HikvisionBridgeError(RuntimeError):
    pass

class HikvisionBridgeClient:
    def __init__(self, base_url: str = settings.hikvision_bridge_url, token: str = settings.hikvision_bridge_token) -> None:
        self.base_url, self.token = base_url.rstrip("/"), token
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self.token:
            raise HikvisionBridgeError("海康桥接服务尚未配置令牌，请在后端 .env 设置 HIKVISION_BRIDGE_TOKEN")
        try:
            response = httpx.request(method, f"{self.base_url}{path}", headers={"X-Bridge-Token": self.token}, timeout=12, **kwargs)
        except httpx.HTTPError as exc:
            raise HikvisionBridgeError("无法连接Windows海康桥接服务，请确认桥接服务已启动") from exc
        if response.is_error:
            try: detail = response.json().get("detail", "桥接服务操作失败")
            except ValueError: detail = "桥接服务操作失败"
            raise HikvisionBridgeError(str(detail))
        return response.json()
    def configure(self, payload: dict[str, Any]) -> dict[str, Any]: return self._request("POST", "/nvr-profiles", json=payload)
    def profiles(self) -> list[dict[str, Any]]: return self._request("GET", "/nvr-profiles")
    def test(self, profile_id: str) -> dict[str, Any]: return self._request("POST", f"/nvr-profiles/{profile_id}/test")
    def sync(self, profile_id: str) -> dict[str, Any]: return self._request("POST", f"/nvr-profiles/{profile_id}/sync")
    def acquire(self, channel_id: str, owner_id: str, path_name: str, profile_id: str, channel_no: int) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/channels/{channel_id}/leases/{owner_id}",
            json={"path_name": path_name, "profile_id": profile_id, "channel_no": channel_no},
        )
    def release(self, channel_id: str, owner_id: str) -> dict[str, Any]: return self._request("DELETE", f"/channels/{channel_id}/leases/{owner_id}")
    def snapshot(self, channel_id: str, profile_id: str, channel_no: int) -> bytes:
        if not self.token:
            raise HikvisionBridgeError("海康桥接服务尚未配置令牌，请在后端 .env 设置 HIKVISION_BRIDGE_TOKEN")
        try:
            response = httpx.post(
                f"{self.base_url}/channels/{channel_id}/snapshot",
                headers={"X-Bridge-Token": self.token},
                json={"profile_id": profile_id, "channel_no": channel_no}, timeout=12,
            )
        except httpx.HTTPError as exc:
            raise HikvisionBridgeError("无法连接Windows海康桥接服务，请确认桥接服务已启动") from exc
        if response.is_error:
            try: detail = response.json().get("detail", "桥接服务抓图失败")
            except ValueError: detail = "桥接服务抓图失败"
            raise HikvisionBridgeError(str(detail))
        return response.content

hikvision_bridge_client = HikvisionBridgeClient()
