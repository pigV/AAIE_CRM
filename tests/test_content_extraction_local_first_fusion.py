import json
import unittest
from pathlib import Path

from content_extraction.billing import FreeOnlyBillingGate
from content_extraction.models import OCRResult, SubtitleSegment
from content_extraction.ocr.box_merge import merge_provider_text
from content_extraction.ocr.critical_tokens import (
    critical_token_conflicts,
    extract_critical_tokens,
)
from content_extraction.ocr.normalization import (
    normalize_for_comparison,
    similarity_signals,
)
from content_extraction.ocr.local_first import (
    FrameOCRFusion,
    FusionConflict,
    FusionMetricsSnapshot,
    LocalFirstOCRFusionCoordinator,
    select_unique_vote,
)
from content_extraction.ocr.temporal import merge_fused_frames


def ocr(provider, text, confidence=.9, boxes=(), status="completed"):
    return OCRResult(
        provider, 1000, 2000, text, confidence, tuple(boxes), status,
        f"{provider}-v1", "" if status == "completed" else "FAILED",
        "" if status == "completed" else "provider failed",
        bounding_boxes=tuple(
            box.get("bounding_box", {}) for box in boxes if isinstance(box, dict)
        ),
    )


class OCRComparisonLayerTests(unittest.TestCase):
    def test_normalization_is_comparison_only_and_handles_nfkc_case_space_and_punctuation(self):
        raw = "  澳洲\n低龄留学　ＶＩＳＡ！ "
        self.assertEqual(normalize_for_comparison(raw), "澳洲低龄留学visa")
        self.assertEqual(raw, "  澳洲\n低龄留学　ＶＩＳＡ！ ")
        self.assertEqual(
            normalize_for_comparison("澳洲低龄留学 visa"),
            normalize_for_comparison("澳洲低龄留学 VISA。"),
        )

    def test_similarity_exposes_exact_ratio_edit_distance_and_overlap(self):
        signals = similarity_signals("孩子最好在10岁以前", "在10岁以前进入英语环境")
        self.assertFalse(signals.exact)
        self.assertGreater(signals.ratio, 0)
        self.assertGreater(signals.levenshtein_distance, 0)
        self.assertEqual(signals.suffix_prefix_overlap, "在10岁以前")

    def test_critical_tokens_accept_case_only_but_reject_acronym_age_and_visa_changes(self):
        self.assertEqual(
            critical_token_conflicts((ocr("paddle", "申请 VISA"), ocr("easyocr", "申请 visa"))),
            (),
        )
        acronym = critical_token_conflicts((
            ocr("paddle", "环境VISA"), ocr("easyocr", "环境 VISF")
        ))
        age = critical_token_conflicts((
            ocr("paddle", "孩子10岁"), ocr("easyocr", "孩子12岁")
        ))
        visa = critical_token_conflicts((
            ocr("paddle", "申请500签证"), ocr("easyocr", "申请590签证")
        ))
        self.assertIn("ASCII_ACRONYM", {item.token_type for item in acronym})
        self.assertIn("AGE", {item.token_type for item in age})
        self.assertIn("VISA_SUBCLASS", {item.token_type for item in visa})

    def test_school_names_and_amounts_are_critical_tokens(self):
        conflict = critical_token_conflicts((
            ocr("paddle", "Scotch College 学费 AUD 50000"),
            ocr("easyocr", "Scots College 学费 AUD 59000"),
        ))
        kinds = {item.token_type for item in conflict}
        self.assertIn("PROPER_NAME", kinds)
        self.assertIn("AMOUNT", kinds)
        self.assertTrue(extract_critical_tokens("2027年成功率95% ATAR"))

    def test_provider_box_merge_uses_line_alignment_then_x_order(self):
        boxes = (
            {"text": "VISA", "bounding_box": {"x": 800, "y": 102, "width": 90, "height": 40}},
            {"text": "孩子最好在10岁以前进入英语环境", "bounding_box": {"x": 100, "y": 100, "width": 680, "height": 44}},
            {"text": "第二行", "bounding_box": {"x": 100, "y": 180, "width": 160, "height": 40}},
        )
        result = ocr("paddle", "wrong order", boxes=boxes)
        self.assertEqual(merge_provider_text(result), "孩子最好在10岁以前进入英语环境VISA\n第二行")


class FakeRepository:
    def __init__(self):
        self.cache = {}
        self.attempts = []

    def get_cached_ocr_result(self, image_hash, provider, model_version, preprocessing_version):
        return self.cache.get((image_hash, provider, model_version, preprocessing_version))

    def record_ocr_attempt(self, segment_id, provider, status, error_code="", error_message=""):
        self.attempts.append((segment_id, provider, status))

    def save_completed_ocr_result(self, segment_id, result, preprocessing_version):
        self.cache[("image-sha", result.provider, result.model_version, preprocessing_version)] = result


class FakeAdapter:
    def __init__(self, provider, responses, billing_type):
        self.provider = provider
        self.model_version = provider + "-v1"
        self.billing_type = billing_type
        self.responses = list(responses)
        self.calls = 0

    def recognize(self, segment):
        self.calls += 1
        response = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return OCRResult(
            self.provider, segment.start_ms, segment.end_ms,
            response.text, response.confidence, response.boxes, response.status,
            self.model_version, response.error_code, response.error_message,
            bounding_boxes=response.bounding_boxes,
        )


def fusion_setup(paddle, easyocr, tencent=None, iflytek=None, allowed=()):
    repo = FakeRepository()
    locals_ = (
        FakeAdapter("paddle", [paddle], "local"),
        FakeAdapter("easyocr", [easyocr], "local"),
    )
    clouds = tuple(
        FakeAdapter(name, [value], "cloud")
        for name, value in (("tencent", tencent), ("iflytek", iflytek))
        if value is not None
    )
    env = {f"{name.upper()}_OCR_FREE_ONLY_ALLOWED": "true" for name in allowed}
    coordinator = LocalFirstOCRFusionCoordinator(
        repo, locals_, clouds, FreeOnlyBillingGate.from_environ(env),
        cloud_order=("tencent", "iflytek"),
    )
    segment = SubtitleSegment(
        "frame-1", "video-1", 1000, 2000, "/fake.jpg",
        "image-sha", "crop-v1",
    )
    return coordinator, locals_, {item.provider: item for item in clouds}, segment


class LocalFirstCoordinatorTests(unittest.TestCase):
    def test_local_raw_exact_is_high_and_never_calls_cloud(self):
        coordinator, locals_, clouds, segment = fusion_setup(
            ocr("paddle", "澳洲低龄留学"), ocr("easyocr", "澳洲低龄留学"),
            tencent=ocr("tencent", "不应调用"), allowed=("tencent",),
        )
        fused = coordinator.fuse(segment)
        self.assertEqual(fused.fusion_status, "LOCAL_CONSENSUS_EXACT")
        self.assertEqual(fused.confidence_level, "HIGH")
        self.assertEqual(fused.fused_text, "澳洲低龄留学")
        self.assertFalse(fused.cloud_called)
        self.assertEqual(clouds["tencent"].calls, 0)
        self.assertEqual(set(fused.providers_used), {"paddle", "easyocr"})

    def test_local_case_punctuation_and_visa_case_are_similar_without_cloud(self):
        coordinator, _, clouds, segment = fusion_setup(
            ocr("paddle", "澳洲低龄留学 VISA"),
            ocr("easyocr", "澳洲低龄留学 visa。"),
            tencent=ocr("tencent", "不应调用"), allowed=("tencent",),
        )
        fused = coordinator.fuse(segment)
        self.assertEqual(fused.fusion_status, "LOCAL_CONSENSUS_SIMILAR")
        self.assertEqual(fused.conflicts, ())
        self.assertEqual(fused.critical_tokens[0].value, "VISA")
        self.assertEqual(clouds["tencent"].calls, 0)

    def test_real_smoke_fixture_is_critical_then_tencent_resolves_two_of_three(self):
        fixture = json.loads((
            Path(__file__).parent
            / "fixtures/content_extraction/providers/ocr/four_provider_smoke.json"
        ).read_text(encoding="utf-8"))
        providers = fixture["providers"]
        coordinator, _, clouds, segment = fusion_setup(
            ocr("paddle", providers["paddle"]["text"], providers["paddle"]["confidence"]),
            ocr("easyocr", providers["easyocr"]["text"], providers["easyocr"]["confidence"]),
            tencent=ocr("tencent", providers["tencent"]["text"], providers["tencent"]["confidence"]),
            iflytek=ocr("iflytek", providers["iflytek"]["text"], providers["iflytek"]["confidence"]),
            allowed=("tencent", "iflytek"),
        )
        fused = coordinator.fuse(segment)
        self.assertEqual(fused.local_fusion_status, "LOCAL_CONFLICT_CRITICAL")
        self.assertEqual(fused.fusion_status, "CLOUD_ARBITRATED_MAJORITY")
        self.assertEqual(fused.fused_text, "孩子最好在10岁以前进入英语环境VISA")
        self.assertEqual(fused.cloud_call_count, 1)
        self.assertEqual(fused.agreement_count, 2)
        self.assertEqual(clouds["tencent"].calls, 1)
        self.assertEqual(clouds["iflytek"].calls, 0)
        self.assertEqual(fused.conflicts[0].type, "ASCII_ACRONYM")
        self.assertIn("2/3", fused.selection_reason)
        self.assertEqual(fused.metrics.providers["tencent"]["calls"], 1)
        self.assertEqual(fused.metrics.providers["iflytek"]["calls"], 0)
        self.assertEqual(fused.metrics.fusion["cloud_arbitration_count"], 1)
        self.assertEqual(fused.metrics.fusion["tencent_arbitration_count"], 1)
        self.assertEqual(fused.to_dict()["cloud_call_count"], 1)
        local_conflicts = critical_token_conflicts((
            ocr("paddle", providers["paddle"]["text"]),
            ocr("easyocr", providers["easyocr"]["text"]),
        ))
        self.assertIn("ASCII_ACRONYM", {item.token_type for item in local_conflicts})

    def test_low_local_conflict_is_distinct_from_critical_before_unresolved_output(self):
        coordinator, _, _, segment = fusion_setup(
            ocr("paddle", "澳洲留学建议"), ocr("easyocr", "家长教育规划")
        )
        fused = coordinator.fuse(segment)
        self.assertEqual(fused.local_fusion_status, "LOCAL_CONFLICT_LOW")
        self.assertEqual(fused.fusion_status, "LOCAL_CONFLICT_UNRESOLVED")

    def test_free_only_blocks_cloud_and_returns_traceable_local_unresolved(self):
        coordinator, _, clouds, segment = fusion_setup(
            ocr("paddle", "孩子10岁"), ocr("easyocr", "孩子12岁"),
            tencent=ocr("tencent", "孩子10岁"), allowed=(),
        )
        fused = coordinator.fuse(segment)
        self.assertEqual(fused.fusion_status, "LOCAL_CONFLICT_UNRESOLVED")
        self.assertEqual(fused.cloud_call_count, 0)
        self.assertEqual(clouds["tencent"].calls, 0)
        self.assertTrue(fused.fused_text)
        self.assertTrue(fused.alternatives)

    def test_first_cloud_failure_allows_second_cloud_only_when_free(self):
        coordinator, _, clouds, segment = fusion_setup(
            ocr("paddle", "申请500签证"), ocr("easyocr", "申请590签证"),
            tencent=ocr("tencent", "", status="failed"),
            iflytek=ocr("iflytek", "申请500签证"),
            allowed=("tencent", "iflytek"),
        )
        fused = coordinator.fuse(segment)
        self.assertEqual(clouds["tencent"].calls, 1)
        self.assertEqual(clouds["iflytek"].calls, 1)
        self.assertEqual(fused.cloud_call_count, 2)
        self.assertEqual(fused.fused_text, "申请500签证")

    def test_one_local_success_without_free_cloud_is_not_very_high(self):
        coordinator, _, _, segment = fusion_setup(
            ocr("paddle", "唯一字幕"), ocr("easyocr", "", status="failed")
        )
        fused = coordinator.fuse(segment)
        self.assertEqual(fused.fusion_status, "LOCAL_ONLY_SINGLE_PROVIDER")
        self.assertNotEqual(fused.confidence_level, "VERY_HIGH")
        self.assertEqual(fused.fused_text, "唯一字幕")

    def test_vote_evaluator_accepts_two_one_one_but_rejects_two_two(self):
        winner, count = select_unique_vote((
            ocr("paddle", "A VISA"), ocr("easyocr", "B VISF"),
            ocr("tencent", "C VISC"), ocr("iflytek", "A VISA"),
        ))
        self.assertEqual(winner[0].provider, "paddle")
        self.assertEqual(count, 2)

        winner, count = select_unique_vote((
            ocr("paddle", "A VISA"), ocr("easyocr", "B VISF"),
            ocr("tencent", "A VISA"), ocr("iflytek", "B VISF"),
        ))
        self.assertIsNone(winner)
        self.assertEqual(count, 2)

    def test_two_of_three_stops_before_fourth_provider_even_if_fourth_would_tie(self):
        coordinator, _, clouds, segment = fusion_setup(
            ocr("paddle", "A VISA"), ocr("easyocr", "B VISF"),
            tencent=ocr("tencent", "A VISA"), iflytek=ocr("iflytek", "B VISF"),
            allowed=("tencent", "iflytek"),
        )
        fused = coordinator.fuse(segment)
        self.assertEqual(fused.fusion_status, "CLOUD_ARBITRATED_MAJORITY")
        self.assertEqual(clouds["tencent"].calls, 1)
        self.assertEqual(clouds["iflytek"].calls, 0)

    def test_cloud_order_is_configurable(self):
        repo = FakeRepository()
        paddle = FakeAdapter("paddle", [ocr("paddle", "A VISA")], "local")
        easy = FakeAdapter("easyocr", [ocr("easyocr", "B VISF")], "local")
        tencent = FakeAdapter("tencent", [ocr("tencent", "A VISA")], "cloud")
        iflytek = FakeAdapter("iflytek", [ocr("iflytek", "B VISF")], "cloud")
        gate = FreeOnlyBillingGate.from_environ({
            "TENCENT_OCR_FREE_ONLY_ALLOWED": "true",
            "IFLYTEK_OCR_FREE_ONLY_ALLOWED": "true",
        })
        coordinator = LocalFirstOCRFusionCoordinator(
            repo, (paddle, easy), (tencent, iflytek), gate,
            cloud_order=("iflytek", "tencent"),
        )
        segment = SubtitleSegment("s", "r", 0, 1000, "/fake", "image-sha", "crop-v1")
        fused = coordinator.fuse(segment)
        self.assertEqual(iflytek.calls, 1)
        self.assertEqual(tencent.calls, 0)
        self.assertEqual(fused.fused_text, "B VISF")

    def test_both_local_failures_can_recover_from_two_matching_free_clouds(self):
        coordinator, _, clouds, segment = fusion_setup(
            ocr("paddle", "", status="failed"),
            ocr("easyocr", "", status="failed"),
            tencent=ocr("tencent", "云端字幕"),
            iflytek=ocr("iflytek", "云端字幕"),
            allowed=("tencent", "iflytek"),
        )
        fused = coordinator.fuse(segment)
        self.assertEqual(fused.fusion_status, "CLOUD_CONSENSUS")
        self.assertEqual(fused.fused_text, "云端字幕")
        self.assertEqual((clouds["tencent"].calls, clouds["iflytek"].calls), (1, 1))

    def test_three_and_four_way_conflicts_remain_automatic_and_traceable(self):
        three, _, _, segment = fusion_setup(
            ocr("paddle", "2026年"), ocr("easyocr", "2027年"),
            tencent=ocr("tencent", "2028年"), allowed=("tencent",),
        )
        three_result = three.fuse(segment)
        self.assertEqual(three_result.fusion_status, "ALL_PROVIDER_CONFLICT")
        self.assertTrue(three_result.fused_text)

        four, _, clouds, segment = fusion_setup(
            ocr("paddle", "2026年"), ocr("easyocr", "2027年"),
            tencent=ocr("tencent", "2028年"), iflytek=ocr("iflytek", "2029年"),
            allowed=("tencent", "iflytek"),
        )
        four_result = four.fuse(segment)
        self.assertEqual(four_result.fusion_status, "ALL_PROVIDER_CONFLICT")
        self.assertEqual(clouds["iflytek"].calls, 1)
        self.assertEqual(len(four_result.provider_results), 4)

    def test_confidence_is_only_tie_breaker_after_agreement_and_completeness(self):
        coordinator, _, _, segment = fusion_setup(
            ocr("paddle", "A VISA", .7), ocr("easyocr", "B VISF", .95)
        )
        fused = coordinator.fuse(segment)
        self.assertEqual(fused.fusion_status, "LOCAL_CONFLICT_UNRESOLVED")
        self.assertEqual(fused.fused_text, "B VISF")
        self.assertIn("tie-breaker", fused.selection_reason)

    def test_zero_confidence_first_cloud_is_invalid_and_second_is_attempted(self):
        coordinator, _, clouds, segment = fusion_setup(
            ocr("paddle", "A VISA"), ocr("easyocr", "B VISF"),
            tencent=ocr("tencent", "A VISA", 0),
            iflytek=ocr("iflytek", "A VISA"),
            allowed=("tencent", "iflytek"),
        )
        fused = coordinator.fuse(segment)
        self.assertEqual((clouds["tencent"].calls, clouds["iflytek"].calls), (1, 1))
        self.assertEqual(fused.fused_text, "A VISA")

    def test_box_merge_happens_before_critical_token_comparison(self):
        paddle_boxes = (
            {"text": "VISA", "bounding_box": {"x": 800, "y": 100, "width": 80, "height": 40}},
            {"text": "澳洲低龄留学", "bounding_box": {"x": 100, "y": 100, "width": 680, "height": 40}},
        )
        coordinator, _, clouds, segment = fusion_setup(
            ocr("paddle", "unmerged text", boxes=paddle_boxes),
            ocr("easyocr", "澳洲低龄留学VISA"),
            tencent=ocr("tencent", "不应调用"), allowed=("tencent",),
        )
        fused = coordinator.fuse(segment)
        self.assertEqual(fused.fusion_status, "LOCAL_CONSENSUS_EXACT")
        self.assertEqual(fused.conflicts, ())
        self.assertEqual(clouds["tencent"].calls, 0)

    def test_completed_cache_prevents_duplicate_local_and_cloud_calls(self):
        coordinator, locals_, clouds, segment = fusion_setup(
            ocr("paddle", "A VISA"), ocr("easyocr", "B VISF"),
            tencent=ocr("tencent", "A VISA"), allowed=("tencent",),
        )
        first = coordinator.fuse(segment)
        second = coordinator.fuse(segment)
        self.assertEqual(first.cloud_call_count, 1)
        self.assertEqual(second.cloud_call_count, 0)
        self.assertEqual([item.calls for item in locals_], [1, 1])
        self.assertEqual(clouds["tencent"].calls, 1)


def fused_frame(frame_id, start, end, text, status="LOCAL_CONSENSUS_EXACT", conflicts=()):
    return FrameOCRFusion(
        frame_id, start, start, end, text, normalize_for_comparison(text),
        status, "HIGH", ("paddle", "easyocr"), ("paddle", "easyocr"),
        ("paddle", "easyocr"), (), False, 0, 2, (),
        extract_critical_tokens(text), tuple(conflicts), (), "paddle",
        "test fixture", {}, FusionMetricsSnapshot({}, {}),
    )


class TemporalOCRMergeTests(unittest.TestCase):
    def test_exact_repeat_extends_one_transcript_entry(self):
        transcript = merge_fused_frames((
            fused_frame("f1", 1000, 2000, "澳洲低龄留学"),
            fused_frame("f2", 2000, 3000, "澳洲低龄留学。"),
        ))
        self.assertEqual(len(transcript.segments), 1)
        self.assertEqual(transcript.segments[0].text, "澳洲低龄留学")
        self.assertEqual((transcript.segments[0].start_ms, transcript.segments[0].end_ms), (1000, 3000))
        self.assertEqual(transcript.segments[0].frame_ids, ("f1", "f2"))

    def test_progressive_subtitle_keeps_only_longest_text(self):
        transcript = merge_fused_frames((
            fused_frame("f1", 0, 1000, "澳洲"),
            fused_frame("f2", 1000, 2000, "澳洲低龄"),
            fused_frame("f3", 2000, 3000, "澳洲低龄留学"),
        ))
        self.assertEqual(transcript.text, "澳洲低龄留学")
        self.assertEqual(len(transcript.segments), 1)

    def test_overlap_appends_only_non_overlapping_suffix(self):
        transcript = merge_fused_frames((
            fused_frame("f1", 0, 1000, "孩子最好在10岁以前"),
            fused_frame("f2", 1000, 2000, "在10岁以前进入英语环境"),
        ))
        self.assertEqual(transcript.text, "孩子最好在10岁以前进入英语环境")
        self.assertEqual(len(transcript.segments), 1)

    def test_replacement_creates_new_timed_entry_and_keeps_conflict_metadata(self):
        conflict = FusionConflict("AGE", {"paddle": ["10岁"], "easyocr": ["12岁"]})
        transcript = merge_fused_frames((
            fused_frame("f1", 0, 1000, "为什么要低龄留学", conflicts=(conflict,)),
            fused_frame("f2", 1000, 2000, "很多家长问我"),
        ))
        self.assertEqual(len(transcript.segments), 2)
        self.assertEqual(transcript.text, "为什么要低龄留学\n很多家长问我")
        self.assertEqual(transcript.segments[0].conflicts, (conflict,))


if __name__ == "__main__":
    unittest.main()
