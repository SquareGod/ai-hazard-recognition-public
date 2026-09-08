from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import yaml

from .config import settings


Severity = Literal["general", "major"]
SeveritySource = Literal["model", "realtime_rule", "catalog_rule"]


@dataclass(frozen=True)
class SeverityDecision:
    severity: Severity
    reason: str
    source: SeveritySource
    rule_version: str


class SeverityResolver:
    """Resolve the two-level severity while keeping a stable future rule interface."""

    def __init__(self) -> None:
        path = settings.root / "config" / "hazard_severity.yaml"
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        self.version = str(payload.get("version") or "model-provisional-v1")
        self.labels: dict[str, dict] = dict(payload.get("labels") or {})
        deadlines = dict(payload.get("deadlines") or {})
        self.deadlines: dict[Severity, str] = {
            "general": str(deadlines.get("general") or "3天内"),
            "major": str(deadlines.get("major") or "24小时内"),
        }

    def resolve(
        self,
        *,
        label_id: str,
        model_severity: Severity,
        model_reason: str,
        model_source: SeveritySource = "model",
    ) -> SeverityDecision:
        rule = self.labels.get(label_id)
        if rule:
            configured = str(rule.get("severity") or "").strip()
            if configured not in {"general", "major"}:
                raise ValueError(f"{label_id}正式等级只能是general或major")
            return SeverityDecision(
                severity=configured,  # type: ignore[arg-type]
                reason=str(rule.get("reason") or f"{label_id}命中正式隐患等级规则"),
                source="catalog_rule",
                rule_version=self.version,
            )
        return SeverityDecision(
            severity=model_severity,
            reason=model_reason.strip() or "视觉模型根据画面可见风险后果给出暂定等级",
            source=model_source,
            rule_version=self.version,
        )

    def deadline(self, severity: Severity) -> str:
        return self.deadlines[severity]


severity_resolver = SeverityResolver()

