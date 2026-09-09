from __future__ import annotations

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import time
from typing import Callable
from dataclasses import replace

from .catalog import labels_for_categories, load_catalog
from .config import settings
from .detectors import Detector, create_detector, route_categories
from .harness import HazardHarness
from .logging_config import logger
from .media import is_image, is_video, prepare_image, sample_video
from .schemas import AnalysisResult, Detection, FrameInfo, VLMFinding
from .vlm import VLMProvider, create_vlm_provider
from .algorithm_adapters import AlgorithmContext


ProgressCallback = Callable[[dict[str, object]], None]


class HazardPipeline:
    def __init__(self, detector: Detector | None = None, vlm: VLMProvider | None = None) -> None:
        self.detector = detector or create_detector()
        self.vlm = vlm
        self.harness = HazardHarness()
        self.last_metadata: dict[str, object] = {}

    def _prepare_frames(
        self,
        source: str | Path,
        *,
        source_type: str,
        job_dir: Path,
        sample_fps: float | None,
        duration_sec: int | None,
    ) -> list[FrameInfo]:
        frame_dir = job_dir / "frames"
        if source_type == "image":
            return prepare_image(Path(source), frame_dir)
        return sample_video(
            source,
            frame_dir,
            sample_fps=sample_fps or settings.default_video_fps,
            max_frames=settings.max_frames,
            duration_sec=duration_sec,
        )

    @staticmethod
    def _chunks(items: list, size: int) -> list[list]:
        return [items[index : index + size] for index in range(0, len(items), size)]

    def _select_label_groups(self, detections: list[Detection], frame_index: int) -> list[list]:
        full_scan = settings.full_scan_every_n_frames > 0 and frame_index % settings.full_scan_every_n_frames == 0
        if full_scan:
            return self._chunks(load_catalog(), settings.max_labels_per_call)
        categories = route_categories(detections)
        matched = labels_for_categories(categories)
        return self._chunks(matched, settings.max_labels_per_call)

    def analyze(
        self,
        *,
        job_id: str,
        source: str | Path,
        source_type: str,
        mode: str,
        job_dir: Path,
        sample_fps: float | None = None,
        duration_sec: int | None = None,
        context: AlgorithmContext | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> AnalysisResult:
        prepare_started = time.monotonic()
        frames = self._prepare_frames(
            source,
            source_type=source_type,
            job_dir=job_dir,
            sample_fps=sample_fps,
            duration_sec=duration_sec,
        )
        # Preparation ends when frames are available.  Do not include detector,
        # rule, or VLM latency in this metric.
        prepare_ms = round((time.monotonic() - prepare_started) * 1000, 1)
        result = self.analyze_frames(job_id=job_id, frames=frames, source_type=source_type, mode=mode, job_dir=job_dir, context=context, progress_callback=progress_callback)
        result.metadata["prepare_ms"] = prepare_ms
        return result

    def analyze_frames(
        self,
        *,
        job_id: str,
        frames: list[FrameInfo],
        source_type: str,
        mode: str,
        job_dir: Path,
        context: AlgorithmContext | None = None,
        detections_ready: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> AnalysisResult:
        started = time.monotonic()
        detector_started = time.monotonic()
        for frame in frames:
            frame.artifact_url = f"/api/v1/jobs/{job_id}/artifact/frames/{Path(frame.path).name}"
        annotated_dir = job_dir / "annotated"
        warnings: list[str] = []
        all_findings: list[VLMFinding] = []

        whole_adapter = os.getenv("ALGORITHM_ADAPTER_SCOPE", "vlm").strip().lower() == "whole" and os.getenv("ALGORITHM_ADAPTER_PROVIDER", "local").strip().lower() not in {"", "local", "none"}
        if not detections_ready and not whole_adapter:
            for detector_completed, frame in enumerate(frames, start=1):
                try:
                    frame.detections = self.detector.detect(frame, mode, annotated_dir)
                    if frame.annotated_path:
                        frame.annotated_url = f"/api/v1/jobs/{job_id}/artifact/annotated/{Path(frame.annotated_path).name}"
                except Exception:
                    logger.exception("小模型检测失败 frame=%s", frame.frame_id)
                    warnings.append(f"{frame.frame_id}小模型检测失败，本帧未获得候选对象")
                    frame.detections = []
                self._report_progress(progress_callback, "detector", detector_completed, len(frames), started)
        elif frames:
            self._report_progress(progress_callback, "detector", len(frames), len(frames), started)
        detector_ms = round((time.monotonic() - detector_started) * 1000, 1)
        vlm_started = time.monotonic()
        vlm_calls = 0
        vlm_successful_calls = 0
        vlm_failed_calls = 0
        vlm_call_ms: list[float] = []

        rules_started = time.monotonic()
        rule_candidates: list[VLMFinding] = []
        if not whole_adapter and mode in {"realtime", "test", "inspection"}:
            realtime_findings = self._realtime_rules(frames)
            if mode == "realtime":
                all_findings.extend(realtime_findings)
            else:
                # Rules are candidates for the VLM, not a second decision-maker:
                # an explicit VLM negative must never be overwritten by a rule hit.
                rule_candidates = realtime_findings

        if mode == "realtime" and not whole_adapter:
            pass
        else:
            if self.vlm is None:
                self.vlm = create_vlm_provider()
            batch_size = max(1, settings.vlm_max_images_per_call)
            parallelism = max(1, int(os.getenv("VLM_PARALLELISM", "4")))
            planned_groups = (
                [load_catalog() for _ in range(0, len(frames), batch_size)]
                if whole_adapter else [
                    group
                    for start in range(0, len(frames), batch_size)
                    for group in self._select_label_groups(
                        [item for frame in frames[start : start + batch_size] for item in frame.detections], start
                    )
                ]
            )
            vlm_completed = 0
            for start in range(0, len(frames), batch_size):
                batch = frames[start : start + batch_size]
                detections = [item for frame in batch for item in frame.detections]
                batch_ids = {frame.frame_id for frame in batch}
                batch_context = self._context_with_rule_candidates(
                    context,
                    [item for item in rule_candidates if batch_ids.intersection(item.source_frame_ids)],
                )
                # A whole algorithm owns label routing itself: one request receives
                # the full catalogue, rather than repeated partial-detector calls.
                groups = [load_catalog()] if whole_adapter else self._select_label_groups(detections, start)
                # Every routed label group is submitted. Bounded parallelism reduces
                # latency without silently dropping coverage under load.
                with ThreadPoolExecutor(max_workers=min(parallelism, len(groups) or 1), thread_name_prefix="hazard-vlm") as executor:
                    futures = {executor.submit(self._call_vlm, batch, labels, detections, batch_context): labels for labels in groups}
                    vlm_calls += len(futures)
                    for future in as_completed(futures):
                        labels = futures[future]
                        try:
                            response, elapsed_ms = future.result()
                            vlm_successful_calls += 1
                            vlm_call_ms.append(elapsed_ms)
                            validated, response_warnings = self.harness.validate_response(
                                response, {item.id for item in labels}
                            )
                            all_findings.extend(validated)
                            warnings.extend(response_warnings)
                        except Exception as exc:
                            logger.exception("VLM分析失败 frames=%s", [frame.frame_id for frame in batch])
                            vlm_failed_calls += 1
                            warnings.append(f"VLM分析失败（{','.join(frame.frame_id for frame in batch)}），本批结果未完成")
                        finally:
                            vlm_completed += 1
                            self._report_progress(progress_callback, "vlm", vlm_completed, len(planned_groups), started)

        completion_status = "complete"
        if vlm_failed_calls:
            completion_status = "failed" if vlm_failed_calls == vlm_calls else "partial"
        aggregated = self.harness.aggregate(all_findings, source_type=source_type)
        self.last_metadata = {
            "pipeline_ms": round((time.monotonic() - started) * 1000, 1),
            "detector_ms": detector_ms,
            "rules_ms": round((time.monotonic() - rules_started) * 1000, 1) if mode in {"realtime", "test", "inspection"} and not whole_adapter else 0,
            "vlm_ms": round((time.monotonic() - vlm_started) * 1000, 1) if mode != "realtime" or whole_adapter else 0,
            "vlm_calls": vlm_calls,
            "vlm_successful_calls": vlm_successful_calls,
            "vlm_failed_calls": vlm_failed_calls,
            "completion_status": completion_status,
            "vlm_call_ms": vlm_call_ms,
            "frame_count": len(frames), "vlm_parallelism": (max(1, int(os.getenv("VLM_PARALLELISM", "2"))) if mode != "realtime" else 0),
            "adapter": getattr(self.vlm, "provider_name", "not_called") if (mode != "realtime" or whole_adapter) else "not_called",
            "rule_candidates": [item.model_dump(mode="json") for item in rule_candidates],
        }
        return AnalysisResult(
            job_id=job_id,
            mode=mode,
            source_type=source_type,
            detector_provider=(getattr(self.vlm, "provider_name", "external_algorithm") if whole_adapter else self.detector.provider_name),
            detector_model=(getattr(self.vlm, "provider_name", "external_algorithm") if whole_adapter else settings.detector_model.name),
            vlm_provider=self.vlm.provider_name if (mode != "realtime" or whole_adapter) and self.vlm else "not_called",
            vlm_model=(getattr(self.vlm, "provider_name", "not_called") if whole_adapter else settings.qwen_model) if (mode != "realtime" or whole_adapter) else "not_called",
            frames=frames,
            findings=aggregated,
            # A failed or partial VLM scan cannot establish that no hazard exists.
            no_clear_hazard=completion_status == "complete" and not aggregated,
            warnings=warnings,
            metadata=self.last_metadata,
        )

    def _call_vlm(self, frames, labels, detections, context):
        # ContextVars do not cross executor threads. Restore project scope for
        # adapters that read it directly, while still passing explicit context.
        started = time.monotonic()
        if context and context.project_id:
            from .projects import current_project
            token = current_project.set(context.project_id)
            try:
                response = self.vlm.analyze(frames, labels, detections, context)
            finally:
                current_project.reset(token)
            return response, round((time.monotonic() - started) * 1000, 1)
        response = self.vlm.analyze(frames, labels, detections, context)
        return response, round((time.monotonic() - started) * 1000, 1)

    @staticmethod
    def _context_with_rule_candidates(
        context: AlgorithmContext | None, candidates: list[VLMFinding]
    ) -> AlgorithmContext | None:
        if not candidates:
            return context
        base = context or AlgorithmContext()
        metadata = dict(base.extra)
        metadata["rule_candidates"] = [item.model_dump(mode="json") for item in candidates]
        return replace(base, extra=metadata)

    @staticmethod
    def _report_progress(callback: ProgressCallback | None, stage: str, completed: int, total: int, started: float) -> None:
        if callback is None:
            return
        elapsed = max(0.0, time.monotonic() - started)
        remaining = (elapsed / completed * max(total - completed, 0)) if completed else None
        try:
            callback({
                "stage": stage,
                "completed": completed,
                "total": total,
                "elapsed_sec": round(elapsed, 3),
                "estimated_remaining_sec": round(remaining, 3) if remaining is not None else None,
            })
        except Exception:
            logger.exception("progress callback failed stage=%s", stage)

    @staticmethod
    def _intersection_over_head(head: Detection, helmet: Detection) -> float:
        hx1, hy1, hx2, hy2 = head.bbox_xyxy
        x1, y1, x2, y2 = helmet.bbox_xyxy
        inter_w = max(0, min(hx2, x2) - max(hx1, x1))
        inter_h = max(0, min(hy2, y2) - max(hy1, y1))
        area = max(1, (hx2 - hx1) * (hy2 - hy1))
        return (inter_w * inter_h) / area

    def _realtime_rules(self, frames: list[FrameInfo]) -> list[VLMFinding]:
        findings: list[VLMFinding] = []
        for frame in frames:
            heads = [item for item in frame.detections if item.label == "head"]
            helmets = [item for item in frame.detections if item.label == "helmet"]
            for head in heads:
                has_helmet = any(self._intersection_over_head(head, helmet) >= 0.15 for helmet in helmets)
                if not has_helmet:
                    findings.append(
                        VLMFinding(
                            label_id="H003",
                            status="confirmed_hazard" if head.score >= 0.35 else "review_required",
                            evidence=f"{frame.frame_id}检测到清晰头部，但头部区域未检测到安全帽；零微调基线需人工复核。",
                            visible_objects=["head"],
                            source_frame_ids=[frame.frame_id],
                            inspection_visibility="clear" if head.score >= 0.35 else "partial",
                            severity="general",
                            severity_reason="未佩戴安全帽属于当前实时试运行规则中的一般隐患",
                            severity_source="realtime_rule",
                        )
                    )
        return findings
