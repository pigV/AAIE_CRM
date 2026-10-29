"""Deterministic suspect-span selection and stable-region routing."""

from difflib import SequenceMatcher

from ..shared.recognition_text import normalize_for_comparison
from .schemas import UnresolvedSpan


def _provider_agreement(segment) -> tuple[int, int]:
    target = normalize_for_comparison(segment.clean_text)
    providers = {
        (item.provider, item.modality) for item in segment.provider_evidence
        if item.status == "completed"
        and normalize_for_comparison(item.text) == target
    }
    return (
        len({provider for provider, modality in providers if modality == "ocr"}),
        len({provider for provider, modality in providers if modality == "asr"}),
    )


def should_process_segment(segment) -> bool:
    if segment.conflicts or segment.provisional_selection:
        return True
    if segment.confidence_level != "HIGH":
        return True
    ocr_count, asr_count = _provider_agreement(segment)
    return not (ocr_count >= 2 and asr_count >= 2)


def _replacement_span(carrier_text: str, alternative: str):
    matcher = SequenceMatcher(None, carrier_text, alternative, autojunk=False)
    candidates = []
    for tag, left_start, left_end, _right_start, _right_end in matcher.get_opcodes():
        if tag == "replace" and left_end > left_start:
            candidates.append((left_end - left_start, left_start, left_end))
    return min(candidates, default=None)


def select_suspect_span(segment, carrier, diagnostic_probe=False):
    best = None
    alternatives = tuple(dict.fromkeys((
        *segment.alternatives,
        *(item.text for item in segment.provider_evidence if item.text),
    )))
    for alternative in alternatives:
        if not alternative or alternative == carrier.carrier_text:
            continue
        candidate = _replacement_span(carrier.carrier_text, alternative)
        if candidate and (best is None or candidate < best):
            best = candidate
    if best:
        _, start, end = best
        end = min(end, start + 12)
        return UnresolvedSpan(
            segment.segment_id, carrier.carrier_text[start:end], start, end,
            reason="provider_difference",
        )
    if not diagnostic_probe and not should_process_segment(segment):
        return None
    end = min(12, len(carrier.carrier_text))
    if end <= 0:
        return None
    return UnresolvedSpan(
        segment.segment_id, carrier.carrier_text[:end], 0, end,
        reason=("silver_stability_probe" if diagnostic_probe else "low_evidence_unresolved"),
    )
