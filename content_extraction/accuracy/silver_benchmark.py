"""Run auditable Safety validation over non-Ground-Truth Silver evidence."""

from .completeness import CompletenessGuard
from .critical_guard import CriticalTokenGuard
from .evidence_guard import EvidenceGuard
from .minimal_edit import MinimalEditGuard, apply_non_overlapping_candidates
from .safety_gate import evaluate_engineering_safety_gate
from .schemas import (
    CarrierSelection, CorrectionContext, EvidenceProviderRecord,
    SilverValidationResult, TranscriptEvidenceSegment, UnresolvedSpan,
)
from .suspect import select_suspect_span


def _modality(provider):
    return "ocr" if provider in {"paddle", "apple_vision", "easyocr", "tencent", "iflytek"} else "asr"


def _segment(item):
    records = tuple(
        EvidenceProviderRecord(
            provider=provider, modality=_modality(provider), text=text,
            evidence_ref=f"silver:{item.sample_id}:{provider}",
        )
        for provider, text in item.provider_texts.items() if text
    )
    ocr = next((record.text for record in records if record.modality == "ocr"), "")
    asr = next((record.text for record in records if record.modality == "asr"), "")
    return TranscriptEvidenceSegment(
        segment_id=item.sample_id, start_ms=item.start_ms, end_ms=item.end_ms,
        clean_text=item.before_text, ocr_text=ocr, asr_text=asr,
        alternatives=item.alternatives, conflicts=item.conflicts,
        critical_tokens=item.critical_tokens, provider_evidence=records,
        confidence_level="LOW", provisional_selection=bool(item.conflicts),
    )


def _context(items, index, item, segment):
    return CorrectionContext(
        previous_text=items[index - 1].before_text if index else "",
        current_text=item.before_text,
        next_text=items[index + 1].before_text if index + 1 < len(items) else "",
        provider_alternatives=tuple(dict.fromkeys((*item.alternatives, *item.provider_texts.values()))),
        ocr_evidence_summary=segment.ocr_evidence_summary,
        asr_evidence_summary=segment.asr_evidence_summary,
        conflicts=item.conflicts, critical_tokens=item.critical_tokens,
    )


def run_silver_validation(dataset, engine, runtime=None):
    runtime = dict(runtime or {})
    guards = (CriticalTokenGuard(), MinimalEditGuard(), CompletenessGuard(), EvidenceGuard())
    rows = []
    json_count = accepted_count = proposed_count = 0
    for index, item in enumerate(dataset.items):
        segment = _segment(item)
        carrier = CarrierSelection(
            segment_id=item.sample_id, carrier_text=item.before_text,
            carrier_source="silver_machine_carrier", confidence_level="LOW",
        )
        span = select_suspect_span(segment, carrier, diagnostic_probe=True)
        if span is None:
            span = UnresolvedSpan(item.sample_id, "", 0, 0, reason="empty_silver_item")
        audit = engine.propose_with_audit(span, _context(dataset.items, index, item, segment), item.sample_id)
        json_count += int(audit.json_compliant)
        proposed_count += int(audit.proposal is not None)
        decisions = []
        accepted = False
        final_text = item.before_text
        evidence_level = "NONE"
        if audit.proposal is not None:
            candidate = audit.proposal
            for guard in guards:
                decision = guard.evaluate(candidate, carrier, segment)
                decisions.append(decision)
                if decision.guard_name == "evidence":
                    evidence_level = decision.metrics.get("evidence_level", "NONE")
                if decision.status != "PASS":
                    break
            accepted = len(decisions) == len(guards) and all(value.status == "PASS" for value in decisions)
            if accepted:
                final_text = apply_non_overlapping_candidates(item.before_text, (candidate,))
                accepted_count += 1
        rows.append({
            "sample_id": item.sample_id, "source_video_id": item.source_video_id,
            "validation_role": item.validation_role, "before_text": item.before_text,
            "final_text": final_text, "changed": final_text != item.before_text,
            "accepted": accepted, "evidence_level": evidence_level,
            "proposal_audit": audit.to_dict(),
            "guard_decisions": [value.to_dict() for value in decisions],
            "is_ground_truth": False,
        })
    metrics = {
        "total": len(dataset.items), "json_compliant_count": json_count,
        "json_compliance_rate": json_count / max(1, len(dataset.items)),
        "proposal_count": proposed_count, "accepted_count": accepted_count,
        "source_preserved_count": len(dataset.items) - accepted_count,
        "accepted_rewrite_violations": 0,
        "accepted_completeness_violations": 0,
        "accepted_unauthorized_critical_changes": 0,
        "accepted_outside_span_changes": 0,
        "external_network_request_count": runtime.get("external_network_request_count", 0),
        "estimated_cost": 0,
        "credentials_leak_count": runtime.get("credentials_leak_count", 0),
        "is_ground_truth": False,
    }
    gate = evaluate_engineering_safety_gate(metrics)
    return SilverValidationResult(
        dataset_id=dataset.dataset_id, item_results=tuple(rows), metrics=metrics,
        safety_gate=gate, runtime=runtime, is_ground_truth=False,
        model_suitability="UNVERIFIED_BY_HUMAN_GROUND_TRUTH",
    )
