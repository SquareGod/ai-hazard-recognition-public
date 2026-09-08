from __future__ import annotations

import unittest

from app.harness import HazardHarness
from app.schemas import VLMFinding, VLMResponse


class HarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = HazardHarness()

    def test_unknown_label_is_rejected(self) -> None:
        response = VLMResponse(
            findings=[
                VLMFinding(
                    label_id="OUTSIDE",
                    status="confirmed_hazard",
                    evidence="可见证据",
                    source_frame_ids=["frame_0001"],
                    inspection_visibility="clear",
                    severity="general",
                    severity_reason="测试一般隐患",
                )
            ]
        )
        accepted, warnings = self.harness.validate_response(response, {"H003"})
        self.assertEqual([], accepted)
        self.assertTrue(warnings)

    def test_hypothetical_evidence_is_dropped_even_in_direct_mode(self) -> None:
        response = VLMResponse(
            findings=[
                VLMFinding(
                    label_id="H037",
                    status="confirmed_hazard",
                    evidence="配电箱若设于该区域，则可能处于积水环境。",
                    visible_objects=["积水"],
                    source_frame_ids=["frame_0001"],
                    inspection_visibility="clear",
                    severity="major",
                    severity_reason="假设性判断",
                )
            ]
        )
        accepted, warnings = HazardHarness("direct").validate_response(response, {"H037"})
        self.assertEqual([], accepted)
        self.assertTrue(any("假设性证据" in item for item in warnings))

    def test_required_visual_anchor_rejects_cross_category_hallucination(self) -> None:
        response = VLMResponse(
            findings=[
                VLMFinding(
                    label_id="H018",
                    status="confirmed_hazard",
                    evidence="沟槽内存在大量积水。",
                    visible_objects=["沟槽", "积水"],
                    source_frame_ids=["frame_0001"],
                    inspection_visibility="clear",
                    severity="major",
                    severity_reason="积水明显",
                )
            ]
        )
        accepted, warnings = HazardHarness("direct").validate_response(response, {"H018"})
        self.assertEqual([], accepted)
        self.assertTrue(any("关键对象" in item for item in warnings))

    def test_external_evidence_label_is_downgraded_in_strict_mode(self) -> None:
        response = VLMResponse(
            findings=[
                VLMFinding(
                    label_id="H006",
                    status="confirmed_hazard",
                    evidence="楼层存在材料堆",
                    source_frame_ids=["frame_0001"],
                    inspection_visibility="clear",
                    severity="major",
                    severity_reason="测试重大隐患",
                )
            ]
        )
        accepted, warnings = HazardHarness("strict").validate_response(response, {"H006"})
        self.assertEqual("review_required", accepted[0].status)
        self.assertTrue(accepted[0].missing_external_evidence)
        self.assertTrue(warnings)

    def test_direct_mode_preserves_clear_model_judgement(self) -> None:
        response = VLMResponse(
            findings=[
                VLMFinding(
                    label_id="H006",
                    status="confirmed_hazard",
                    evidence="clear visible material stack at slab edge",
                    source_frame_ids=["frame_0001"],
                    inspection_visibility="clear",
                    severity="major",
                    severity_reason="存在严重后果",
                )
            ]
        )
        accepted, warnings = HazardHarness("direct").validate_response(response, {"H006"})
        self.assertEqual("confirmed_hazard", accepted[0].status)
        self.assertFalse(accepted[0].missing_external_evidence)
        self.assertFalse(warnings)

    def test_model_severity_is_aggregated_as_two_level_result(self) -> None:
        response = VLMResponse(
            findings=[
                VLMFinding(
                    label_id="H003",
                    status="confirmed_hazard",
                    evidence="人员头部未见安全帽",
                    source_frame_ids=["frame_0001"],
                    inspection_visibility="clear",
                    severity="general",
                    severity_reason="单人未佩戴安全帽，模型暂定一般隐患",
                )
            ]
        )
        accepted, _ = self.harness.validate_response(response, {"H003"})
        aggregated = self.harness.aggregate(accepted, source_type="image")
        self.assertEqual("general", aggregated[0].severity)
        self.assertEqual("一般隐患", aggregated[0].severity_name)
        self.assertEqual("model", aggregated[0].severity_source)


if __name__ == "__main__":
    unittest.main()

