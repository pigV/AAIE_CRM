"""Candidate mapping and existing-Guard resolution for Spark arbitration."""

from dataclasses import dataclass, replace
import hashlib
import json

from .candidate_projection import orthographic_key, project_candidate_spans
from .completeness import CompletenessGuard, validate_corrected_segment
from .critical_guard import CriticalTokenGuard
from .evidence_guard import EvidenceGuard
from .external.arbiter_schemas import SparkArbitrationCandidate
from .external.arbiter_schemas import SparkArbitrationResult
from .minimal_edit import MinimalEditGuard, apply_non_overlapping_candidates
from .schemas import CorrectionCandidate, GuardDecision
from .spark_queue_benchmark import _carrier, _evidence_segment
from ..shared.critical_tokens import extract_critical_tokens
from ..shared.recognition_text import levenshtein_distance


_OPTION_LABELS = tuple(value for value in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if value != "X")
_NUMERIC_TYPES = {"AGE", "DATE", "MONEY", "NUMBER", "PERCENTAGE", "VISA_CLASS", "YEAR"}
_ALLOWED_CANDIDATE_SOURCES = {
    "apple_vision", "asr_fusion", "cross_modal", "crossmodal", "funasr",
    "glossary", "ocr_fusion", "paddle", "qwen3_8b", "whispercpp",
}


@dataclass(frozen=True)
class ReviewArbitrationClassification:
    category: str
    candidates: tuple[SparkArbitrationCandidate, ...]
    raw_candidate_count: int
    substantive_group_count: int
    safe_candidate_count: int
    critical_token_item: bool
    reasons: tuple[str, ...] = ()

    def to_dict(self):
        return {
            "category": self.category,
            "candidates": [value.to_dict() for value in self.candidates],
            "raw_candidate_count": self.raw_candidate_count,
            "substantive_group_count": self.substantive_group_count,
            "safe_candidate_count": self.safe_candidate_count,
            "critical_token_item": self.critical_token_item,
            "reasons": list(self.reasons),
        }


def _localized_alternative(review, text):
    value = str(text or "")
    if not value:
        return ""
    if value == review.carrier_text:
        return review.suspect_span
    prefix = review.carrier_text[:review.start_offset]
    suffix = review.carrier_text[review.end_offset:]
    if (not prefix or value.startswith(prefix)) and (not suffix or value.endswith(suffix)):
        start = len(prefix)
        end = len(value) - len(suffix) if suffix else len(value)
        localized = value[start:end]
        return localized if localized else ""
    if len(value) <= 12:
        return value
    return ""


def map_review_candidates(review):
    ordered = []
    positions = {}

    def add(text, sources, reason):
        localized = _localized_alternative(review, text)
        provenance = tuple(
            str(value) for value in sources
            if str(value or "") in _ALLOWED_CANDIDATE_SOURCES
        )
        if not localized or not provenance:
            return
        if localized in positions:
            index = positions[localized]
            existing = ordered[index]
            combined = tuple(dict.fromkeys((*existing[1], *provenance)))
            ordered[index] = (localized, combined, existing[2])
            return
        if len(ordered) >= len(_OPTION_LABELS):
            return
        positions[localized] = len(ordered)
        ordered.append((localized, provenance, str(reason or "machine_alternative")))

    for alternative in review.alternatives:
        add(alternative.text, alternative.sources, alternative.reason)

    qwen = review.qwen_proposal if isinstance(review.qwen_proposal, dict) else {}
    if (
        qwen.get("proposal_source") == "local_llm"
        and str(qwen.get("before_span") or "") == review.suspect_span
        and isinstance(qwen.get("after_span"), str)
        and qwen.get("after_span")
    ):
        add(qwen["after_span"], ("qwen3_8b",), "valid_local_qwen_proposal")

    return tuple(
        SparkArbitrationCandidate(_OPTION_LABELS[index], text, sources, reason)
        for index, (text, sources, reason) in enumerate(ordered)
    )


def _collapsed_orthographic_candidates(candidates):
    grouped = []
    positions = {}
    for candidate in candidates:
        key = orthographic_key(candidate.text)
        if key in positions:
            index = positions[key]
            existing = grouped[index]
            grouped[index] = SparkArbitrationCandidate(
                existing.option,
                existing.text,
                tuple(dict.fromkeys((*existing.sources, *candidate.sources))),
                existing.reason,
            )
            continue
        positions[key] = len(grouped)
        grouped.append(candidate)
    return tuple(
        SparkArbitrationCandidate(_OPTION_LABELS[index], value.text, value.sources, value.reason)
        for index, value in enumerate(grouped)
    )


def _critical_candidate_authorized(review, candidate, projection):
    if not review.critical_token_type:
        return True
    evidence = _evidence_segment(review)
    numeric_like = str(review.critical_token_type).upper() in _NUMERIC_TYPES
    for span in projection.substantive_spans:
        supporting = tuple(
            value for value in evidence.provider_evidence
            if span.after_span in value.text and value.status == "completed"
        )
        providers = {value.provider for value in supporting}
        modalities = {value.modality for value in supporting}
        if len(providers) < 2:
            return False
        if numeric_like and len(modalities) < 2:
            return False
    return True


def classify_review_for_arbitration(review):
    raw_candidates = map_review_candidates(review)
    critical = bool(review.critical_token_type)
    if len(raw_candidates) < 2:
        return ReviewArbitrationClassification(
            "NO_REAL_ALTERNATIVE", raw_candidates, len(raw_candidates),
            len(raw_candidates), len(raw_candidates), critical,
            ("fewer_than_two_sourced_candidates",),
        )
    collapsed = _collapsed_orthographic_candidates(raw_candidates)
    if len(collapsed) == 1:
        return ReviewArbitrationClassification(
            "NON_SUBSTANTIVE_VARIANT", (), len(raw_candidates), 1, 0,
            critical, ("candidates_differ_only_in_writing_form",),
        )

    safe = []
    unsafe_count = 0
    critical_blocked = False
    for candidate in collapsed:
        projection = project_candidate_spans(review.suspect_span, candidate.text)
        if projection.status == "EXACT":
            safe.append(candidate)
            continue
        if projection.status != "PROJECTED":
            unsafe_count += 1
            continue
        if not _critical_candidate_authorized(review, candidate, projection):
            critical_blocked = True
            continue
        safe.append(candidate)
    safe = tuple(
        SparkArbitrationCandidate(_OPTION_LABELS[index], value.text, value.sources, value.reason)
        for index, value in enumerate(safe)
    )
    if len(safe) >= 2:
        return ReviewArbitrationClassification(
            "SPARK_ELIGIBLE_LEXICAL_CONFLICT", safe, len(raw_candidates),
            len(collapsed), len(safe), critical,
            tuple(filter(None, (
                "unsafe_candidates_excluded" if unsafe_count else "",
            ))),
        )
    reasons = []
    if unsafe_count:
        reasons.append("insufficient_safe_local_projections")
    if critical_blocked:
        reasons.append("critical_token_lacks_independent_authorization")
    return ReviewArbitrationClassification(
        "HUMAN_ONLY", safe, len(raw_candidates), len(collapsed), len(safe),
        critical, tuple(reasons or ("no_safe_arbiter_candidate_pair",)),
    )


def arbitration_fingerprint_payload(review, candidates, queue_hash=""):
    evidence = json.dumps(
        {"ocr": review.ocr_evidence, "asr": review.asr_evidence},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return {
        "model": "lite",
        "arbiter_prompt_version": "spark-lite-arbiter-v1",
        "queue_hash": str(queue_hash or ""),
        "review_id": str(review.review_id),
        "suspect_span": review.suspect_span,
        "context": {
            "previous": review.context_before,
            "carrier": review.carrier_text,
            "next": review.context_after,
        },
        "candidates": [value.to_dict() for value in candidates],
        "evidence_fingerprint": "sha256:" + hashlib.sha256(evidence).hexdigest(),
    }


@dataclass(frozen=True)
class SparkArbiterReviewDecision:
    review_id: str
    segment_id: str
    status: str
    source_text: str
    final_text: str
    arbitration_result: object
    guard_decisions: tuple[GuardDecision, ...] = ()
    reasons: tuple[str, ...] = ()

    def to_dict(self):
        return {
            "review_id": self.review_id,
            "segment_id": self.segment_id,
            "status": self.status,
            "source_text": self.source_text,
            "final_text": self.final_text,
            "arbitration_result": self.arbitration_result.to_dict(),
            "guard_decisions": [value.to_dict() for value in self.guard_decisions],
            "reasons": list(self.reasons),
        }


def _keep(review, result, decisions=(), reasons=()):
    return SparkArbiterReviewDecision(
        review.review_id, review.segment_id, "KEEP_REVIEW",
        review.carrier_text, review.carrier_text, result,
        tuple(decisions), tuple(reasons),
    )


def _resolve_without_change(review, result, reasons=()):
    return SparkArbiterReviewDecision(
        review.review_id, review.segment_id, "AUTO_RESOLVED",
        review.carrier_text, review.carrier_text, result, (), tuple(reasons),
    )


def _correction_type(review, before, after):
    token_type = str(review.critical_token_type or "").upper()
    if token_type in _NUMERIC_TYPES or before.isdigit() or after.isdigit():
        return "NUMBER_CORRECTION"
    if token_type in {"ACRONYM", "ENGLISH_ACRONYM"}:
        return "ACRONYM_CORRECTION"
    if token_type:
        return "PROPER_NOUN_CORRECTION"
    return "ASR_PHONETIC_CORRECTION"


def _guard_candidate(review, result, mapped, span, projection_index):
    before = span.before_span
    after = span.after_span
    start_offset = review.start_offset + span.start_offset
    end_offset = review.start_offset + span.end_offset
    digest = hashlib.sha256(
        f"{review.segment_id}|{start_offset}|{end_offset}|{after}|spark-arbiter-v1".encode("utf-8")
    ).hexdigest()[:16]
    affects_critical = bool(
        review.critical_token_type
        or extract_critical_tokens(before)
        or extract_critical_tokens(after)
    )
    return CorrectionCandidate(
        candidate_id=f"spark-arbiter-candidate:{digest}",
        segment_id=review.segment_id,
        carrier_fingerprint=_carrier(review).fingerprint,
        before_span=before,
        after_span=after,
        start_offset=start_offset,
        end_offset=end_offset,
        correction_type=_correction_type(review, before, after),
        proposal_source="external_llm",
        alternatives=tuple(value.text for value in map_review_candidates(review)),
        evidence_refs=tuple(f"queue:{review.review_id}:{value}" for value in mapped.sources),
        evidence_strength="LEVEL_C",
        generation_reasons=("spark_selected_existing_local_candidate",),
        selection_reason=f"Spark Lite selected option {result.selected_option}",
        confidence_level="HIGH",
        affects_critical_token=affects_critical,
        lexical_edit_distance=levenshtein_distance(before, after),
        llm_metadata={
            "provider": "spark_lite", "model": "lite",
            "arbiter_prompt_version": result.arbiter_prompt_version,
            "selection_only": True,
            "projection_index": projection_index,
        },
    )


def _precheck_skip_result(category, candidates):
    return SparkArbitrationResult(
        status="skipped",
        allowed_options=tuple(value.option for value in candidates) + ("X",),
        free_only_status="NOT_REQUIRED",
        error_code=category,
        error_message="Local candidate precheck skipped Spark Lite",
        request_count=0,
        estimated_cost=0,
    )


def evaluate_arbitrated_review_item(review, arbiter):
    classification = classify_review_for_arbitration(review)
    candidates = classification.candidates
    if classification.category != "SPARK_ELIGIBLE_LEXICAL_CONFLICT":
        result = _precheck_skip_result(classification.category, candidates)
        if classification.category == "NON_SUBSTANTIVE_VARIANT":
            return _resolve_without_change(review, result, classification.reasons)
        return _keep(review, result, reasons=classification.reasons)
    result = arbiter.arbitrate(review, candidates)
    if result.status != "completed" or not result.choice_valid:
        return _keep(review, result, reasons=(result.error_code or "arbiter_unavailable",))
    if result.selected_option == "X":
        return _keep(review, result, reasons=("spark_unable_to_determine",))
    mapped = next((value for value in candidates if value.option == result.selected_option), None)
    if mapped is None or result.selected_text != mapped.text:
        return _keep(review, result, reasons=("selected_option_mapping_invalid",))
    if mapped.text == review.suspect_span:
        return _keep(review, result, reasons=("selected_existing_carrier_text",))

    projection = project_candidate_spans(review.suspect_span, mapped.text)
    if projection.status != "PROJECTED" or not projection.substantive_spans:
        return _keep(
            review, result,
            reasons=("selected_candidate_projection_unsafe", *projection.reasons),
        )
    guard_candidates = tuple(
        _guard_candidate(review, result, mapped, span, index)
        for index, span in enumerate(projection.substantive_spans)
    )
    carrier = _carrier(review)
    evidence = _evidence_segment(review)
    decisions = []
    for projection_index, candidate in enumerate(guard_candidates):
        for guard in (EvidenceGuard(), CriticalTokenGuard(), MinimalEditGuard(), CompletenessGuard()):
            decision = guard.evaluate(candidate, carrier, evidence)
            decision = replace(
                decision,
                metrics={**decision.metrics, "projection_index": projection_index},
            )
            decisions.append(decision)
            if decision.status != "PASS":
                return _keep(
                    review, result, decisions,
                    tuple(reason for value in decisions for reason in value.reasons),
                )
    try:
        final_text = apply_non_overlapping_candidates(
            review.carrier_text, guard_candidates,
        )
    except ValueError as exc:
        return _keep(review, result, decisions, (str(exc),))
    final_check = validate_corrected_segment(carrier, final_text, guard_candidates)
    if final_check.status != "PASS":
        return _keep(review, result, (*decisions, final_check), final_check.reasons)
    return SparkArbiterReviewDecision(
        review.review_id, review.segment_id, "AUTO_RESOLVED",
        review.carrier_text, final_text, result, tuple(decisions),
        ("spark_selected_local_candidate_and_all_guards_passed",),
    )
