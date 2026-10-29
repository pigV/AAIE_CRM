"""Conservative authorization from immutable Provider evidence."""

from .schemas import GuardDecision
from ..shared.recognition_text import normalize_for_comparison


_CONTEXTUAL_TYPES = {
    "HOMOPHONE_CORRECTION", "TYPO_CORRECTION", "OCR_CHARACTER_CORRECTION",
    "ASR_PHONETIC_CORRECTION", "PUNCTUATION_CORRECTION",
}
_LLM_PROPOSAL_SOURCES = {"local_llm", "external_llm"}


def _supporting_records(candidate, carrier, evidence):
    patched = (
        carrier.carrier_text[:candidate.start_offset]
        + candidate.after_span
        + carrier.carrier_text[candidate.end_offset:]
    )
    patched_normalized = normalize_for_comparison(patched)
    return tuple(
        item for item in evidence.provider_evidence
        if item.status == "completed"
        and patched_normalized
        and patched_normalized in normalize_for_comparison(item.text)
    )


class EvidenceGuard:
    def evaluate(self, candidate, carrier, evidence):
        supporting = _supporting_records(candidate, carrier, evidence)
        providers = {item.provider for item in supporting}
        refs = tuple(item.evidence_ref for item in supporting if item.evidence_ref)
        if len(providers) >= 2:
            return GuardDecision(
                "evidence", "PASS", ("independent_provider_support",), refs,
                {"evidence_level": "LEVEL_1", "provider_count": len(providers)},
            )
        if (
            len(providers) == 1
            and candidate.proposal_source in _LLM_PROPOSAL_SOURCES
            and candidate.confidence_level == "HIGH"
            and not candidate.affects_critical_token
        ):
            return GuardDecision(
                "evidence", "PASS", ("provider_plus_high_qwen",), refs,
                {"evidence_level": "LEVEL_2", "provider_count": 1},
            )
        if (
            not providers
            and candidate.proposal_source in _LLM_PROPOSAL_SOURCES
            and candidate.confidence_level == "HIGH"
            and not candidate.affects_critical_token
            and max(len(candidate.before_span), len(candidate.after_span)) <= 4
            and candidate.lexical_edit_distance <= 2
            and candidate.correction_type in _CONTEXTUAL_TYPES
        ):
            return GuardDecision(
                "evidence", "PASS", ("CONTEXTUAL_CORRECTION",), (),
                {"evidence_level": "LEVEL_3", "provider_count": 0},
            )
        return GuardDecision(
            "evidence", "REJECT", ("insufficient_correction_evidence",), refs,
            {"evidence_level": "NONE", "provider_count": len(providers)},
        )
