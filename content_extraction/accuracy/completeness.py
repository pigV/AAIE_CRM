"""Complete-carrier selection and preservation checks."""

from ..shared.recognition_text import normalize_for_comparison, similarity_signals
from .schemas import CarrierSelection, GuardDecision


def select_complete_carrier(segment):
    baseline = segment.clean_text
    asr = segment.asr_text
    supporting = tuple(
        item for item in segment.provider_evidence
        if item.modality == "asr" and normalize_for_comparison(item.text) == normalize_for_comparison(asr)
    )
    clean_normalized = normalize_for_comparison(baseline)
    asr_normalized = normalize_for_comparison(asr)
    aligned = bool(clean_normalized and clean_normalized in asr_normalized)
    relation_allows = segment.content_relation in {"subtitle_compression", "ocr_subset", "partial_overlap"}
    if asr and len({item.provider for item in supporting}) >= 2 and (aligned or relation_allows):
        score = similarity_signals(baseline, asr).ratio
        return CarrierSelection(
            segment.segment_id, asr, "asr_fused",
            tuple(item.evidence_ref for item in supporting if item.evidence_ref),
            score, selection_reason="corroborated aligned ASR preserves complete utterance",
            confidence_level="HIGH",
        )
    return CarrierSelection(
        segment.segment_id, baseline, "clean_fused", alignment_score=1.0,
        selection_reason="retained baseline; longer carrier not independently corroborated",
        confidence_level=segment.confidence_level,
        unresolved_conflicts=(("unverified_longer_asr_carrier",) if asr and len(asr) > len(baseline) else ()),
    )


class CompletenessGuard:
    def evaluate(self, candidate, carrier, evidence):
        del evidence
        if carrier.carrier_text[candidate.start_offset:candidate.end_offset] != candidate.before_span:
            return GuardDecision("completeness", "REJECT", ("carrier_span_mismatch",))
        return GuardDecision("completeness", "PASS", ("outside_span_preserved",))


def validate_corrected_segment(carrier, corrected_text, accepted_candidates):
    from .minimal_edit import apply_non_overlapping_candidates
    try:
        expected = apply_non_overlapping_candidates(carrier.carrier_text, accepted_candidates)
    except ValueError as exc:
        return GuardDecision("completeness", "REJECT", (str(exc),))
    if corrected_text != expected:
        return GuardDecision("completeness", "REJECT", ("outside_span_modified",))
    return GuardDecision("completeness", "PASS", ("carrier_completeness_preserved",))
