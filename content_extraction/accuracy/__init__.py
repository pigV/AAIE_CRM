"""Evidence-aware transcript correction, isolated from recognition providers."""

from .schemas import (
    AccurateTranscriptResult,
    AccurateTranscriptSegment,
    AppliedCorrection,
    CarrierSelection,
    CorrectionCandidate,
    CorrectionContext,
    EvidenceProviderRecord,
    GuardDecision,
    TranscriptEvidenceBundle,
    TranscriptEvidenceSegment,
    UnresolvedSpan,
)

__all__ = (
    "AccurateTranscriptResult", "AccurateTranscriptSegment", "AppliedCorrection",
    "CarrierSelection", "CorrectionCandidate", "EvidenceProviderRecord",
    "CorrectionContext", "GuardDecision", "TranscriptEvidenceBundle",
    "TranscriptEvidenceSegment", "UnresolvedSpan",
)
