import re
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher

from ..conflicts import classify_conflicts
from ..shared.recognition_text import normalize_chinese_numbers


_PUNCTUATION = re.compile(r"[\s，。！？、；：,.!?;:'\"“”‘’（）()\[\]【】—_-]+")


def normalize_recognition_text(text):
    return _PUNCTUATION.sub("", normalize_chinese_numbers(text)).lower()


@dataclass(frozen=True)
class OCRConsensusDecision:
    recommended_text: str
    recommended_provider: str
    reliability: str
    variants: tuple[str, ...]
    conflicts: tuple
    score_breakdown: dict


def _unique(values):
    return tuple(dict.fromkeys(values))


def choose_ocr_consensus(results, previous_text="", provider_metrics=None, segment_id=""):
    successful = tuple(result for result in results if result.status == "completed" and result.text)
    if not successful:
        return OCRConsensusDecision("", "", "none", (), (), {})
    variants = _unique(tuple(result.text for result in successful))
    normalized = tuple(normalize_recognition_text(result.text) for result in successful)
    counts = Counter(normalized)
    majority_key, majority_count = counts.most_common(1)[0]
    score_breakdown = {}

    if majority_count >= 2:
        winner = next(result for result, key in zip(successful, normalized) if key == majority_key)
        reliability = "high" if majority_count == len(successful) else "medium"
    elif len(successful) == 1:
        winner = successful[0]
        reliability = "low"
    else:
        metrics = provider_metrics or {}
        max_length = max(len(value) for value in normalized) or 1
        for result, normalized_text in zip(successful, normalized):
            completeness = len(normalized_text) / max_length
            context = SequenceMatcher(None, normalize_recognition_text(previous_text), normalized_text).ratio() if previous_text else 0
            score_breakdown[result.provider] = {
                "confidence": float(result.confidence) * 0.45,
                "completeness": completeness * 0.25,
                "box": (0.1 if result.boxes else 0.0),
                "provider_metric": float(metrics.get(result.provider, 0.5)) * 0.15,
                "context": context * 0.05,
            }
        winner = max(
            successful,
            key=lambda result: sum(score_breakdown[result.provider].values()),
        )
        reliability = "low"

    conflicts = classify_conflicts(segment_id, winner.text, variants) if len(variants) > 1 else ()
    return OCRConsensusDecision(
        winner.text, winner.provider, reliability, variants, conflicts, score_breakdown
    )
