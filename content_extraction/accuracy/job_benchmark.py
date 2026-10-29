"""Correction-only benchmark over an already frozen Cross-Modal payload."""

from .corrector import AccurateTranscriptCorrector
from .evidence import build_evidence_bundle_from_payload
from .glossary import AccurateDomainGlossary


def run_frozen_job_correction(cross_modal_payload, proposal_engine=None):
    glossary = AccurateDomainGlossary.default()
    bundle = build_evidence_bundle_from_payload(cross_modal_payload, glossary)
    result = AccurateTranscriptCorrector(
        glossary=glossary, proposal_engine=proposal_engine,
    ).correct(bundle)
    diffs = tuple({
        "segment_id": item.segment_id, "start_ms": item.start_ms, "end_ms": item.end_ms,
        "before": item.source_text, "carrier": item.carrier_text, "after": item.final_text,
        "carrier_source": item.carrier_source,
        "lexical_edit_distance": item.lexical_edit_distance,
        "edit_ratio": item.edit_ratio, "risk_level": item.risk_level,
    } for item in result.segments if item.final_text != item.source_text)
    return {
        "schema_version": "accurate-transcript-phase1b-job-v1",
        "source_video_id": bundle.source_video_id,
        "source_evidence_bundle_id": bundle.bundle_id,
        "source_transcript": bundle.source_clean_transcript,
        "final_transcript": result.final_accurate_transcript,
        "segments": [item.to_dict() for item in result.segments],
        "corrections": [item.to_dict() for item in result.corrections],
        "rejected_candidates": list(result.rejected_candidates),
        "unresolved_conflicts": list(result.unresolved_conflicts),
        "diffs": list(diffs),
        "metrics": {
            "segment_count": len(result.segments),
            "changed_segment_count": len(diffs),
            "accepted_correction_count": len(result.corrections),
            "rejected_candidate_count": len(result.rejected_candidates),
            "provider_execution_count": 0,
            "qwen_request_count": int(getattr(proposal_engine, "request_count", 0)),
            "estimated_cost": 0,
        },
        "provider_execution_performed": False,
        "billing_mode": "FREE_ONLY",
        "is_ground_truth": False,
        "accuracy_claim": "NOT_AVAILABLE_WITHOUT_HUMAN_GROUND_TRUTH",
        "model_suitability": "UNVERIFIED_BY_HUMAN_GROUND_TRUTH",
        "change_signal": (
            "EVIDENCE_SUPPORTED_CHANGESET" if result.corrections
            else "NO_ACCEPTED_CORRECTION"
        ),
        "final_truth": False,
    }
