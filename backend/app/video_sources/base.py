from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class VideoSourceUnavailable(RuntimeError):
    """Raised when a configured video source cannot be resolved safely."""


def redact_source_url(source_url: str) -> str:
    """Return a public URL without user info, query credentials, or fragments."""
    try:
        parsed = urlsplit(source_url)
        if not parsed.scheme or not parsed.netloc:
            return "***"
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        if parsed.username is not None or parsed.password is not None:
            host = f"***@{host}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except (TypeError, ValueError):
        return "***"


@dataclass(frozen=True)
class VideoSourceSpec:
    id: str
    name: str
    source_type: str
    source_url: str
    work_area: str
    enabled: bool = True
    risk_point: str = ""
    ai_enabled: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "source_type": self.source_type,
            "source_url": redact_source_url(self.source_url),
            "work_area": self.work_area,
            "enabled": self.enabled,
            **({"risk_point": self.risk_point} if self.risk_point else {}),
            **({"ai_enabled": True} if self.ai_enabled else {}),
        }


@dataclass(frozen=True)
class ResolvedVideoSource:
    source_url: str
    requires_transcode: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class VideoSourceAdapter(ABC):
    @abstractmethod
    def resolve(self, spec: VideoSourceSpec) -> ResolvedVideoSource:
        raise NotImplementedError
