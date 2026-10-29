from collections import Counter
from dataclasses import dataclass

from ..conflicts import classify_conflicts
from ..models import RecognitionAlignment, RecognitionSourceResult
from ..ocr.consensus import choose_ocr_consensus, normalize_recognition_text


@dataclass(frozen=True)
class RecognitionFusionDecision:
    alignment: RecognitionAlignment
    source_results: tuple
    conflicts: tuple
    conflict_modalities: tuple[str, ...]


def _overlaps(start_ms, end_ms, item):
    return min(end_ms, item.end_ms) > max(start_ms, item.start_ms)


def _as_source(result):
    return RecognitionSourceResult(
        result.provider, "ocr", result.start_ms, result.end_ms,
        result.text, result.confidence, result.status,
    )


def fuse_recognition_segment(segment_id, start_ms, end_ms, ocr_results, asr_results):
    ocr_results = tuple(
        result for result in ocr_results
        if result.status == "completed" and result.text and _overlaps(start_ms, end_ms, result)
    )
    asr_results = tuple(
        result for result in asr_results
        if result.status == "completed" and result.text and _overlaps(start_ms, end_ms, result)
    )
    source_results = tuple(_as_source(result) for result in ocr_results) + asr_results
    if not source_results:
        alignment = RecognitionAlignment(segment_id, start_ms, end_ms, "", "none", ())
        return RecognitionFusionDecision(alignment, (), (), ())

    normalized = tuple(normalize_recognition_text(result.text) for result in source_results)
    counts = Counter(normalized)
    winner_key, winner_count = counts.most_common(1)[0]
    if winner_count >= 2:
        winner = next(
            result for result, key in zip(source_results, normalized) if key == winner_key
        )
    else:
        ocr_decision = choose_ocr_consensus(ocr_results, segment_id=segment_id)
        candidates = list(source_results)
        if ocr_decision.recommended_text:
            winner = next(
                result for result in candidates
                if result.modality == "ocr" and result.provider == ocr_decision.recommended_provider
            )
        else:
            winner = max(candidates, key=lambda item: item.confidence)
        winner_key = normalize_recognition_text(winner.text)

    ocr_support = any(
        result.modality == "ocr" and normalize_recognition_text(result.text) == winner_key
        for result in source_results
    )
    asr_support = any(
        result.modality == "asr" and normalize_recognition_text(result.text) == winner_key
        for result in source_results
    )
    if ocr_support and asr_support and winner_count >= 2:
        reliability = "high"
    elif winner_count >= 2:
        reliability = "medium"
    else:
        reliability = "low"

    conflict_modalities = []
    for modality in ("ocr", "asr"):
        modal_values = {
            normalize_recognition_text(result.text)
            for result in source_results if result.modality == modality
        }
        if modal_values and (winner_key not in modal_values or len(modal_values) > 1):
            conflict_modalities.append(modality)

    variants = tuple(dict.fromkeys(result.text for result in source_results))
    conflicts = classify_conflicts(segment_id, winner.text, variants) if len(variants) > 1 else ()
    alignment = RecognitionAlignment(
        segment_id, start_ms, end_ms, winner.text, reliability,
        tuple(result.provider for result in source_results),
    )
    return RecognitionFusionDecision(
        alignment, source_results, conflicts, tuple(conflict_modalities)
    )
