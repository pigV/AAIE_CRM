"""Correction orchestration. Only deterministic guards authorize text changes."""

from typing import Protocol

from ..shared.critical_tokens import extract_critical_tokens
from ..shared.recognition_text import levenshtein_distance
from .candidate_generation import generate_deterministic_candidates
from .completeness import CompletenessGuard, select_complete_carrier, validate_corrected_segment
from .critical_guard import CriticalTokenGuard
from .evidence_guard import EvidenceGuard
from .glossary import AccurateDomainGlossary
from .minimal_edit import MinimalEditGuard, apply_non_overlapping_candidates
from .schemas import (
    AccurateTranscriptResult, AccurateTranscriptSegment, AppliedCorrection,
    CorrectionContext, CorrectionCandidate, UnresolvedSpan,
)
from .suspect import select_suspect_span, should_process_segment


class CorrectionProposalEngine(Protocol):
    engine_name: str

    def propose(self, span: UnresolvedSpan, context: CorrectionContext) -> CorrectionCandidate | None: ...


class AccurateTranscriptCorrector:
    def __init__(self, glossary=None, proposal_engine=None):
        self.glossary = glossary or AccurateDomainGlossary.default()
        self.proposal_engine = proposal_engine
        self.guards = (
            CriticalTokenGuard(), MinimalEditGuard(), CompletenessGuard(), EvidenceGuard(),
        )

    @staticmethod
    def _context(segments, index, segment, carrier):
        return CorrectionContext(
            previous_text=segments[index - 1].clean_text if index else "",
            current_text=carrier.carrier_text,
            next_text=segments[index + 1].clean_text if index + 1 < len(segments) else "",
            provider_alternatives=tuple(dict.fromkeys((
                *segment.alternatives, *(item.text for item in segment.provider_evidence)
            ))),
            ocr_evidence_summary=segment.ocr_evidence_summary,
            asr_evidence_summary=segment.asr_evidence_summary,
            conflicts=segment.conflicts, critical_tokens=segment.critical_tokens,
            glossary_candidates=(),
        )

    def _evaluate(self, candidate, carrier, segment):
        decisions = []
        for guard in self.guards:
            decision = guard.evaluate(candidate, carrier, segment)
            decisions.append(decision)
            if decision.status != "PASS":
                break
        return tuple(decisions)

    def correct(self, bundle):
        result_segments = []
        applied = []
        rejected = []
        unresolved = []
        source_segments = bundle.segments
        for index, segment in enumerate(source_segments):
            if not segment.included_in_clean_transcript:
                result_segments.append(AccurateTranscriptSegment(
                    segment_id=segment.segment_id, start_ms=segment.start_ms, end_ms=segment.end_ms,
                    source_text=segment.clean_text, carrier_text=segment.clean_text,
                    final_text="", carrier_source="excluded_cross_modal_evidence",
                    completeness_preserved=True, lexical_edit_distance=0,
                    edit_ratio=0.0, risk_level="LOW",
                ))
                continue
            carrier = select_complete_carrier(segment)
            processable = should_process_segment(segment)
            candidates = list(
                generate_deterministic_candidates(segment, carrier, self.glossary)
                if processable else ()
            )
            if not candidates and processable and self.proposal_engine is not None:
                span = select_suspect_span(segment, carrier)
                context = self._context(source_segments, index, segment, carrier)
                try:
                    proposal = self.proposal_engine.propose(span, context) if span else None
                    if proposal is not None:
                        proposal.validate()
                        if proposal.segment_id != segment.segment_id:
                            raise ValueError("proposal_segment_mismatch")
                        candidates.append(proposal)
                except (ValueError, TypeError) as exc:
                    rejected.append({"segment_id": segment.segment_id,
                                     "reason": f"invalid_proposal:{exc}",
                                     "guard_results": {}})
            accepted = []
            occupied = []
            competing_spans = {}
            for item in candidates:
                key = (item.start_offset, item.end_offset, item.before_span)
                competing_spans.setdefault(key, set()).add(item.after_span)
            for candidate in candidates:
                span_key = (candidate.start_offset, candidate.end_offset, candidate.before_span)
                if len(competing_spans[span_key]) > 1:
                    rejected.append({**candidate.to_dict(),
                                     "reason": "competing_candidate_values",
                                     "guard_results": {}})
                    continue
                if any(candidate.start_offset < end and candidate.end_offset > start for start, end in occupied):
                    rejected.append({**candidate.to_dict(), "reason": "overlapping_candidate",
                                     "guard_results": {}})
                    continue
                decisions = self._evaluate(candidate, carrier, segment)
                statuses = {item.guard_name: item.status for item in decisions}
                if len(decisions) == len(self.guards) and all(item.status == "PASS" for item in decisions):
                    accepted.append(candidate); occupied.append((candidate.start_offset, candidate.end_offset))
                    correction_id = f"correction:{candidate.candidate_id}"
                    applied.append(AppliedCorrection(
                        correction_id=correction_id, candidate_id=candidate.candidate_id,
                        segment_id=segment.segment_id, before_span=candidate.before_span,
                        after_span=candidate.after_span, start_offset=candidate.start_offset,
                        end_offset=candidate.end_offset, correction_type=candidate.correction_type,
                        alternatives=candidate.alternatives,
                        evidence_sources=candidate.evidence_refs,
                        selection_reason=candidate.selection_reason,
                        confidence_level=candidate.confidence_level,
                        critical_token_change=candidate.affects_critical_token,
                        llm_participated=candidate.proposal_source == "local_llm",
                        guard_results=statuses,
                    ))
                else:
                    rejected.append({**candidate.to_dict(), "guard_results": statuses,
                                     "reasons": [reason for item in decisions for reason in item.reasons]})
            final_text = apply_non_overlapping_candidates(carrier.carrier_text, tuple(accepted))
            final_check = validate_corrected_segment(carrier, final_text, tuple(accepted))
            if final_check.status != "PASS":
                final_text = carrier.carrier_text
                unresolved.append({"segment_id": segment.segment_id,
                                   "reason": "final_completeness_guard_rejected"})
            result_segments.append(AccurateTranscriptSegment(
                segment_id=segment.segment_id, start_ms=segment.start_ms, end_ms=segment.end_ms,
                source_text=segment.clean_text, carrier_text=carrier.carrier_text,
                final_text=final_text, carrier_source=carrier.carrier_source,
                correction_ids=tuple(f"correction:{item.candidate_id}" for item in accepted),
                completeness_preserved=final_check.status == "PASS",
                lexical_edit_distance=levenshtein_distance(carrier.carrier_text, final_text),
                edit_ratio=(levenshtein_distance(carrier.carrier_text, final_text) / max(1, len(carrier.carrier_text))),
                risk_level="LOW" if final_check.status == "PASS" else "HIGH",
            ))
            unresolved.extend({"segment_id": segment.segment_id, "reason": value}
                              for value in carrier.unresolved_conflicts)
        source_texts = tuple(
            item.clean_text for item in source_segments if item.included_in_clean_transcript
        )
        separator = "\n" if bundle.source_clean_transcript == "\n".join(source_texts) else ""
        final_text = separator.join(
            item.final_text for item in result_segments if item.final_text
        )
        return AccurateTranscriptResult(
            status="completed", final_accurate_transcript=final_text,
            source_clean_transcript=bundle.source_clean_transcript,
            source_evidence_bundle_id=bundle.bundle_id,
            segments=tuple(result_segments), corrections=tuple(applied),
            rejected_candidates=tuple(rejected), unresolved_conflicts=tuple(unresolved),
            critical_tokens=extract_critical_tokens(final_text),
            source_provenance={"source_video_id": bundle.source_video_id,
                               "evidence_bundle_id": bundle.bundle_id},
            runtime_metadata={
                "engine": getattr(self.proposal_engine, "engine_name", "deterministic-only"),
                "model": None, "model_hash": None, "offline": True,
                "network_request_count": 0,
            }, cost=0, final_truth=False,
        )
