"""Contracts for delegating visual hazard analysis to another algorithm service.

The local YOLO/VLM path remains the default.  This module deliberately uses the
standard library so deploying the HTTP adapter does not add another runtime
dependency.
"""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.error import URLError
from urllib.request import Request, urlopen

from .schemas import Detection, FrameInfo, HazardLabel, VLMResponse


@dataclass(frozen=True)
class AlgorithmContext:
    project_id: str = ""
    work_area: str = ""
    camera_id: str = ""
    task_id: str = ""
    frame_timestamp: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def payload(self, frame: FrameInfo) -> dict[str, Any]:
        timestamp = self.frame_timestamp or datetime.now(timezone.utc).timestamp()
        return {
            "project_id": self.project_id,
            "work_area": self.work_area,
            "camera_id": self.camera_id,
            "task_id": self.task_id,
            "frame_id": frame.frame_id,
            "frame_timestamp": timestamp,
            "context": self.extra,
        }


@dataclass(frozen=True)
class AlgorithmProviderConfig:
    """Independent algorithm-provider selection; local detector/VLM settings stay untouched."""
    provider: str = "local"
    scope: str = "vlm"  # ``whole`` delegates detector + decision, including realtime.
    url: str = ""
    timeout_sec: float = 180

    @classmethod
    def from_env(cls) -> "AlgorithmProviderConfig":
        return cls(os.getenv("ALGORITHM_ADAPTER_PROVIDER", "local").strip().lower(), os.getenv("ALGORITHM_ADAPTER_SCOPE", "vlm").strip().lower(), os.getenv("ALGORITHM_ADAPTER_URL", "").rstrip("/"), float(os.getenv("ALGORITHM_ADAPTER_TIMEOUT_SEC", "180")))


class AlgorithmAdapter(Protocol):
    provider_name: str

    def health(self) -> dict[str, Any]: ...
    def capabilities(self) -> dict[str, Any]: ...
    def analyze(self, frames: list[FrameInfo], labels: list[HazardLabel], detections: list[Detection], context: AlgorithmContext | None = None) -> VLMResponse: ...


class MockAlgorithmAdapter:
    provider_name = "mock_algorithm_adapter"

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "provider": self.provider_name}

    def capabilities(self) -> dict[str, Any]:
        return {"structured_output": True, "cancellation": False, "labels": "all", "scope": "whole"}

    def analyze(self, frames: list[FrameInfo], labels: list[HazardLabel], detections: list[Detection], context: AlgorithmContext | None = None) -> VLMResponse:
        return VLMResponse(scene_summary="mock external algorithm; no remote request", findings=[])


class HttpAlgorithmAdapter:
    """JSON HTTP adapter contract: POST /analyze, GET /health and /capabilities."""
    provider_name = "external_http"

    def __init__(self, base_url: str | None = None, *, timeout_sec: float | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("ALGORITHM_ADAPTER_URL", "")).rstrip("/")
        if not self.base_url:
            raise RuntimeError("ALGORITHM_ADAPTER_URL is required for external_http")
        self.timeout_sec = timeout_sec or float(os.getenv("ALGORITHM_ADAPTER_TIMEOUT_SEC", "180"))
        self.token = token if token is not None else os.getenv("ALGORITHM_ADAPTER_TOKEN", "")

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            with urlopen(Request(self.base_url + path, data=data, headers=headers), timeout=self.timeout_sec) as response:
                return json.loads(response.read().decode("utf-8"))
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"external algorithm request failed: {exc}") from exc

    def health(self) -> dict[str, Any]:
        return self._request("/health")

    def capabilities(self) -> dict[str, Any]:
        return self._request("/capabilities")

    def analyze(self, frames: list[FrameInfo], labels: list[HazardLabel], detections: list[Detection], context: AlgorithmContext | None = None) -> VLMResponse:
        if context is None:
            from .projects import current_project
            context = AlgorithmContext(project_id=current_project.get())
        payload = {
            "frames": [{**context.payload(frame), "image_base64": base64.b64encode(Path(frame.path).read_bytes()).decode("ascii"), "width": frame.width, "height": frame.height} for frame in frames],
            "labels": [item.model_dump(mode="json") for item in labels],
            "detections": [item.model_dump(mode="json") for item in detections],
            # Rule output is advisory metadata.  The external VLM remains the
            # authority for its positive or negative visual verification.
            "metadata": dict(context.extra),
            "cancellation": {"supported": False, "task_id": context.task_id},
        }
        return VLMResponse.model_validate(self._request("/analyze", payload))


def create_algorithm_adapter() -> AlgorithmAdapter | None:
    provider = AlgorithmProviderConfig.from_env().provider
    if provider in {"", "local", "none"}:
        return None
    if provider == "mock":
        return MockAlgorithmAdapter()
    if provider in {"http", "external_http"}:
        return HttpAlgorithmAdapter()
    raise ValueError(f"unsupported ALGORITHM_ADAPTER_PROVIDER: {provider}")
