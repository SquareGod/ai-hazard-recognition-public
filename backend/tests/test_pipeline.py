from __future__ import annotations

import tempfile
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from app.detectors import MockDetector
from app.pipeline import HazardPipeline
from app.vlm import MockVLMProvider
from app.schemas import FrameInfo, VLMResponse


class CapturingVLM(MockVLMProvider):
    def __init__(self, *, fail: bool = False, delay: float = 0) -> None:
        self.calls = []; self.fail = fail; self.delay = delay

    def analyze(self, frames, labels, detections, context=None):
        self.calls.append((frames, labels, detections, context))
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("token=do-not-expose")
        return VLMResponse(findings=[])


class PipelineTests(unittest.TestCase):
    def test_realtime_mock_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image_path = root / "sample.jpg"
            cv2.imencode(".jpg", np.full((480, 640, 3), 180, dtype=np.uint8))[1].tofile(str(image_path))
            pipeline = HazardPipeline(detector=MockDetector(), vlm=MockVLMProvider())
            result = pipeline.analyze(
                job_id="test-job",
                source=image_path,
                source_type="image",
                mode="realtime",
                job_dir=root / "job",
            )
            self.assertEqual("H003", result.findings[0].label_id)
            self.assertEqual("confirmed_hazard", result.findings[0].final_status)
            self.assertFalse(result.no_clear_hazard)
            self.assertTrue(result.frames[0].artifact_url.endswith("/frames/frame_0001.jpg"))

    def test_inspection_mock_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image_path = root / "sample.jpg"
            cv2.imencode(".jpg", np.full((480, 640, 3), 180, dtype=np.uint8))[1].tofile(str(image_path))
            pipeline = HazardPipeline(detector=MockDetector(), vlm=MockVLMProvider())
            result = pipeline.analyze(
                job_id="inspection-job",
                source=image_path,
                source_type="image",
                mode="inspection",
                job_dir=root / "job",
            )
            self.assertTrue(result.no_clear_hazard)
            self.assertEqual("mock", result.vlm_provider)
            self.assertIsNotNone(result.frames[0].artifact_url)
            self.assertIsNone(result.frames[0].annotated_url)

    def test_progress_callback_reports_detector_and_vlm_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); image_path = root / "sample.jpg"
            cv2.imencode(".jpg", np.full((20, 20, 3), 180, dtype=np.uint8))[1].tofile(str(image_path))
            updates: list[dict] = []
            HazardPipeline(detector=MockDetector(), vlm=MockVLMProvider()).analyze(
                job_id="progress-job", source=image_path, source_type="image", mode="inspection", job_dir=root / "job", progress_callback=updates.append
            )
            self.assertEqual("detector", updates[0]["stage"])
            self.assertTrue(any(item["stage"] == "vlm" for item in updates))
            self.assertEqual({"stage", "completed", "total", "elapsed_sec", "estimated_remaining_sec"}, set(updates[-1]))

    def test_prepare_ms_excludes_inference_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); image_path = root / "sample.jpg"
            cv2.imencode(".jpg", np.full((20, 20, 3), 180, dtype=np.uint8))[1].tofile(str(image_path))
            vlm = CapturingVLM(delay=.04)
            pipeline = HazardPipeline(detector=MockDetector(), vlm=vlm)
            original = pipeline._prepare_frames
            def slow_prepare(*args, **kwargs):
                time.sleep(.02); return original(*args, **kwargs)
            pipeline._prepare_frames = slow_prepare
            result = pipeline.analyze(job_id="timing", source=image_path, source_type="image", mode="inspection", job_dir=root / "job")
            self.assertGreater(result.metadata["pipeline_ms"], result.metadata["prepare_ms"])
            self.assertLess(result.metadata["prepare_ms"], 40)

    def test_inspection_runs_rules_as_vlm_metadata_without_overriding_negative(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); image_path = root / "sample.jpg"
            cv2.imencode(".jpg", np.full((20, 20, 3), 180, dtype=np.uint8))[1].tofile(str(image_path))
            vlm = CapturingVLM()
            result = HazardPipeline(detector=MockDetector(), vlm=vlm).analyze(job_id="rules", source=image_path, source_type="image", mode="inspection", job_dir=root / "job")
            self.assertTrue(vlm.calls[0][2])
            self.assertEqual("H003", vlm.calls[0][3].extra["rule_candidates"][0]["label_id"])
            self.assertEqual([], result.findings)

    def test_vlm_failure_is_not_reported_as_no_hazard_and_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); image_path = root / "sample.jpg"
            cv2.imencode(".jpg", np.full((20, 20, 3), 180, dtype=np.uint8))[1].tofile(str(image_path))
            result = HazardPipeline(detector=MockDetector(), vlm=CapturingVLM(fail=True)).analyze(job_id="failed", source=image_path, source_type="image", mode="inspection", job_dir=root / "job")
            self.assertFalse(result.no_clear_hazard)
            self.assertEqual("failed", result.metadata["completion_status"])
            self.assertEqual(0, result.metadata["vlm_successful_calls"])
            self.assertEqual(result.metadata["vlm_calls"], result.metadata["vlm_failed_calls"])
            self.assertGreater(result.metadata["vlm_failed_calls"], 0)
            self.assertNotIn("token=", " ".join(result.warnings))

    def test_whole_adapter_realtime_uses_full_catalog_once_and_reports_provider(self) -> None:
        frame = FrameInfo(frame_id="f1", path="unused.jpg", width=1, height=1, blur_score=1, brightness=1)
        vlm = CapturingVLM()
        with patch.dict(os.environ, {"ALGORITHM_ADAPTER_SCOPE": "whole", "ALGORITHM_ADAPTER_PROVIDER": "mock"}, clear=False):
            result = HazardPipeline(detector=MockDetector(), vlm=vlm).analyze_frames(job_id="whole", frames=[frame], source_type="camera", mode="realtime", job_dir=Path("."))
        self.assertEqual(1, len(vlm.calls))
        self.assertGreater(len(vlm.calls[0][1]), 1)
        self.assertEqual("mock", result.metadata["adapter"])
        self.assertEqual("mock", result.detector_provider)


if __name__ == "__main__":
    unittest.main()
