from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import Mock
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.algorithm_adapters import AlgorithmContext, HttpAlgorithmAdapter, MockAlgorithmAdapter
from app.harness import HazardHarness
from app.schemas import VLMFinding, VLMResponse
from app.schemas import FrameInfo
from app.streams import LatestFrameSlot, StreamManager, StreamState
from app.notifications import Recipient


class AlgorithmIntegrationTests(unittest.TestCase):
    def test_latest_slot_discards_backlog(self) -> None:
        slot, stopped = LatestFrameSlot(), threading.Event()
        first = slot.put("old", 1.0)
        slot.put("latest", 4.0)
        sequence, timestamp, image = slot.take_after(first, stopped)
        self.assertEqual((2, 4.0, "latest"), (sequence, timestamp, image))

    def test_public_state_reports_actual_processing_fps(self) -> None:
        state = StreamState("fps", "cam", "0", 2, "A", False, started_at=time.time() - 2)
        state.realtime_processed = 3
        self.assertGreaterEqual(state.public()["actual_fps"], 1.4)

    def test_mock_adapter_has_offline_contract_and_context(self) -> None:
        adapter = MockAlgorithmAdapter()
        self.assertEqual("ok", adapter.health()["status"])
        self.assertTrue(adapter.capabilities()["structured_output"])
        response = adapter.analyze([], [], [], AlgorithmContext(project_id="p1", work_area="A", camera_id="c", task_id="t", frame_timestamp=1))
        self.assertEqual([], response.findings)

    def test_confirmed_finding_with_no_hazard_evidence_is_rejected(self) -> None:
        response = VLMResponse(findings=[VLMFinding(label_id="H003", status="confirmed_hazard", evidence="画面未发现任何隐患或异常。", source_frame_ids=["f1"], inspection_visibility="clear", severity="general", severity_reason="contradiction")])
        accepted, warnings = HazardHarness("direct").validate_response(response, {"H003"})
        self.assertEqual([], accepted)
        self.assertTrue(any("无隐患" in warning for warning in warnings))

    def test_slow_inspection_keeps_only_latest_pending_frame(self) -> None:
        manager = StreamManager()
        state = StreamState("slow", "cam", "0", 2, "A", False)
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        seen = []
        def slow(_state, frame, _timestamp):
            seen.append(frame.frame_id); entered.set(); release.wait(1); done.set()
        manager._inspect_once = slow
        def frame(name): return FrameInfo(frame_id=name, path="x", width=1, height=1, blur_score=1, brightness=1)
        manager._schedule_inspection(state, frame("first"), 1)
        self.assertTrue(entered.wait(1))
        manager._schedule_inspection(state, frame("old-pending"), 2)
        manager._schedule_inspection(state, frame("latest"), 3)
        release.set(); self.assertTrue(done.wait(1))
        deadline=time.monotonic()+1
        while state.inspection_running and time.monotonic()<deadline: time.sleep(.01)
        self.assertEqual(["first", "latest"], seen)
        self.assertEqual(1, state.inspection_skipped)

    def test_stopped_stream_does_not_write_findings(self) -> None:
        manager = StreamManager(); state = StreamState("stopped", "cam", "0", 2, "A", False); state.stop_event.set()
        manager._emit(state, FrameInfo(frame_id="f",path="x",width=1,height=1,blur_score=1,brightness=1), [], 1, {})
        self.assertEqual(0, state.emitted_events)

    def test_stop_discards_latest_pending_inspection(self) -> None:
        manager = StreamManager(); state = StreamState("stop-pending", "cam", "0", 2, "A", False)
        entered, release = threading.Event(), threading.Event(); seen = []
        def slow(_state, frame, _timestamp):
            seen.append(frame.frame_id); entered.set(); release.wait(1)
        manager._inspect_once = slow
        def frame(name): return FrameInfo(frame_id=name, path="x", width=1, height=1, blur_score=1, brightness=1)
        manager._schedule_inspection(state, frame("running"), 1); self.assertTrue(entered.wait(1))
        manager._schedule_inspection(state, frame("pending"), 2)
        manager._states[state.stream_id] = state
        manager.stop(state.stream_id); release.set()
        deadline = time.monotonic() + 1
        while state.inspection_running and time.monotonic() < deadline: time.sleep(.01)
        self.assertEqual(["running"], seen)
        self.assertIsNone(state.pending_inspection)

    def test_stopped_stream_never_creates_ticket_or_notification(self) -> None:
        manager = StreamManager(); state = StreamState("stopped-confirmed", "cam", "0", 2, "A", True); state.stop_event.set()
        finding = VLMFinding(label_id="H003", status="confirmed_hazard", evidence="可见未佩戴安全帽", source_frame_ids=["f"], inspection_visibility="clear", severity="general", severity_reason="test")
        with patch("app.streams.workflow_store.create_from_findings") as create, patch("app.streams.send_email") as email:
            manager._emit(state, FrameInfo(frame_id="f",path="x",width=1,height=1,blur_score=1,brightness=1), [finding], 1, {})
        create.assert_not_called(); email.assert_not_called()

    def test_email_groups_findings_by_eligible_recipient(self) -> None:
        manager = StreamManager(); state = StreamState("email-groups", "cam", "0", 2, "A", True)
        general = VLMFinding(label_id="H003", status="confirmed_hazard", evidence="general", source_frame_ids=["f"], inspection_visibility="clear", severity="general", severity_reason="test")
        major = VLMFinding(label_id="H004", status="confirmed_hazard", evidence="major", source_frame_ids=["f"], inspection_visibility="clear", severity="major", severity_reason="test")
        sent = []
        def recipients(severity, *_args, **_kwargs):
            return [Recipient("safety_officer", "普通", "general@example.com")] if severity == "general" else [Recipient("project_manager", "重大", "major@example.com")]
        def send(**kwargs):
            sent.append((kwargs["recipients"][0].email, kwargs["subject"])); return [{"status": "sent"}]
        self.assertTrue(manager._email_slots.acquire(blocking=False))
        with patch("app.streams.resolve_recipients", side_effect=recipients), patch("app.streams.send_email", side_effect=send), patch("app.streams.workflow_store.record_email_status"):
            manager._send_email(state, [general, major], [{"id":"g", "group_id":"group"}, {"id":"m", "group_id":"group"}], Path("x"), {})
        self.assertEqual({"general@example.com", "major@example.com"}, {item[0] for item in sent})
        self.assertTrue(any("一般隐患" in subject for email, subject in sent if email == "general@example.com"))
        self.assertTrue(any("重大隐患" in subject for email, subject in sent if email == "major@example.com"))

    def test_http_adapter_sends_rule_candidates_as_metadata(self) -> None:
        with TemporaryDirectory() as temp:
            image = Path(temp) / "f.jpg"; image.write_bytes(b"not-an-image")
            frame = FrameInfo(frame_id="f", path=str(image), width=1, height=1, blur_score=1, brightness=1)
            adapter = HttpAlgorithmAdapter("http://example.invalid")
            with patch.object(adapter, "_request", return_value={"findings": []}) as request:
                adapter.analyze([frame], [], [], AlgorithmContext(extra={"rule_candidates": [{"label_id":"H003"}]}))
        self.assertEqual("H003", request.call_args.args[1]["metadata"]["rule_candidates"][0]["label_id"])
