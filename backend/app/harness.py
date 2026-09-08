from __future__ import annotations

from collections import defaultdict
import re
from pathlib import Path

import yaml

from .catalog import catalog_by_id
from .config import settings
from .schemas import AggregatedFinding, VLMFinding, VLMResponse
from .severity import severity_resolver


class HazardHarness:
    _HYPOTHETICAL_EVIDENCE = ("如果", "若设", "若存在", "可能布置", "推断为", "假定", "假设")
    _CONTRADICTORY_EVIDENCE = ("不能确认", "无法确认该隐患", "无法判定该隐患", "未出现，故", "未见，故")
    _NO_HAZARD_EVIDENCE = re.compile(r"(?:未发现|未见|没有|无).{0,8}(?:隐患|风险|异常)")

    def __init__(self, decision_mode: str | None = None) -> None:
        policy_path = settings.root / "config" / "hazard_policies.yaml"
        policies = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
        self.external_required: dict[str, str] = dict(policies.get("external_evidence_required", {}))
        self.measurement_required: dict[str, str] = dict(policies.get("measurement_or_calibration_required", {}))
        self.single_image_direct: set[str] = set(policies.get("single_image_direct_visual", []))
        self.visual_anchor_required: dict[str, list[str]] = dict(policies.get("visual_anchor_required", {}))
        self.catalog = catalog_by_id()
        self.decision_mode = decision_mode or settings.decision_mode

    def validate_response(self, response: VLMResponse, allowed_ids: set[str]) -> tuple[list[VLMFinding], list[str]]:
        accepted: list[VLMFinding] = []
        warnings: list[str] = []
        for finding in response.findings:
            if finding.label_id not in self.catalog:
                warnings.append(f"丢弃标签库外输出：{finding.label_id}")
                continue
            if finding.label_id not in allowed_ids:
                warnings.append(f"丢弃本次候选范围外输出：{finding.label_id}")
                continue
            if any(marker in finding.evidence for marker in self._HYPOTHETICAL_EVIDENCE):
                warnings.append(f"丢弃{finding.label_id}的假设性证据：画面未直接证明目标对象或状态")
                continue
            if any(marker in finding.evidence for marker in self._CONTRADICTORY_EVIDENCE):
                warnings.append(f"丢弃{finding.label_id}的自相矛盾证据：证据文本明确表示无法确认")
                continue
            if finding.status == "confirmed_hazard" and self._NO_HAZARD_EVIDENCE.search(finding.evidence):
                warnings.append(f"丢弃{finding.label_id}的无隐患矛盾证据")
                continue
            anchors = self.visual_anchor_required.get(finding.label_id, [])
            visible_text = " ".join(finding.visible_objects)
            if anchors and not any(anchor in visible_text for anchor in anchors):
                warnings.append(f"丢弃{finding.label_id}：关键对象未出现在visible_objects中")
                continue
            if not finding.source_frame_ids:
                finding.status = "review_required"
                warnings.append(f"{finding.label_id}缺少来源帧，已降级复核")
            if finding.status == "confirmed_hazard" and finding.inspection_visibility != "clear":
                finding.status = "review_required"
                warnings.append(f"{finding.label_id}检查区域不清晰，已降级复核")
            if (
                self.decision_mode == "strict"
                and settings.downgrade_external_evidence_labels
                and finding.label_id in self.external_required
            ):
                requirement = self.external_required[finding.label_id]
                if requirement not in finding.missing_external_evidence:
                    finding.missing_external_evidence.append(requirement)
                if finding.status == "confirmed_hazard":
                    finding.status = "review_required"
                    warnings.append(f"{finding.label_id}依赖外部证据，不能仅凭图像确认")
            if (
                self.decision_mode == "strict"
                and finding.label_id in self.measurement_required
                and finding.status == "confirmed_hazard"
            ):
                requirement = self.measurement_required[finding.label_id]
                if requirement not in finding.missing_external_evidence:
                    finding.missing_external_evidence.append(requirement)
                finding.status = "review_required"
                warnings.append(f"{finding.label_id}需要测量或标定，已降级复核")
            accepted.append(finding)
        return accepted, warnings

    def aggregate(
        self,
        findings: list[VLMFinding],
        *,
        source_type: str,
    ) -> list[AggregatedFinding]:
        grouped: dict[str, list[VLMFinding]] = defaultdict(list)
        for item in findings:
            grouped[item.label_id].append(item)

        output: list[AggregatedFinding] = []
        for label_id, items in grouped.items():
            label = self.catalog[label_id]
            confirmed = [item for item in items if item.status == "confirmed_hazard"]
            frame_ids = sorted({frame_id for item in items for frame_id in item.source_frame_ids})
            notes: list[str] = []
            if self.decision_mode == "direct":
                # Preserve a confirmed model judgement when its evidence is clear.
                can_confirm = bool(confirmed)
            elif source_type == "image":
                can_confirm = (
                    settings.allow_single_image_confirmation
                    and label_id in self.single_image_direct
                    and bool(confirmed)
                )
                if confirmed and not can_confirm:
                    notes.append("该标签不在单图直接确认清单中，保守降级为复核")
            else:
                can_confirm = len({frame_id for item in confirmed for frame_id in item.source_frame_ids}) >= settings.video_confirm_frames
                if confirmed and not can_confirm:
                    notes.append(f"未达到{settings.video_confirm_frames}个独立帧确认条件")

            decisions = [
                severity_resolver.resolve(
                    label_id=label_id,
                    model_severity=item.severity,
                    model_reason=item.severity_reason,
                    model_source=item.severity_source,
                )
                for item in items
            ]
            # 同一隐患跨帧等级不一致时采用更高等级，避免遗漏模型已识别出的重大后果。
            selected = next((item for item in decisions if item.severity == "major"), decisions[0])

            output.append(
                AggregatedFinding(
                    label_id=label_id,
                    category=label.category,
                    name=label.name,
                    final_status="confirmed_hazard" if can_confirm else "review_required",
                    evidence=list(dict.fromkeys(item.evidence for item in items)),
                    source_frame_ids=frame_ids,
                    occurrence_count=len(items),
                    missing_external_evidence=sorted(
                        {value for item in items for value in item.missing_external_evidence}
                    ),
                    harness_notes=notes,
                    severity=selected.severity,
                    severity_name="重大隐患" if selected.severity == "major" else "一般隐患",
                    severity_reason=selected.reason,
                    severity_source=selected.source,
                    severity_rule_version=selected.rule_version,
                )
            )
        return sorted(output, key=lambda item: (item.final_status != "confirmed_hazard", item.label_id))

