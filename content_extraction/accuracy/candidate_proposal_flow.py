"""Projection and unchanged Guard evaluation for verified proposals."""

from dataclasses import dataclass, replace
import hashlib

from .candidate_projection import project_candidate_spans
from .completeness import CompletenessGuard, validate_corrected_segment
from .critical_guard import CriticalTokenGuard
from .evidence_guard import EvidenceGuard
from .minimal_edit import MinimalEditGuard, apply_non_overlapping_candidates
from .schemas import CorrectionCandidate, GuardDecision
from .spark_queue_benchmark import _carrier, _evidence_segment
from ..shared.critical_tokens import extract_critical_tokens
from ..shared.recognition_text import levenshtein_distance


@dataclass(frozen=True)
class CandidateProposalDecision:
    review_id: str
    status: str
    source_text: str
    final_text: str
    guard_decisions: tuple[GuardDecision, ...] = ()
    reasons: tuple[str, ...] = ()

    def to_dict(self):
        return {
            "review_id": self.review_id,
            "status": self.status,
            "source_text": self.source_text,
            "final_text": self.final_text,
            "guard_decisions": [value.to_dict() for value in self.guard_decisions],
            "reasons": list(self.reasons),
        }


def _keep(review, reasons, decisions=()):
    return CandidateProposalDecision(
        review.review_id, "KEEP_REVIEW", review.carrier_text,
        review.carrier_text, tuple(decisions), tuple(reasons),
    )


def _candidate(review, proposal, qwen_result, span, projection_index):
    start = review.start_offset + span.start_offset
    end = review.start_offset + span.end_offset
    digest = hashlib.sha256(
        f"{review.segment_id}|{start}|{end}|{span.after_span}|candidate-proposer-v1".encode("utf-8")
    ).hexdigest()[:16]
    critical = bool(
        review.critical_token_type
        or extract_critical_tokens(span.before_span)
        or extract_critical_tokens(span.after_span)
    )
    return CorrectionCandidate(
        candidate_id=f"candidate-proposer:{digest}",
        segment_id=review.segment_id,
        carrier_fingerprint=_carrier(review).fingerprint,
        before_span=span.before_span,
        after_span=span.after_span,
        start_offset=start,
        end_offset=end,
        correction_type="ASR_PHONETIC_CORRECTION",
        proposal_source="external_llm",
        alternatives=(review.suspect_span, proposal),
        evidence_refs=("spark_lite:candidate_proposal", "qwen3_8b:local_verification"),
        evidence_strength="LEVEL_C",
        generation_reasons=("spark_proposed_qwen_verified_candidate",),
        selection_reason="Qwen3-8B selected B from fixed A/B/X map",
        confidence_level="HIGH",
        affects_critical_token=critical,
        lexical_edit_distance=levenshtein_distance(span.before_span, span.after_span),
        llm_metadata={
            "spark_prompt_version": "spark-lite-candidate-proposer-v1",
            "qwen_prompt_version": qwen_result.prompt_version,
            "selection_only": True,
            "projection_index": projection_index,
        },
    )


def evaluate_verified_proposal(review, proposal_text, qwen_result):
    if not qwen_result.choice_valid or qwen_result.status != "completed":
        return _keep(review, (qwen_result.error_code or "qwen_verification_invalid",))
    if qwen_result.selected_option != "B":
        return _keep(review, ("qwen_did_not_select_proposal",))
    if qwen_result.selected_text != proposal_text:
        return _keep(review, ("qwen_candidate_mapping_mismatch",))

    projection = project_candidate_spans(review.suspect_span, proposal_text)
    if projection.status != "PROJECTED" or not projection.substantive_spans:
        return _keep(review, ("projection_unsafe", *projection.reasons))

    candidates = tuple(
        _candidate(review, proposal_text, qwen_result, span, index)
        for index, span in enumerate(projection.substantive_spans)
    )
    carrier = _carrier(review)
    evidence = _evidence_segment(review)
    decisions = []
    for index, candidate in enumerate(candidates):
        for guard in (EvidenceGuard(), CriticalTokenGuard(), MinimalEditGuard(), CompletenessGuard()):
            decision = guard.evaluate(candidate, carrier, evidence)
            decision = replace(
                decision,
                metrics={**decision.metrics, "projection_index": index},
            )
            decisions.append(decision)
            if decision.status != "PASS":
                return _keep(
                    review,
                    tuple(reason for value in decisions for reason in value.reasons),
                    decisions,
                )
    try:
        final_text = apply_non_overlapping_candidates(review.carrier_text, candidates)
    except ValueError as exc:
        return _keep(review, (str(exc),), decisions)
    final_check = validate_corrected_segment(carrier, final_text, candidates)
    if final_check.status != "PASS":
        return _keep(review, final_check.reasons, (*decisions, final_check))
    return CandidateProposalDecision(
        review.review_id, "AUTO_RESOLVED", review.carrier_text,
        final_text, tuple(decisions),
        ("spark_proposal_qwen_verified_all_guards_passed",),
    )
