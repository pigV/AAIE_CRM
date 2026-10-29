import json
import unittest
from pathlib import Path

from content_extraction.asr.alignment import align_text_to_segments
from content_extraction.asr.fusion import ASRFusionEngine
from content_extraction.asr.glossary import DomainGlossary
from content_extraction.models import (
    ASRAlignmentMetrics,
    ASRSegment,
    ASRFusedSegment,
    ASRFusionConflict,
    ASRFusionResult,
    ASRResult,
)
from content_extraction.ocr.normalization import (
    normalize_for_comparison as ocr_normalize,
)
from content_extraction.shared.critical_tokens import extract_critical_tokens
from content_extraction.shared.recognition_text import (
    normalize_for_comparison,
    traceable_normalize,
)


def asr_result(
    provider,
    text,
    *,
    segments=(),
    status="completed",
    duration_ms=2000,
    language="zh",
    error_code="",
):
    return ASRResult(
        provider=provider,
        status=status,
        full_text=text,
        segments=tuple(segments),
        words=(),
        language=language,
        duration_ms=duration_ms,
        request_id=f"{provider}-fixture",
        request_id_source="local",
        engine_version="fixture-engine",
        model_name="fixture-model",
        model_version="fixture-version",
        model_hash="fixture-model-hash",
        billing_type="local",
        cost=0,
        raw_response={"source": "fixture"},
        error_code=error_code,
        error_message="provider failed" if error_code else "",
        source_video_id="",
        source_audio_id="fixture.wav",
        audio_hash="fixture-audio-hash",
        timestamp_granularity="segment" if segments else "none",
        timestamp_quality="native" if segments else "none",
        capabilities={
            "vad": False,
            "punctuation": "native" if provider == "funasr" else "absent",
            "confidence": False,
            "word_timestamps": False,
        },
        latency_ms=None,
        confidence=None,
    )


def accepted_smoke_fixture():
    fixture = json.loads((
        Path(__file__).parent
        / "fixtures/content_extraction/providers/asr/funasr_whisper_smoke.json"
    ).read_text(encoding="utf-8"))
    providers = fixture["providers"]
    funasr = asr_result(
        "funasr",
        providers["funasr"]["raw_transcript"],
        duration_ms=fixture["audio"]["duration_ms"],
    )
    whisper = asr_result(
        "whispercpp",
        providers["whispercpp"]["raw_transcript"],
        segments=tuple(
            ASRSegment(item["start_ms"], item["end_ms"], item["text"])
            for item in providers["whispercpp"]["segments"]
        ),
        duration_ms=fixture["audio"]["duration_ms"],
    )
    return fixture, funasr, whisper


class SharedRecognitionTests(unittest.TestCase):
    def test_shared_normalization_is_traceable_and_ocr_exports_stay_compatible(self):
        traced = traceable_normalize("Ｅ Ａ Ｌ，十岁")

        self.assertEqual(traced.normalized, "eal10岁")
        self.assertEqual(normalize_for_comparison("Ｅ Ａ Ｌ，十岁"), "eal10岁")
        self.assertEqual(ocr_normalize("Ｅ Ａ Ｌ，十岁"), "eal10岁")
        self.assertEqual(len(traced.trace), len(traced.normalized))
        self.assertTrue(all(item.raw_start <= item.raw_end for item in traced.trace))

    def test_shared_critical_tokens_include_esl_gpa_and_preserve_ocr_categories(self):
        values = {
            (item.token_type, item.value)
            for item in extract_critical_tokens("ESL GPA 500签证")
        }

        self.assertIn(("ASCII_ACRONYM", "ESL"), values)
        self.assertIn(("ASCII_ACRONYM", "GPA"), values)
        self.assertIn(("VISA_SUBCLASS", "500"), values)


class ASRFusionSchemaTests(unittest.TestCase):
    def test_serialization_preserves_provenance_conflicts_and_empty_words(self):
        metrics = ASRAlignmentMetrics(
            alignment_score=0.8,
            matched_chars=8,
            substituted_chars=1,
            inserted_chars=0,
            deleted_chars=1,
            unmatched_chars=2,
            coverage_ratio=0.8,
            char_match_ratio=0.8,
            boundary_anchor_score=0.5,
            quality_level="MEDIUM",
            warnings=("boundary_ambiguous",),
        )
        conflict = ASRFusionConflict(
            conflict_type="ASR_CRITICAL_TOKEN_CONFLICT",
            severity="HIGH",
            category="ASCII_ACRONYM",
            provider_values={"funasr": ("GPS",), "whispercpp": ("GPA",)},
            alternatives=("GPS", "GPA"),
            selected_value="GPA",
            selection_reason="provider conflict remains",
            requires_cross_modal_evidence=True,
        )
        segment = ASRFusedSegment(
            start_ms=25280,
            end_ms=27000,
            raw_start_ms=25280,
            raw_end_ms=27280,
            fused_text="开始拼选课 拼GPA",
            text_source="whispercpp",
            timestamp_source="whispercpp",
            timestamp_quality="native",
            timestamp_adjustment="duration_clamp",
            provider_texts={"funasr": "开始拼选课，拼GPS。", "whispercpp": "开始拼选课 拼GPA"},
            alternatives=("开始拼选课，拼GPS。",),
            conflicts=(conflict,),
            critical_tokens=extract_critical_tokens("GPA"),
            selection_reason="Whisper candidate; provider conflict remains",
            alignment_score=0.8,
            alignment_metrics=metrics,
            fusion_status="ASR_CRITICAL_TOKEN_CONFLICT",
            confidence_level="LOW",
            asr_consensus=False,
            final_truth=False,
            domain_candidates=("GPA",),
            raw_metadata={"fixture": "real_smoke"},
        )
        result = ASRFusionResult(
            full_text=segment.fused_text,
            segments=(segment,),
            words=(),
            providers_used=("funasr", "whispercpp"),
            provider_results=(),
            language="zh",
            duration_ms=27000,
            timestamp_granularity="segment",
            fusion_status="ASR_CRITICAL_TOKEN_CONFLICT",
            confidence_level="LOW",
            asr_consensus=False,
            final_truth=False,
            raw_metadata={"final_truth": False},
        )

        serialized = result.to_dict()

        self.assertEqual(serialized["words"], [])
        self.assertEqual(serialized["segments"][0]["raw_end_ms"], 27280)
        self.assertEqual(serialized["segments"][0]["end_ms"], 27000)
        self.assertEqual(
            serialized["segments"][0]["timestamp_adjustment"],
            "duration_clamp",
        )
        self.assertEqual(
            serialized["segments"][0]["conflicts"][0]["alternatives"],
            ["GPS", "GPA"],
        )
        self.assertEqual(serialized["confidence_level"], "LOW")
        self.assertFalse(serialized["asr_consensus"])
        self.assertFalse(serialized["final_truth"])
        self.assertNotIn("confidence", serialized)


class MonotonicAlignmentTests(unittest.TestCase):
    def test_exact_alignment_maps_forward_and_reports_literal_metrics(self):
        result = align_text_to_segments(
            "甲乙，丙丁。",
            (ASRSegment(0, 1000, "甲乙"), ASRSegment(1000, 2000, "丙丁")),
        )

        mapped = tuple(
            index for index in result.funasr_to_whisper_segment
            if index is not None
        )
        self.assertEqual(result.funasr_normalized, "甲乙丙丁")
        self.assertEqual(result.funasr_to_whisper_segment, (0, 0, 1, 1))
        self.assertEqual(mapped, tuple(sorted(mapped)))
        self.assertEqual(result.metrics.matched_chars, 4)
        self.assertEqual(result.metrics.unmatched_chars, 0)
        self.assertEqual(result.metrics.coverage_ratio, 1.0)
        self.assertEqual(result.metrics.quality_level, "HIGH")
        self.assertEqual(
            tuple(item.funasr_text for item in result.segments),
            ("甲乙", "丙丁"),
        )

    def test_cross_segment_insertion_is_unmapped_instead_of_time_allocated(self):
        result = align_text_to_segments(
            "甲乙新增丙丁",
            (ASRSegment(0, 1000, "甲乙"), ASRSegment(1000, 2000, "丙丁")),
        )

        self.assertEqual(
            result.funasr_to_whisper_segment,
            (0, 0, None, None, 1, 1),
        )
        self.assertIn("CROSS_SEGMENT_INSERTION", result.metrics.warnings)
        self.assertNotEqual(result.metrics.quality_level, "HIGH")

    def test_similar_alignment_exposes_edit_counts_and_quality_formula(self):
        result = align_text_to_segments(
            "澳洲低龄留学规甲",
            (ASRSegment(0, 1000, "澳洲低龄留学规划"),),
        )

        self.assertEqual(result.metrics.matched_chars, 7)
        self.assertEqual(result.metrics.substituted_chars, 1)
        self.assertEqual(result.metrics.unmatched_chars, 1)
        self.assertEqual(result.metrics.coverage_ratio, 1.0)
        self.assertAlmostEqual(result.metrics.char_match_ratio, 0.875)
        self.assertAlmostEqual(result.metrics.alignment_score, 0.925)
        self.assertEqual(result.metrics.quality_level, "HIGH")

    def test_repeated_text_ambiguity_forces_low_quality_without_backward_mapping(self):
        result = align_text_to_segments(
            "很多家长很多家长",
            (
                ASRSegment(0, 1000, "很多家长需要规划"),
                ASRSegment(1000, 2000, "很多家长也要准备"),
            ),
        )

        mapped = tuple(
            index for index in result.funasr_to_whisper_segment
            if index is not None
        )
        self.assertEqual(mapped, tuple(sorted(mapped)))
        self.assertIn("AMBIGUOUS_REPEATED_TEXT", result.metrics.warnings)
        self.assertEqual(result.metrics.quality_level, "LOW")
        self.assertLess(result.metrics.boundary_anchor_score, 1.0)


class DomainGlossaryTests(unittest.TestCase):
    def test_edit_distance_one_generates_domain_candidates_without_rewriting_input(self):
        glossary = DomainGlossary.default()
        raw = "ESL"

        candidates = glossary.generate_candidates(raw, context="英语课程")

        self.assertEqual(raw, "ESL")
        self.assertIn("ESL", tuple(item.value for item in candidates))
        self.assertIn("EAL", tuple(item.value for item in candidates))
        self.assertTrue(all(item.source_value == "ESL" for item in candidates))

    def test_gpa_wins_glossary_supported_tie_but_gps_alternative_remains(self):
        glossary = DomainGlossary.default()
        provider_values = {"funasr": ("GPS",), "whispercpp": ("GPA",)}

        scores = glossary.score_candidates(provider_values, context="教育选课")

        self.assertEqual(scores[0].value, "GPA")
        self.assertGreater(scores[0].score, scores[1].score)
        self.assertEqual({item.value for item in scores}, {"GPS", "GPA"})
        self.assertEqual(
            provider_values,
            {"funasr": ("GPS",), "whispercpp": ("GPA",)},
        )
        self.assertIn("domain_glossary_exact", scores[0].reasons)
        self.assertIn("provider_exact:whispercpp", scores[0].reasons)


class ASRFusionOrchestrationTests(unittest.TestCase):
    def test_exact_agreement_keeps_native_whisper_segment_and_no_fake_confidence(self):
        funasr = asr_result("funasr", "你好世界")
        whisper = asr_result(
            "whispercpp",
            "你好世界",
            segments=(ASRSegment(0, 2000, "你好世界"),),
        )

        fused = ASRFusionEngine().fuse(funasr, whisper)

        self.assertEqual(fused.fusion_status, "ASR_CONSENSUS_EXACT")
        self.assertEqual(fused.confidence_level, "HIGH")
        self.assertTrue(fused.asr_consensus)
        self.assertFalse(fused.final_truth)
        self.assertEqual(fused.words, ())
        self.assertEqual(fused.segments[0].text_source, "whispercpp")
        self.assertEqual(fused.segments[0].timestamp_source, "whispercpp")
        self.assertEqual(fused.segments[0].timestamp_quality, "native")
        self.assertIsNone(fused.segments[0].timestamp_adjustment)
        self.assertNotIn("confidence", fused.to_dict())

    def test_longer_aligned_funasr_candidate_uses_whisper_time_as_aligned(self):
        funasr = asr_result("funasr", "澳洲低龄留学完整规划")
        whisper = asr_result(
            "whispercpp",
            "澳洲低龄留学规划",
            segments=(ASRSegment(0, 2000, "澳洲低龄留学规划"),),
        )

        fused = ASRFusionEngine().fuse(funasr, whisper)

        self.assertEqual(fused.full_text, "澳洲低龄留学完整规划")
        self.assertEqual(fused.segments[0].text_source, "funasr")
        self.assertEqual(fused.segments[0].timestamp_source, "whispercpp")
        self.assertEqual(fused.segments[0].timestamp_quality, "aligned")
        self.assertIn("aligned lexical completeness", fused.segments[0].selection_reason)

    def test_low_alignment_keeps_whisper_and_preserves_funasr_alternative(self):
        funasr = asr_result("funasr", "完全不同内容")
        whisper = asr_result(
            "whispercpp",
            "澳洲留学规划",
            segments=(ASRSegment(0, 2000, "澳洲留学规划"),),
        )

        fused = ASRFusionEngine().fuse(funasr, whisper)

        segment = fused.segments[0]
        self.assertEqual(segment.fused_text, "澳洲留学规划")
        self.assertEqual(segment.timestamp_quality, "native")
        self.assertIn("完全不同内容", segment.alternatives)
        self.assertEqual(
            segment.selection_reason,
            "low_alignment_quality_fallback_to_whisper",
        )
        self.assertEqual(segment.confidence_level, "LOW")

    def test_high_alignment_projects_internal_and_terminal_funasr_punctuation(self):
        funasr = asr_result("funasr", "你好，世界。")
        whisper = asr_result(
            "whispercpp",
            "你好世界",
            segments=(ASRSegment(0, 2000, "你好世界"),),
        )

        fused = ASRFusionEngine().fuse(funasr, whisper)

        segment = fused.segments[0]
        self.assertEqual(segment.fused_text, "你好，世界。")
        self.assertEqual(segment.text_source, "fused")
        self.assertEqual(segment.timestamp_quality, "aligned")
        self.assertEqual(segment.fusion_status, "ASR_CONSENSUS_SIMILAR")
        self.assertIn("native punctuation", segment.selection_reason)

    def test_punctuation_between_whisper_segments_is_rejected_as_ambiguous(self):
        funasr = asr_result("funasr", "甲乙，丙丁")
        whisper = asr_result(
            "whispercpp",
            "甲乙丙丁",
            segments=(ASRSegment(0, 1000, "甲乙"), ASRSegment(1000, 2000, "丙丁")),
        )

        fused = ASRFusionEngine().fuse(funasr, whisper)

        self.assertEqual(fused.full_text, "甲乙丙丁")
        self.assertNotIn("，", fused.segments[0].fused_text)
        self.assertNotIn("，", fused.segments[1].fused_text)

    def test_gpa_gps_is_critical_conflict_with_glossary_selection_and_no_punctuation(self):
        funasr = asr_result("funasr", "开始拼选课，拼GPS。")
        whisper = asr_result(
            "whispercpp",
            "开始拼选课 拼GPA",
            segments=(ASRSegment(0, 2000, "开始拼选课 拼GPA"),),
        )

        fused = ASRFusionEngine().fuse(funasr, whisper)

        segment = fused.segments[0]
        self.assertEqual(segment.fusion_status, "ASR_CRITICAL_TOKEN_CONFLICT")
        self.assertEqual(segment.confidence_level, "LOW")
        self.assertEqual(segment.fused_text, "开始拼选课 拼GPA")
        self.assertNotIn("，", segment.fused_text)
        self.assertNotIn("。", segment.fused_text)
        self.assertEqual(segment.conflicts[0].conflict_type, "ASR_CRITICAL_TOKEN_CONFLICT")
        self.assertEqual(segment.conflicts[0].alternatives, ("GPS", "GPA"))
        self.assertEqual(segment.conflicts[0].selected_value, "GPA")
        self.assertIn("Whisper candidate", segment.selection_reason)
        self.assertIn("domain glossary support", segment.selection_reason)
        self.assertIn("provider conflict remains", segment.selection_reason)
        self.assertIn("final_truth=false", segment.selection_reason)
        self.assertFalse(segment.final_truth)

    def test_number_and_visa_subclass_differences_are_critical(self):
        funasr = asr_result("funasr", "申请500签证")
        whisper = asr_result(
            "whispercpp",
            "申请590签证",
            segments=(ASRSegment(0, 2000, "申请590签证"),),
        )

        fused = ASRFusionEngine().fuse(funasr, whisper)

        categories = {item.category for item in fused.segments[0].conflicts}
        self.assertIn("NUMBER", categories)
        self.assertIn("VISA_SUBCLASS", categories)
        self.assertEqual(fused.fusion_status, "ASR_CRITICAL_TOKEN_CONFLICT")

    def test_esl_consensus_is_not_truth_and_retains_eal_domain_candidate(self):
        funasr = asr_result("funasr", "ESL课")
        whisper = asr_result(
            "whispercpp",
            "ESL课",
            segments=(ASRSegment(0, 2000, "ESL课"),),
        )

        fused = ASRFusionEngine().fuse(funasr, whisper)

        segment = fused.segments[0]
        self.assertEqual(segment.fused_text, "ESL课")
        self.assertTrue(segment.asr_consensus)
        self.assertFalse(segment.final_truth)
        self.assertIn("ESL", segment.domain_candidates)
        self.assertIn("EAL", segment.domain_candidates)
        self.assertEqual(segment.conflicts, ())

    def test_whisper_only_returns_native_timed_single_provider(self):
        funasr = asr_result(
            "funasr", "", status="failed", error_code="FUNASR_FAILED"
        )
        whisper = asr_result(
            "whispercpp",
            "唯一结果",
            segments=(ASRSegment(0, 2000, "唯一结果"),),
        )

        fused = ASRFusionEngine().fuse(funasr, whisper)

        self.assertEqual(fused.fusion_status, "ASR_SINGLE_PROVIDER")
        self.assertEqual(fused.confidence_level, "LOW")
        self.assertEqual(fused.full_text, "唯一结果")
        self.assertEqual(fused.segments[0].timestamp_quality, "native")
        self.assertEqual(fused.providers_used, ("whispercpp",))

    def test_funasr_only_returns_untimed_full_text_without_fake_segment(self):
        funasr = asr_result("funasr", "只有全文")
        whisper = asr_result(
            "whispercpp", "", status="failed", error_code="WHISPER_FAILED"
        )

        fused = ASRFusionEngine().fuse(funasr, whisper)

        self.assertEqual(fused.fusion_status, "ASR_SINGLE_PROVIDER")
        self.assertEqual(fused.full_text, "只有全文")
        self.assertEqual(fused.segments, ())
        self.assertEqual(fused.words, ())
        self.assertEqual(fused.timestamp_granularity, "none")
        self.assertEqual(fused.providers_used, ("funasr",))

    def test_both_fail_returns_non_throwing_traceable_no_provider_result(self):
        funasr = asr_result(
            "funasr", "", status="failed", error_code="FUNASR_FAILED"
        )
        whisper = asr_result(
            "whispercpp", "", status="failed", error_code="WHISPER_FAILED"
        )

        fused = ASRFusionEngine().fuse(funasr, whisper)

        self.assertEqual(fused.fusion_status, "ASR_NO_PROVIDER_RESULT")
        self.assertEqual(fused.full_text, "")
        self.assertEqual(fused.segments, ())
        self.assertEqual(fused.confidence_level, "LOW")
        self.assertEqual(
            fused.raw_metadata["provider_errors"],
            {"funasr": "FUNASR_FAILED", "whispercpp": "WHISPER_FAILED"},
        )


class RealSmokeFusionRegressionTests(unittest.TestCase):
    def test_fixture_is_historical_provider_evidence_not_ground_truth(self):
        fixture, funasr, whisper = accepted_smoke_fixture()

        self.assertFalse(fixture["is_ground_truth"])
        self.assertTrue(fixture["created_without_provider_execution"])
        self.assertEqual(fixture["ground_truth"], None)
        self.assertIn("多孩子", funasr.full_text)
        self.assertIn("配不了业", funasr.full_text)
        self.assertIn("教务", funasr.full_text)
        self.assertEqual(len(whisper.segments), 14)
        self.assertEqual(whisper.segments[-1].end_ms, 27280)

    def test_real_fixture_preserves_providers_and_clamps_only_rendered_end(self):
        fixture, funasr, whisper = accepted_smoke_fixture()
        original_funasr = funasr.full_text
        original_whisper = whisper.full_text

        fused = ASRFusionEngine().fuse(funasr, whisper)

        self.assertEqual(len(fused.segments), 14)
        self.assertEqual(fused.segments[-1].raw_end_ms, 27280)
        self.assertEqual(fused.segments[-1].end_ms, 27000)
        self.assertEqual(
            fused.segments[-1].timestamp_adjustment,
            "duration_clamp",
        )
        self.assertTrue(all(
            item.timestamp_source == "whispercpp" for item in fused.segments
        ))
        self.assertEqual(fused.timestamp_granularity, "segment")
        self.assertEqual(fused.words, ())
        self.assertFalse(fused.final_truth)
        self.assertEqual(funasr.full_text, original_funasr)
        self.assertEqual(whisper.full_text, original_whisper)
        self.assertEqual(
            fused.provider_results[0].full_text,
            fixture["providers"]["funasr"]["raw_transcript"],
        )
        self.assertEqual(
            fused.provider_results[1].full_text,
            fixture["providers"]["whispercpp"]["raw_transcript"],
        )

    def test_real_fixture_keeps_esl_consensus_and_gpa_gps_conflict(self):
        _, funasr, whisper = accepted_smoke_fixture()

        fused = ASRFusionEngine().fuse(funasr, whisper)

        esl_segment = next(item for item in fused.segments if "ESL" in item.fused_text)
        gpa_segment = next(item for item in fused.segments if "GPA" in item.fused_text)
        self.assertTrue(esl_segment.asr_consensus)
        self.assertFalse(esl_segment.final_truth)
        self.assertIn("EAL", esl_segment.domain_candidates)
        self.assertIn("ESL", esl_segment.domain_candidates)
        self.assertEqual(gpa_segment.fusion_status, "ASR_CRITICAL_TOKEN_CONFLICT")
        acronym_conflict = next(
            item for item in gpa_segment.conflicts
            if item.category == "ASCII_ACRONYM"
        )
        self.assertEqual(acronym_conflict.alternatives, ("GPS", "GPA"))
        self.assertEqual(acronym_conflict.selected_value, "GPA")
        self.assertIn("provider conflict remains", gpa_segment.selection_reason)
        self.assertFalse(gpa_segment.final_truth)


class ASRPublicContractTests(unittest.TestCase):
    def test_asr_package_exports_fusion_alignment_and_glossary_contracts(self):
        from content_extraction import asr

        self.assertIs(asr.ASRFusionEngine, ASRFusionEngine)
        self.assertIs(asr.DomainGlossary, DomainGlossary)
        self.assertIs(asr.align_text_to_segments, align_text_to_segments)

    def test_zero_duration_after_clamp_is_omitted_but_raw_segment_is_traceable(self):
        funasr = asr_result("funasr", "越界文字", duration_ms=2000)
        whisper = asr_result(
            "whispercpp",
            "越界文字",
            segments=(ASRSegment(3000, 4000, "越界文字"),),
            duration_ms=2000,
        )

        fused = ASRFusionEngine().fuse(funasr, whisper)

        self.assertEqual(fused.full_text, "越界文字")
        self.assertEqual(fused.segments, ())
        self.assertEqual(
            fused.raw_metadata["omitted_zero_duration_segments"][0]["raw_start_ms"],
            3000,
        )
        self.assertEqual(
            fused.raw_metadata["omitted_zero_duration_segments"][0]["raw_end_ms"],
            4000,
        )
        self.assertEqual(
            fused.raw_metadata["warnings"],
            ["ZERO_DURATION_SEGMENT_OMITTED"],
        )


if __name__ == "__main__":
    unittest.main()
