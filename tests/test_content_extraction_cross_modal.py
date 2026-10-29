import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from content_extraction.fusion.alignment import (
    build_temporal_components,
    calculate_temporal_overlap,
)
from content_extraction.fusion.evidence import (
    ModalityEvidence,
    classify_asr_evidence,
    classify_ocr_evidence,
)
from content_extraction.fusion.cross_modal import (
    CrossModalFusionEngine,
    CrossModalFusionResult,
    CrossModalSegment,
)
from content_extraction.models import ASRFusedSegment, ASRFusionConflict, ASRFusionResult
from content_extraction.ocr.evidence import OCRCrossModalEvidenceResolver
from content_extraction.ocr.evidence_gate import (
    OCRFrameEvidenceDecision,
    evaluate_frame_evidence,
)
from content_extraction.ocr.temporal import (
    OCRFusedTranscript,
    TemporalOCRSegment,
    merge_fused_frames,
)
from content_extraction.preprocessing.opencv_subtitles import CandidateEvidence
from content_extraction.shared.critical_tokens import extract_critical_tokens


def frame(
    frame_id,
    text="EAL课程",
    start_ms=0,
    end_ms=1000,
    providers=("paddle", "easyocr"),
    agreement_count=2,
    fusion_status="LOCAL_CONSENSUS_EXACT",
    alternatives=(),
    conflicts=(),
    bounding_region=None,
    cloud_call_count=0,
):
    return SimpleNamespace(
        frame_id=frame_id,
        fused_text=text,
        start_ms=start_ms,
        end_ms=end_ms,
        providers_used=tuple(providers),
        agreement_count=agreement_count,
        fusion_status=fusion_status,
        confidence_level="HIGH",
        alternatives=tuple(alternatives),
        conflicts=tuple(conflicts),
        critical_tokens=(),
        bounding_region=bounding_region or {},
        selection_reason="fixture provider agreement",
        selected_source=providers[0] if providers else "",
        local_providers=tuple(item for item in providers if item in {"paddle", "easyocr"}),
        cloud_providers=tuple(item for item in providers if item in {"tencent", "iflytek"}),
        cloud_called=cloud_call_count > 0,
        cloud_call_count=cloud_call_count,
    )


def asr_segment(
    text="EAL课程",
    start_ms=0,
    end_ms=1000,
    fusion_status="ASR_CONSENSUS_EXACT",
    confidence_level="HIGH",
    provider_texts=None,
    conflicts=(),
    alternatives=(),
    asr_consensus=True,
):
    return ASRFusedSegment(
        start_ms=start_ms,
        end_ms=end_ms,
        raw_start_ms=start_ms,
        raw_end_ms=end_ms,
        fused_text=text,
        text_source="whispercpp",
        timestamp_source="whispercpp",
        timestamp_quality="native",
        timestamp_adjustment=None,
        provider_texts=provider_texts or {"funasr": text, "whispercpp": text},
        alternatives=tuple(alternatives),
        conflicts=tuple(conflicts),
        critical_tokens=(),
        selection_reason="fixture ASR selection",
        alignment_score=1.0,
        alignment_metrics=None,
        fusion_status=fusion_status,
        confidence_level=confidence_level,
        asr_consensus=asr_consensus,
        final_truth=False,
        domain_candidates=(),
        raw_metadata={},
    )


def asr_result(segments=(), source_video_id="video-1", status="ASR_CONSENSUS_EXACT"):
    segments = tuple(segments)
    return ASRFusionResult(
        full_text="".join(item.fused_text for item in segments),
        segments=segments,
        words=(),
        providers_used=("funasr", "whispercpp") if segments else (),
        provider_results=(),
        language="zh",
        duration_ms=max((item.end_ms for item in segments), default=0),
        timestamp_granularity="segment" if segments else "none",
        fusion_status=status,
        confidence_level="HIGH" if segments else "LOW",
        asr_consensus=bool(segments),
        final_truth=False,
        raw_metadata={"source_video_id": source_video_id, "final_truth": False},
    )


def ocr_transcript(
    text,
    *,
    start_ms=0,
    end_ms=1000,
    source_video_id="video-1",
    role="spoken_subtitle",
    providers=("paddle", "easyocr"),
    agreement_count=2,
    fusion_status="LOCAL_CONSENSUS_EXACT",
):
    item = frame(
        "f1", text=text, start_ms=start_ms, end_ms=end_ms,
        providers=providers, agreement_count=agreement_count,
        fusion_status=fusion_status,
    )
    return merge_fused_frames(
        (item,), source_video_id=source_video_id, duration_ms=end_ms,
        segment_roles={"f1": role},
    )


def asr_critical_segment(selected, left, right, start_ms=0, end_ms=1000):
    alternatives = tuple(value for value in (left, right) if value != selected)
    conflict = ASRFusionConflict(
        conflict_type="ASR_CRITICAL_TOKEN_CONFLICT",
        severity="HIGH",
        category="ASCII_ACRONYM",
        provider_values={"funasr": (left,), "whispercpp": (right,)},
        alternatives=tuple(dict.fromkeys((left, right))),
        selected_value=selected,
        selection_reason="fixture ASR critical candidate",
        requires_cross_modal_evidence=True,
    )
    return asr_segment(
        selected, start_ms, end_ms,
        fusion_status="ASR_CRITICAL_TOKEN_CONFLICT",
        confidence_level="LOW",
        provider_texts={"funasr": left, "whispercpp": right},
        conflicts=(conflict,), alternatives=alternatives, asr_consensus=False,
    )


class OCRCrossModalEvidenceCompatibilityTests(unittest.TestCase):
    def test_old_transcript_constructor_remains_compatible_and_missing_provenance_is_explicit(self):
        segment = TemporalOCRSegment(0, 1000, "EAL课程", ("f1",), ("LOCAL_CONSENSUS_EXACT",), ())
        transcript = OCRFusedTranscript((segment,))

        evidence = OCRCrossModalEvidenceResolver(transcript).resolve(segment)

        self.assertEqual(transcript.text, "EAL课程")
        self.assertFalse(evidence.provenance_complete)
        self.assertEqual(evidence.evidence_completeness, "missing")
        self.assertEqual(evidence.providers_used, ())
        self.assertEqual(evidence.text_role, "unknown")

    def test_resolver_aggregates_traceable_frames_without_raw_provider_payloads(self):
        first = frame(
            "f1",
            providers=("paddle", "easyocr"),
            bounding_region={"x": 100, "y": 700, "width": 500, "height": 50},
        )
        second = frame(
            "f2",
            start_ms=900,
            end_ms=1800,
            providers=("paddle", "easyocr", "tencent"),
            agreement_count=3,
            fusion_status="CLOUD_ARBITRATED_MAJORITY",
            alternatives=("ESL课程",),
            bounding_region={"x": 90, "y": 695, "width": 530, "height": 60},
            cloud_call_count=1,
        )

        transcript = merge_fused_frames(
            (first, second),
            source_video_id="video-1",
            duration_ms=2000,
            segment_roles={"f1|f2": "spoken_subtitle"},
        )
        segment = transcript.segments[0]
        evidence = OCRCrossModalEvidenceResolver(transcript).resolve(segment)

        self.assertTrue(evidence.provenance_complete)
        self.assertEqual(evidence.evidence_completeness, "complete")
        self.assertEqual(evidence.frame_ids, ("f1", "f2"))
        self.assertEqual(set(evidence.providers_used), {"paddle", "easyocr", "tencent"})
        self.assertEqual(evidence.provider_count, 3)
        self.assertEqual(evidence.agreement_count, 3)
        self.assertEqual(evidence.alternatives, ("ESL课程",))
        self.assertEqual(
            evidence.bounding_region,
            {"x": 90, "y": 695, "width": 530, "height": 60},
        )
        self.assertEqual(evidence.cloud_call_count, 1)
        self.assertTrue(evidence.cloud_arbitrated)
        self.assertEqual(evidence.text_role, "spoken_subtitle")
        serialized = evidence.to_dict()
        self.assertNotIn("raw_response", serialized)
        self.assertEqual(
            {item["frame_id"] for item in serialized["provider_provenance"]},
            {"f1", "f2"},
        )

    def test_missing_one_referenced_frame_is_partial_and_not_complete(self):
        segment = TemporalOCRSegment(
            0, 1000, "EAL课程", ("f1", "missing"),
            ("LOCAL_CONSENSUS_EXACT",), (),
        )
        transcript = OCRFusedTranscript(
            (segment,), frame_fusions=(frame("f1"),), source_video_id="video-1"
        )

        evidence = OCRCrossModalEvidenceResolver(transcript).resolve(segment)

        self.assertFalse(evidence.provenance_complete)
        self.assertEqual(evidence.evidence_completeness, "partial")
        self.assertEqual(evidence.missing_frame_ids, ("missing",))

    def test_unhashable_conflict_provenance_is_preserved_without_crashing(self):
        conflict = SimpleNamespace(type="AGE", values={"paddle": ["10岁"], "easyocr": ["12岁"]})
        item = frame("f1", conflicts=(conflict, conflict))
        transcript = merge_fused_frames(
            (item,), source_video_id="video-1", segment_roles={"f1": "spoken_subtitle"}
        )

        evidence = OCRCrossModalEvidenceResolver(transcript).resolve(transcript.segments[0])

        self.assertEqual(len(evidence.conflicts), 1)
        self.assertEqual(evidence.to_dict()["conflicts"][0]["type"], "AGE")


class CrossModalSchemaTests(unittest.TestCase):
    def test_schema_serialization_preserves_both_modalities_and_never_claims_truth(self):
        overlap = calculate_temporal_overlap(0, 1000, 100, 900)
        evidence = ModalityEvidence(
            "ocr", "STRONG", ("LOCAL_CONSENSUS_EXACT",), ("HIGH",),
            ("paddle", "easyocr"), 2, 2, False, False, True,
            ("two provider OCR agreement",),
        )
        segment = CrossModalSegment(
            id="cm-0001", start_ms=0, end_ms=1000,
            clean_text="EAL课程", normalized_text="eal课程",
            ocr_text="EAL课程", asr_text="ESL课程",
            selected_source="cross_modal", included_in_clean_transcript=True,
            text_role="spoken_subtitle", content_relation="safely_similar",
            ocr_evidence=evidence, asr_evidence=evidence,
            temporal_overlap=overlap, text_similarity={"ratio": 0.8},
            critical_tokens=(), conflicts=(), alternatives=("ESL课程",),
            selection_reason="fixture decision", fusion_status="CROSS_MODAL_RESOLVED",
            resolution_level=2, confidence_level="HIGH",
            ocr_provenance={"frame_ids": ["f1"]},
            asr_provenance={"providers_used": ["funasr", "whispercpp"]},
            provisional_selection=False, final_truth=False,
        )
        result = CrossModalFusionResult(
            clean_raw_transcript="EAL课程", segments=(segment,),
            included_segment_ids=("cm-0001",), excluded_segment_ids=(),
            ocr_fusion_result=OCRFusedTranscript(()),
            asr_fusion_result=asr_result(()), duration_ms=1000,
            fusion_status="CROSS_MODAL_RESOLVED", confidence_level="HIGH",
            source_identity={"source_video_id": "video-1"}, final_truth=False,
            raw_metadata={"fixture": True},
        )

        serialized = result.to_dict()

        self.assertFalse(serialized["final_truth"])
        self.assertFalse(serialized["segments"][0]["final_truth"])
        self.assertEqual(serialized["segments"][0]["alternatives"], ["ESL课程"])
        self.assertIn("ocr_fusion_result", serialized)
        self.assertIn("asr_fusion_result", serialized)
        self.assertNotIn("confidence", serialized)


class CrossModalTemporalTests(unittest.TestCase):
    def test_literal_overlap_metrics_and_strength_boundaries(self):
        strong = calculate_temporal_overlap(0, 1000, 100, 900)
        moderate = calculate_temporal_overlap(0, 1000, 650, 1450)
        weak = calculate_temporal_overlap(0, 1000, 1100, 1500)
        none = calculate_temporal_overlap(0, 1000, 1300, 1500)

        self.assertEqual(strong.intersection_ms, 800)
        self.assertEqual(strong.shorter_coverage_ratio, 1.0)
        self.assertEqual(strong.iou_ratio, 0.8)
        self.assertEqual(strong.midpoint_distance_ms, 0)
        self.assertEqual(strong.strength, "STRONG")
        self.assertEqual(moderate.strength, "MODERATE")
        self.assertEqual(weak.boundary_gap_ms, 100)
        self.assertEqual(weak.strength, "WEAK")
        self.assertEqual(none.strength, "NONE")

    def test_midpoint_mismatch_prevents_strong_and_weak_edge_does_not_connect(self):
        long_asr = calculate_temporal_overlap(0, 1000, 0, 5000)
        self.assertEqual(long_asr.strength, "MODERATE")

        ocr = (
            TemporalOCRSegment(0, 1000, "第一句", ("f1",), (), ()),
            TemporalOCRSegment(2100, 3000, "第二句", ("f2",), (), ()),
        )
        asr = (
            asr_segment("第一句", 100, 900),
            asr_segment("第二句", 1200, 2000),
        )
        components = build_temporal_components(ocr, asr)

        self.assertEqual(len(components), 3)
        self.assertEqual(components[0].ocr_indexes, (0,))
        self.assertEqual(components[0].asr_indexes, (0,))
        self.assertEqual(components[1].asr_indexes, (1,))
        self.assertEqual(components[2].ocr_indexes, (1,))
        self.assertTrue(any(edge.overlap.strength == "WEAK" for edge in components[1].weak_edges))

    def test_one_ocr_can_connect_multiple_ordered_asr_segments(self):
        ocr = (TemporalOCRSegment(0, 2000, "完整一句", ("f1",), (), ()),)
        asr = (asr_segment("完整", 0, 900), asr_segment("一句", 900, 1900))

        components = build_temporal_components(ocr, asr)

        self.assertEqual(len(components), 1)
        self.assertEqual(components[0].ocr_indexes, (0,))
        self.assertEqual(components[0].asr_indexes, (0, 1))


class CrossModalEvidenceTests(unittest.TestCase):
    def test_ocr_strength_requires_complete_provenance_and_real_agreement(self):
        complete = OCRCrossModalEvidenceResolver(merge_fused_frames(
            (frame("f1"),), source_video_id="video-1",
            segment_roles={"f1": "spoken_subtitle"},
        )).resolve(merge_fused_frames((frame("f1"),)).segments[0])
        missing_segment = TemporalOCRSegment(0, 1000, "EAL课程", ("missing",), (), ())
        missing = OCRCrossModalEvidenceResolver(
            OCRFusedTranscript((missing_segment,))
        ).resolve(missing_segment)

        self.assertEqual(classify_ocr_evidence(complete).strength, "STRONG")
        self.assertEqual(classify_ocr_evidence(missing).strength, "WEAK")

    def test_asr_exact_consensus_is_strong_but_critical_conflict_is_weak(self):
        exact = asr_segment("EAL课程")
        critical = asr_segment(
            "GPA很重要", fusion_status="ASR_CRITICAL_TOKEN_CONFLICT",
            confidence_level="LOW", provider_texts={"funasr": "GPS很重要", "whispercpp": "GPA很重要"},
            alternatives=("GPS很重要",), asr_consensus=False,
        )

        self.assertEqual(classify_asr_evidence((exact,), ("funasr", "whispercpp")).strength, "STRONG")
        weak = classify_asr_evidence((critical,), ("funasr", "whispercpp"))
        self.assertEqual(weak.strength, "WEAK")
        self.assertTrue(weak.critical_conflict)

    def test_provider_control_tag_payloads_are_not_critical_tokens(self):
        tokens = extract_critical_tokens(
            "<|zh|><|NEUTRAL|><|Speech|><|withitn|>普通正文"
        )

        self.assertEqual(tokens, ())

    def test_post_ocr_gate_rejects_ui_and_accepts_compact_spoken_subtitle(self):
        ui = frame("ui", text="LIKE 99 VISA", bounding_region={
            "x": 2, "y": 2, "width": 30, "height": 10,
        })
        subtitle = frame("subtitle", text="孩子申请VISA", bounding_region={
            "x": 20, "y": 5, "width": 240, "height": 42,
        })
        crop = CandidateEvidence.for_test(top=0.45, height=0.08)

        ui_decision = evaluate_frame_evidence(ui, "ui", crop)
        subtitle_decision = evaluate_frame_evidence(
            subtitle, "spoken_subtitle", crop
        )

        self.assertFalse(ui_decision.accepted)
        self.assertEqual(ui_decision.rejection_reason, "role_ui")
        self.assertTrue(subtitle_decision.accepted)

    def test_excluded_ocr_is_retained_for_diagnostics_but_cannot_create_cross_modal_conflict(self):
        excluded = frame(
            "ui", text="VISA 500 LIKE 99", start_ms=0, end_ms=1000,
            bounding_region={"x": 0, "y": 0, "width": 300, "height": 50},
        )
        decision = OCRFrameEvidenceDecision(
            accepted=False, role="ui", rejection_reason="role_ui",
            line_count=1, text_density=0.1, box_geometry_valid=True,
        )
        transcript = merge_fused_frames(
            (excluded,), source_video_id="video-1", duration_ms=1000,
            segment_roles={"ui": "ui"}, evidence_decisions={"ui": decision},
        )

        result = CrossModalFusionEngine().fuse(
            transcript,
            asr_result((asr_segment("孩子正常上课", 0, 1000),)),
        )

        self.assertEqual(transcript.segments, ())
        self.assertEqual(len(transcript.frame_fusions), 1)
        self.assertEqual(transcript.excluded_frame_ids, ("ui",))
        self.assertEqual(result.segments[0].fusion_status, "ASR_ONLY_SEGMENT")
        self.assertEqual(result.segments[0].conflicts, ())
        self.assertIsNone(result.segments[0].ocr_evidence)


class CrossModalResolutionTests(unittest.TestCase):
    def setUp(self):
        self.engine = CrossModalFusionEngine()

    def test_exact_consensus_uses_both_modalities_without_fixed_priority(self):
        result = self.engine.fuse(
            ocr_transcript("孩子申请500签证"),
            asr_result((asr_segment("孩子申请500签证"),)),
        )

        segment = result.segments[0]
        self.assertEqual(segment.fusion_status, "CROSS_MODAL_CONSENSUS")
        self.assertEqual(segment.selected_source, "cross_modal")
        self.assertEqual(segment.confidence_level, "VERY_HIGH")
        self.assertEqual(result.clean_raw_transcript, "孩子申请500签证")
        self.assertFalse(segment.final_truth)

    def test_strong_visual_eal_resolves_asr_esl_consensus(self):
        result = self.engine.fuse(
            ocr_transcript("EAL课要是高二还没过的话"),
            asr_result((asr_segment(
                "ESL课要是高二还没过的话",
                provider_texts={
                    "funasr": "ESL课要是高二还没过的话",
                    "whispercpp": "ESL课要是高二还没过的话",
                },
            ),)),
        )

        segment = result.segments[0]
        self.assertEqual(segment.clean_text, "EAL课要是高二还没过的话")
        self.assertEqual(segment.fusion_status, "CROSS_MODAL_CRITICAL_TOKEN_RESOLVED")
        self.assertEqual(segment.confidence_level, "HIGH")
        self.assertEqual(set(segment.alternatives), {
            "EAL课要是高二还没过的话", "ESL课要是高二还没过的话"
        })
        self.assertIn("strong_ocr_evidence", segment.selection_reason)
        self.assertTrue(segment.asr_provenance["asr_consensus"])
        self.assertFalse(segment.final_truth)

    def test_single_provider_eal_cannot_override_esl_consensus(self):
        result = self.engine.fuse(
            ocr_transcript(
                "EAL课", providers=("paddle",), agreement_count=1,
                fusion_status="LOCAL_ONLY_SINGLE_PROVIDER",
            ),
            asr_result((asr_segment("ESL课"),)),
        )

        segment = result.segments[0]
        self.assertEqual(segment.clean_text, "ESL课")
        self.assertEqual(segment.fusion_status, "CROSS_MODAL_CRITICAL_CONFLICT")
        self.assertEqual(segment.confidence_level, "LOW")
        self.assertTrue(segment.provisional_selection)

    def test_ocr_gpa_resolves_internal_asr_gps_gpa_conflict(self):
        result = self.engine.fuse(
            ocr_transcript("GPA很重要"),
            asr_result((asr_critical_segment("GPA很重要", "GPS很重要", "GPA很重要"),)),
        )

        segment = result.segments[0]
        self.assertEqual(segment.clean_text, "GPA很重要")
        self.assertEqual(segment.fusion_status, "CROSS_MODAL_CRITICAL_TOKEN_RESOLVED")
        self.assertIn("ocr_confirms_asr_selected_candidate", segment.selection_reason)
        self.assertIn("GPS很重要", segment.alternatives)
        self.assertEqual(
            segment.asr_provenance["underlying_conflicts"][0]["conflict_type"],
            "ASR_CRITICAL_TOKEN_CONFLICT",
        )

    def test_ocr_gps_supports_alternative_but_does_not_erase_asr_conflict(self):
        result = self.engine.fuse(
            ocr_transcript("GPS很重要"),
            asr_result((asr_critical_segment("GPA很重要", "GPS很重要", "GPA很重要"),)),
        )

        segment = result.segments[0]
        self.assertEqual(segment.clean_text, "GPA很重要")
        self.assertEqual(segment.fusion_status, "CROSS_MODAL_CRITICAL_CONFLICT")
        self.assertEqual(segment.confidence_level, "LOW")
        self.assertTrue(segment.provisional_selection)
        self.assertIn("GPS很重要", segment.alternatives)

    def test_numeric_and_visa_conflicts_remain_unresolved_without_cross_support(self):
        for ocr_text, asr_text in (("孩子10岁", "孩子12岁"), ("申请500签证", "申请590签证")):
            with self.subTest(ocr_text=ocr_text):
                result = self.engine.fuse(
                    ocr_transcript(ocr_text),
                    asr_result((asr_segment(asr_text),)),
                )
                segment = result.segments[0]
                self.assertEqual(segment.clean_text, asr_text)
                self.assertEqual(segment.fusion_status, "CROSS_MODAL_CRITICAL_CONFLICT")
                self.assertTrue(segment.conflicts)

    def test_ordinary_chinese_is_resolved_by_cross_modal_provider_corroboration(self):
        ordinary_conflict = ASRFusionConflict(
            conflict_type="ASR_TEXT_CONFLICT", severity="MEDIUM", category="TEXT",
            provider_values={"funasr": ("学校教务安排",), "whispercpp": ("学校教育安排",)},
            alternatives=("学校教务安排", "学校教育安排"), selected_value="学校教务安排",
            selection_reason="fixture ordinary text conflict",
            requires_cross_modal_evidence=False,
        )
        asr = asr_segment(
            "学校教务安排",
            fusion_status="ASR_TEXT_CONFLICT", confidence_level="MEDIUM",
            provider_texts={"funasr": "学校教务安排", "whispercpp": "学校教育安排"},
            alternatives=("学校教育安排",), conflicts=(ordinary_conflict,),
            asr_consensus=False,
        )
        result = self.engine.fuse(
            ocr_transcript("学校教育安排"), asr_result((asr,), status="ASR_TEXT_CONFLICT")
        )

        segment = result.segments[0]
        self.assertEqual(segment.clean_text, "学校教育安排")
        self.assertEqual(segment.fusion_status, "CROSS_MODAL_RESOLVED")
        self.assertEqual(segment.selected_source, "cross_modal")
        self.assertIn("学校教务安排", segment.alternatives)

    def test_localized_graduation_typo_uses_strong_subtitle_and_near_provider_evidence(self):
        ordinary_conflict = ASRFusionConflict(
            conflict_type="ASR_TEXT_CONFLICT", severity="MEDIUM", category="TEXT",
            provider_values={"funasr": ("配不了业",), "whispercpp": ("可以养业",)},
            alternatives=("配不了业", "可以养业"), selected_value="配不了业",
            selection_reason="fixture ordinary text conflict",
            requires_cross_modal_evidence=False,
        )
        asr = asr_segment(
            "配不了业", fusion_status="ASR_TEXT_CONFLICT", confidence_level="MEDIUM",
            provider_texts={"funasr": "配不了业", "whispercpp": "可以养业"},
            alternatives=("可以养业",), conflicts=(ordinary_conflict,),
            asr_consensus=False,
        )

        result = self.engine.fuse(
            ocr_transcript("可以毕业"), asr_result((asr,), status="ASR_TEXT_CONFLICT")
        )

        segment = result.segments[0]
        self.assertEqual(segment.clean_text, "可以毕业")
        self.assertEqual(segment.fusion_status, "CROSS_MODAL_RESOLVED")
        self.assertIn("localized", segment.selection_reason)
        self.assertEqual(set(segment.alternatives), {"可以毕业", "配不了业", "可以养业"})

    def test_subtitle_compression_keeps_spoken_asr_and_never_concatenates(self):
        ocr_text = "刚来澳洲英语适应需要时间"
        asr_text = "孩子刚来澳洲以后英语适应需要时间"
        result = self.engine.fuse(
            ocr_transcript(ocr_text), asr_result((asr_segment(asr_text),))
        )

        segment = result.segments[0]
        self.assertEqual(segment.content_relation, "subtitle_compression")
        self.assertEqual(segment.clean_text, asr_text)
        self.assertEqual(segment.selected_source, "asr")
        self.assertNotIn(ocr_text + asr_text, result.clean_raw_transcript)

    def test_broader_paraphrase_keeps_asr_provisional_and_preserves_ocr(self):
        result = self.engine.fuse(
            ocr_transcript("选择学校很关键"),
            asr_result((asr_segment("家长需要提前准备签证材料"),)),
        )

        segment = result.segments[0]
        self.assertEqual(segment.content_relation, "paraphrase_or_content_difference")
        self.assertEqual(segment.clean_text, "家长需要提前准备签证材料")
        self.assertTrue(segment.provisional_selection)
        self.assertIn("选择学校很关键", segment.alternatives)


class CrossModalModalityOnlyAndTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.engine = CrossModalFusionEngine()

    def test_asr_only_speech_is_included(self):
        result = self.engine.fuse(
            OCRFusedTranscript((), source_video_id="video-1"),
            asr_result((asr_segment("没有字幕的口播"),)),
        )

        self.assertEqual(result.clean_raw_transcript, "没有字幕的口播")
        self.assertEqual(result.segments[0].fusion_status, "ASR_ONLY_SEGMENT")
        self.assertTrue(result.segments[0].included_in_clean_transcript)

    def test_ocr_only_title_and_unknown_are_excluded(self):
        for role in ("screen_title", "unknown"):
            with self.subTest(role=role):
                result = self.engine.fuse(
                    ocr_transcript("澳洲留学专题", role=role),
                    asr_result((), status="ASR_NO_PROVIDER_RESULT"),
                )
                self.assertEqual(result.clean_raw_transcript, "")
                self.assertFalse(result.segments[0].included_in_clean_transcript)
                self.assertEqual(result.segments[0].fusion_status, "OCR_ONLY_TEXT")

    def test_strong_spoken_ocr_can_be_included_only_when_asr_unavailable(self):
        result = self.engine.fuse(
            ocr_transcript("无音轨字幕", role="spoken_subtitle"),
            asr_result((), status="ASR_NO_PROVIDER_RESULT"),
        )

        self.assertEqual(result.clean_raw_transcript, "无音轨字幕")
        self.assertEqual(result.segments[0].fusion_status, "OCR_ONLY_SPOKEN_SUBTITLE")

    def test_clean_transcript_is_ordered_and_deduplicates_overlapping_exact_asr(self):
        result = self.engine.fuse(
            OCRFusedTranscript((), source_video_id="video-1"),
            asr_result((
                asr_segment("第一句", 0, 1000),
                asr_segment("第一句", 800, 1500),
                asr_segment("第二句", 1600, 2400),
            )),
        )

        self.assertEqual(result.clean_raw_transcript, "第一句\n第二句")
        self.assertEqual(len(result.included_segment_ids), 2)

    def test_source_mismatch_fails_closed_without_text_comparison(self):
        result = self.engine.fuse(
            ocr_transcript("EAL", source_video_id="video-A"),
            asr_result((asr_segment("ESL"),), source_video_id="video-B"),
        )

        self.assertEqual(result.fusion_status, "CROSS_MODAL_SOURCE_MISMATCH")
        self.assertEqual(result.clean_raw_transcript, "")
        self.assertEqual(result.segments, ())
        self.assertFalse(result.final_truth)

    def test_missing_both_modalities_returns_isolated_empty_result(self):
        result = self.engine.fuse(None, None)

        self.assertEqual(result.fusion_status, "CROSS_MODAL_NO_INPUT")
        self.assertEqual(result.clean_raw_transcript, "")
        self.assertFalse(result.final_truth)


class CrossModalSyntheticFixtureTests(unittest.TestCase):
    def test_fixture_is_rule_contract_not_ground_truth_or_provider_benchmark(self):
        path = (
            Path(__file__).parent
            / "fixtures/content_extraction/fusion/cross_modal_synthetic.json"
        )
        fixture = json.loads(path.read_text(encoding="utf-8"))

        self.assertFalse(fixture["is_ground_truth"])
        self.assertTrue(fixture["is_synthetic_alignment_fixture"])
        self.assertFalse(fixture["provider_execution_performed"])
        self.assertTrue(
            fixture["expected_behavior_is_rule_contract_not_transcript_truth"]
        )
        self.assertEqual(fixture["source_identity"], "synthetic-cross-modal-phase4")
        expected = {
            "exact_consensus",
            "eal_esl_strong_ocr",
            "eal_esl_weak_ocr",
            "gpa_ocr_resolves",
            "gps_ocr_unresolved",
            "education_chinese_corroboration",
            "graduation_subtitle_resolution",
            "subtitle_compression",
            "screen_title_excluded",
            "asr_only_included",
            "ocr_unknown_excluded",
            "one_ocr_multiple_asr",
            "source_mismatch",
        }
        self.assertEqual({item["id"] for item in fixture["cases"]}, expected)
        self.assertTrue(all(
            item["expected"]["final_truth"] is False
            for item in fixture["cases"]
        ))


if __name__ == "__main__":
    unittest.main()
