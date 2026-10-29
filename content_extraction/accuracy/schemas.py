"""Immutable public contracts for SAFEBOX 181 correction and audit output."""

from dataclasses import asdict, dataclass, field
from typing import Any


def _serialize(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    return value


class _Serializable:
    def to_dict(self) -> dict:
        return {key: _serialize(value) for key, value in asdict(self).items()}


@dataclass(frozen=True)
class EvidenceProviderRecord(_Serializable):
    provider: str
    modality: str
    text: str
    status: str = "completed"
    confidence: float | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    timestamp_quality: str = "unknown"
    agreement_role: str = "alternative"
    evidence_ref: str = ""


@dataclass(frozen=True)
class TranscriptEvidenceSegment(_Serializable):
    segment_id: str
    start_ms: int
    end_ms: int
    clean_text: str
    ocr_text: str = ""
    asr_text: str = ""
    selected_source: str = ""
    included_in_clean_transcript: bool = True
    text_role: str = "unknown"
    content_relation: str = ""
    alternatives: tuple[str, ...] = ()
    conflicts: tuple[Any, ...] = ()
    critical_tokens: tuple[Any, ...] = ()
    provider_evidence: tuple[EvidenceProviderRecord, ...] = ()
    ocr_evidence_summary: dict = field(default_factory=dict)
    asr_evidence_summary: dict = field(default_factory=dict)
    selection_reason: str = ""
    fusion_status: str = ""
    confidence_level: str = "LOW"
    provisional_selection: bool = False


@dataclass(frozen=True)
class TranscriptEvidenceBundle(_Serializable):
    bundle_id: str
    source_video_id: str
    duration_ms: int
    source_clean_transcript: str
    segments: tuple[TranscriptEvidenceSegment, ...]
    schema_version: str = "accurate-evidence-v1"
    timebase: str = "milliseconds"
    source_fusion_status: str = ""
    source_confidence_level: str = "LOW"
    source_final_truth: bool = False
    glossary: dict = field(default_factory=dict)
    sanitation: dict = field(default_factory=lambda: {
        "provider_control_tags_removed": True,
        "raw_responses_excluded": True,
        "credentials_excluded": True,
    })
    source_provenance: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source_final_truth:
            raise ValueError("machine_evidence_cannot_be_final_truth")

    def to_dict(self) -> dict:
        payload = super().to_dict()
        payload["source_final_truth"] = False
        return payload


@dataclass(frozen=True)
class CarrierSelection(_Serializable):
    segment_id: str
    carrier_text: str
    carrier_source: str
    source_evidence_refs: tuple[str, ...] = ()
    alignment_score: float = 0.0
    prefix_preserved: bool = True
    suffix_preserved: bool = True
    selection_reason: str = ""
    confidence_level: str = "LOW"
    unresolved_conflicts: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        import hashlib
        return "sha256:" + hashlib.sha256(self.carrier_text.encode("utf-8")).hexdigest()


_CORRECTION_TYPES = {
    "HOMOPHONE_CORRECTION", "TYPO_CORRECTION", "OCR_CHARACTER_CORRECTION",
    "ASR_PHONETIC_CORRECTION", "ACRONYM_CORRECTION", "NUMBER_CORRECTION",
    "PROPER_NOUN_CORRECTION", "PUNCTUATION_CORRECTION",
    "SEGMENT_COMPLETENESS_REPAIR", "SEGMENT_BOUNDARY_REPAIR",
}


@dataclass(frozen=True)
class CorrectionCandidate(_Serializable):
    candidate_id: str
    segment_id: str
    carrier_fingerprint: str
    before_span: str
    after_span: str
    start_offset: int
    end_offset: int
    correction_type: str
    proposal_source: str
    alternatives: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    evidence_strength: str = "LEVEL_C"
    generation_reasons: tuple[str, ...] = ()
    selection_reason: str = ""
    confidence_level: str = "LOW"
    affects_critical_token: bool = False
    lexical_edit_distance: int = 0
    llm_metadata: dict | None = None

    def validate(self) -> None:
        if self.start_offset < 0 or self.end_offset < self.start_offset:
            raise ValueError("invalid_half_open_offsets")
        if self.end_offset - self.start_offset != len(self.before_span):
            raise ValueError("offset_length_mismatch")
        if self.correction_type not in _CORRECTION_TYPES:
            raise ValueError("unsupported_correction_type")
        if self.proposal_source not in {"deterministic", "local_llm", "external_llm"}:
            raise ValueError("unsupported_proposal_source")


@dataclass(frozen=True)
class GuardDecision(_Serializable):
    guard_name: str
    status: str
    reasons: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    metrics: dict = field(default_factory=dict)


@dataclass(frozen=True)
class UnresolvedSpan(_Serializable):
    segment_id: str
    before_span: str
    start_offset: int
    end_offset: int
    critical: bool = False
    reason: str = "low_confidence_unresolved"


@dataclass(frozen=True)
class CorrectionContext(_Serializable):
    previous_text: str
    current_text: str
    next_text: str
    provider_alternatives: tuple[str, ...] = ()
    ocr_evidence_summary: dict = field(default_factory=dict)
    asr_evidence_summary: dict = field(default_factory=dict)
    conflicts: tuple[Any, ...] = ()
    critical_tokens: tuple[Any, ...] = ()
    glossary_candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppliedCorrection(_Serializable):
    correction_id: str
    candidate_id: str
    segment_id: str
    before_span: str
    after_span: str
    start_offset: int
    end_offset: int
    correction_type: str
    alternatives: tuple[str, ...] = ()
    evidence_sources: tuple[str, ...] = ()
    selection_reason: str = ""
    confidence_level: str = "LOW"
    critical_token_change: bool = False
    llm_participated: bool = False
    guard_results: dict = field(default_factory=dict)
    final_approver: str = "deterministic_guard_pipeline"


@dataclass(frozen=True)
class AccurateTranscriptSegment(_Serializable):
    segment_id: str
    start_ms: int
    end_ms: int
    source_text: str
    carrier_text: str
    final_text: str
    carrier_source: str
    correction_ids: tuple[str, ...] = ()
    completeness_preserved: bool = True
    lexical_edit_distance: int = 0
    edit_ratio: float = 0.0
    risk_level: str = "LOW"


@dataclass(frozen=True)
class AccurateTranscriptResult(_Serializable):
    status: str
    final_accurate_transcript: str
    source_clean_transcript: str
    source_evidence_bundle_id: str
    segments: tuple[AccurateTranscriptSegment, ...] = ()
    corrections: tuple[AppliedCorrection, ...] = ()
    rejected_candidates: tuple[dict, ...] = ()
    unresolved_conflicts: tuple[dict, ...] = ()
    critical_tokens: tuple[Any, ...] = ()
    accuracy_metadata: dict | None = None
    source_provenance: dict = field(default_factory=dict)
    runtime_metadata: dict = field(default_factory=lambda: {
        "engine": "deterministic-only", "model": None, "model_hash": None,
        "offline": True, "network_request_count": 0,
    })
    cost: float = 0.0
    final_truth: bool = False
    schema_version: str = "accurate-transcript-v1"

    def __post_init__(self) -> None:
        if self.final_truth:
            raise ValueError("machine_result_cannot_be_final_truth")

    def to_dict(self) -> dict:
        payload = super().to_dict()
        payload["final_truth"] = False
        return payload


@dataclass(frozen=True)
class LLMProposalAudit(_Serializable):
    sample_id: str
    json_compliant: bool
    correction_needed: bool
    proposal: CorrectionCandidate | None
    response_fingerprint: str
    latency_ms: float
    usage: dict = field(default_factory=dict)
    error: str | None = None
    is_ground_truth: bool = False

    def __post_init__(self) -> None:
        if self.is_ground_truth:
            raise ValueError("llm_proposal_cannot_be_ground_truth")


@dataclass(frozen=True)
class SilverValidationItem(_Serializable):
    sample_id: str
    source_video_id: str
    media_sha256: str
    start_ms: int
    end_ms: int
    before_text: str
    validation_role: str
    provider_texts: dict = field(default_factory=dict)
    alternatives: tuple[str, ...] = ()
    conflicts: tuple[Any, ...] = ()
    critical_tokens: tuple[Any, ...] = ()
    is_ground_truth: bool = False

    def __post_init__(self) -> None:
        if self.is_ground_truth:
            raise ValueError("silver_item_cannot_be_ground_truth")


@dataclass(frozen=True)
class SilverValidationSet(_Serializable):
    dataset_id: str
    items: tuple[SilverValidationItem, ...]
    schema_version: str = "silver-validation-v1"
    is_ground_truth: bool = False

    def __post_init__(self) -> None:
        if self.is_ground_truth:
            raise ValueError("silver_set_cannot_be_ground_truth")


@dataclass(frozen=True)
class SilverValidationResult(_Serializable):
    dataset_id: str
    item_results: tuple[dict, ...]
    metrics: dict
    safety_gate: dict
    runtime: dict
    status: str = "completed"
    is_ground_truth: bool = False
    model_suitability: str = "UNVERIFIED_BY_HUMAN_GROUND_TRUTH"

    def __post_init__(self) -> None:
        if self.is_ground_truth:
            raise ValueError("silver_result_cannot_be_ground_truth")
