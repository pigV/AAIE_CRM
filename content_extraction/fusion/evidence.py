from dataclasses import dataclass


@dataclass(frozen=True)
class ModalityEvidence:
    modality: str
    strength: str
    fusion_statuses: tuple[str, ...]
    confidence_levels: tuple[str, ...]
    providers_used: tuple[str, ...]
    provider_count: int
    agreement_count: int
    critical_conflict: bool
    unresolved_conflict: bool
    provenance_complete: bool
    reasons: tuple[str, ...]

    def to_dict(self):
        return {
            "modality": self.modality,
            "strength": self.strength,
            "fusion_statuses": list(self.fusion_statuses),
            "confidence_levels": list(self.confidence_levels),
            "providers_used": list(self.providers_used),
            "provider_count": int(self.provider_count),
            "agreement_count": int(self.agreement_count),
            "critical_conflict": bool(self.critical_conflict),
            "unresolved_conflict": bool(self.unresolved_conflict),
            "provenance_complete": bool(self.provenance_complete),
            "reasons": list(self.reasons),
        }


def classify_ocr_evidence(evidence):
    statuses = tuple(evidence.fusion_statuses)
    critical = bool(evidence.conflicts)
    unresolved = any("UNRESOLVED" in item or "CONFLICT" in item for item in statuses)
    reasons = []
    if not evidence.provenance_complete:
        strength = "WEAK"
        reasons.append("missing_or_partial_frame_provenance")
    elif not evidence.text:
        strength = "UNUSABLE"
        reasons.append("no_ocr_text")
    elif critical or unresolved:
        strength = "WEAK"
        reasons.append("ocr_conflict_or_unresolved_selection")
    elif evidence.text_role == "unknown":
        strength = "WEAK"
        reasons.append("uncertain_ocr_text_role")
    elif evidence.provider_count >= 3 and evidence.agreement_count >= 3 and evidence.cloud_arbitrated:
        strength = "VERY_STRONG"
        reasons.append("three_provider_traceable_agreement")
    elif evidence.provider_count >= 2 and evidence.agreement_count >= 2:
        strength = "STRONG"
        reasons.append("two_provider_traceable_agreement")
    elif evidence.provider_count >= 1:
        strength = "MODERATE"
        reasons.append("traceable_single_selection")
    else:
        strength = "WEAK"
        reasons.append("provider_provenance_missing")
    return ModalityEvidence(
        "ocr", strength, statuses, tuple(evidence.confidence_levels),
        tuple(evidence.providers_used), evidence.provider_count,
        evidence.agreement_count, critical, unresolved,
        evidence.provenance_complete, tuple(reasons),
    )


def classify_asr_evidence(segments, providers_used):
    segments = tuple(segments or ())
    providers = tuple(providers_used or ())
    statuses = tuple(dict.fromkeys(item.fusion_status for item in segments))
    confidence_levels = tuple(dict.fromkeys(item.confidence_level for item in segments))
    critical = any(
        item.fusion_status == "ASR_CRITICAL_TOKEN_CONFLICT" or any(
            conflict.conflict_type == "ASR_CRITICAL_TOKEN_CONFLICT"
            for conflict in item.conflicts
        ) for item in segments
    )
    unresolved = any("UNRESOLVED" in status or "CONFLICT" in status for status in statuses)
    reasons = []
    if not segments or not any(item.fused_text for item in segments):
        strength = "UNUSABLE"
        reasons.append("no_timed_asr_text")
    elif critical or any(item.confidence_level == "LOW" for item in segments):
        strength = "WEAK"
        reasons.append("asr_critical_conflict_or_low_quality")
    elif len(providers) >= 2 and all(
        item.fusion_status in {"ASR_CONSENSUS_EXACT", "ASR_CONSENSUS_SIMILAR"}
        and item.confidence_level == "HIGH"
        for item in segments
    ):
        strength = "STRONG"
        reasons.append("two_provider_asr_consensus")
    elif any(item.fusion_status == "ASR_SINGLE_PROVIDER" for item in segments):
        strength = "WEAK"
        reasons.append("single_asr_provider")
    else:
        strength = "MODERATE"
        reasons.append("traceable_aligned_asr_selection")
    return ModalityEvidence(
        "asr", strength, statuses, confidence_levels, providers,
        len(providers), len(providers) if strength == "STRONG" else 1,
        critical, unresolved, True, tuple(reasons),
    )
