"""Read-only Spark Lite evaluation over immutable SAFEBOX 181 Review Queues."""

from dataclasses import dataclass

from .completeness import CompletenessGuard, validate_corrected_segment
from .critical_guard import CriticalTokenGuard
from .evidence_guard import EvidenceGuard
from .minimal_edit import MinimalEditGuard, apply_non_overlapping_candidates
from .schemas import CarrierSelection, EvidenceProviderRecord, GuardDecision, TranscriptEvidenceSegment


@dataclass(frozen=True)
class SparkReviewDecision:
    review_id: str
    segment_id: str
    status: str
    source_text: str
    final_text: str
    proposal_audit: object
    guard_decisions: tuple[GuardDecision, ...] = ()
    reasons: tuple[str, ...] = ()

    def to_dict(self):
        return {
            "review_id": self.review_id,
            "segment_id": self.segment_id,
            "status": self.status,
            "source_text": self.source_text,
            "final_text": self.final_text,
            "proposal_audit": self.proposal_audit.to_dict(),
            "guard_decisions": [value.to_dict() for value in self.guard_decisions],
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class SparkQueueBenchmarkResult:
    source_video_id: str
    source_queue_hash: str
    source_queue_version: str
    item_results: tuple[SparkReviewDecision, ...]
    metrics: dict
    schema_version: str = "spark-lite-frozen-queue-benchmark-v1"

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "source_video_id": self.source_video_id,
            "source_queue_hash": self.source_queue_hash,
            "source_queue_version": self.source_queue_version,
            "metrics": dict(self.metrics),
            "item_results": [value.to_dict() for value in self.item_results],
        }


def _provider_records(review):
    records = []
    for modality, values in (("ocr", review.ocr_evidence), ("asr", review.asr_evidence)):
        for provider, text in dict(values or {}).items():
            if not text:
                continue
            records.append(EvidenceProviderRecord(
                provider=str(provider),
                modality=modality,
                text=str(text),
                status="completed",
                agreement_role="alternative",
                evidence_ref=f"queue:{review.review_id}:{provider}",
            ))
    return tuple(records)


def _evidence_segment(review):
    records = _provider_records(review)
    alternatives = tuple(value.text for value in review.alternatives if value.text)
    return TranscriptEvidenceSegment(
        segment_id=review.segment_id,
        start_ms=review.start_ms,
        end_ms=review.end_ms,
        clean_text=review.carrier_text,
        ocr_text=" ".join(value.text for value in records if value.modality == "ocr"),
        asr_text=" ".join(value.text for value in records if value.modality == "asr"),
        alternatives=alternatives,
        provider_evidence=records,
        confidence_level="LOW",
        provisional_selection=True,
    )


def _carrier(review):
    return CarrierSelection(
        segment_id=review.segment_id,
        carrier_text=review.carrier_text,
        carrier_source="frozen_quick_review_queue",
        source_evidence_refs=tuple(value.evidence_ref for value in _provider_records(review)),
        alignment_score=1.0,
        prefix_preserved=True,
        suffix_preserved=True,
        selection_reason="immutable frozen Review Item carrier",
        confidence_level="LOW",
    )


def _keep(review, audit, decisions=(), reasons=()):
    return SparkReviewDecision(
        review.review_id, review.segment_id, "KEEP_REVIEW",
        review.carrier_text, review.carrier_text, audit,
        tuple(decisions), tuple(reasons),
    )


def evaluate_review_item(review, adapter):
    audit = adapter.propose_with_audit(review, review.review_id)
    if audit.status != "completed":
        return _keep(review, audit, reasons=(audit.error_code or "spark_provider_unavailable",))
    candidate = audit.proposal
    if candidate is None:
        return _keep(review, audit, reasons=("spark_no_safe_proposal",))
    if candidate.segment_id != review.segment_id:
        return _keep(review, audit, reasons=("candidate_segment_mismatch",))
    if candidate.start_offset < review.start_offset or candidate.end_offset > review.end_offset:
        return _keep(review, audit, reasons=("candidate_outside_review_span",))

    carrier = _carrier(review)
    evidence = _evidence_segment(review)
    guards = (EvidenceGuard(), CriticalTokenGuard(), MinimalEditGuard(), CompletenessGuard())
    decisions = []
    for guard in guards:
        decision = guard.evaluate(candidate, carrier, evidence)
        decisions.append(decision)
        if decision.status != "PASS":
            return _keep(
                review, audit, decisions,
                tuple(reason for value in decisions for reason in value.reasons),
            )
    try:
        final_text = apply_non_overlapping_candidates(review.carrier_text, (candidate,))
    except ValueError as exc:
        return _keep(review, audit, decisions, (str(exc),))
    final_check = validate_corrected_segment(carrier, final_text, (candidate,))
    if final_check.status != "PASS":
        return _keep(review, audit, (*decisions, final_check), final_check.reasons)
    return SparkReviewDecision(
        review.review_id, review.segment_id, "AUTO_RESOLVED",
        review.carrier_text, final_text, audit, tuple(decisions),
        ("all_existing_guards_passed",),
    )


def evaluate_frozen_queue(queue, adapter):
    decisions = tuple(evaluate_review_item(item, adapter) for item in queue.items)
    audits = tuple(value.proposal_audit for value in decisions)
    auto_resolved = sum(value.status == "AUTO_RESOLVED" for value in decisions)
    rejected = sum(
        value.status == "KEEP_REVIEW"
        and value.proposal_audit.proposal is not None
        for value in decisions
    )
    all_confirmed = all(
        value.free_only_status == "CONFIRMED_BY_OPERATOR" for value in audits
    ) if audits else False
    metrics = {
        "spark_requests": sum(value.request_count for value in audits),
        "spark_success": sum(value.status == "completed" for value in audits),
        "spark_rejected_by_guard": rejected,
        "spark_auto_resolved": auto_resolved,
        "remaining_review_items": len(queue.items) - auto_resolved,
        "input_tokens": sum(value.usage.prompt_tokens for value in audits),
        "output_tokens": sum(value.usage.completion_tokens for value in audits),
        "total_tokens": sum(value.usage.total_tokens for value in audits),
        "free_only_status": "CONFIRMED_BY_OPERATOR" if all_confirmed else "FAIL_CLOSED",
        "estimated_cost": 0,
    }
    return SparkQueueBenchmarkResult(
        source_video_id=queue.source_video_id,
        source_queue_hash=queue.queue_hash,
        source_queue_version=queue.queue_version,
        item_results=decisions,
        metrics=metrics,
    )
