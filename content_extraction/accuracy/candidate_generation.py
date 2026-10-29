"""Generic localized candidates derived only from traceable alternatives."""

from difflib import SequenceMatcher
import hashlib

from ..shared.critical_tokens import extract_critical_tokens
from ..shared.recognition_text import levenshtein_distance
from .schemas import CorrectionCandidate


def _critical(before, after):
    return bool(extract_critical_tokens(before) or extract_critical_tokens(after))


def _type(before, after, critical):
    if critical:
        if any(char.isdigit() for char in before + after):
            return "NUMBER_CORRECTION"
        if before.isascii() and after.isascii():
            return "ACRONYM_CORRECTION"
        return "PROPER_NOUN_CORRECTION"
    if not before.strip() or not after.strip():
        return "PUNCTUATION_CORRECTION"
    return "ASR_PHONETIC_CORRECTION"


def _expand_ascii_token(left, right, left_start, left_end, right_start, right_end):
    if not (left[left_start:left_end] + right[right_start:right_end]).isascii():
        return left_start, left_end, right_start, right_end
    while (left_start > 0 and right_start > 0
           and left[left_start - 1].isascii() and right[right_start - 1].isascii()
           and left[left_start - 1].isalnum() and right[right_start - 1].isalnum()):
        left_start -= 1
        right_start -= 1
    while (left_end < len(left) and right_end < len(right)
           and left[left_end].isascii() and right[right_end].isascii()
           and left[left_end].isalnum() and right[right_end].isalnum()):
        left_end += 1
        right_end += 1
    return left_start, left_end, right_start, right_end


def generate_deterministic_candidates(segment, carrier, glossary):
    del glossary  # Lookup may score provider values; it never creates authority.
    alternatives = tuple(dict.fromkeys((
        *segment.alternatives, segment.ocr_text, segment.asr_text,
        *(item.text for item in segment.provider_evidence),
    )))
    output = []
    seen = set()
    for alternative in alternatives:
        if not alternative or alternative == carrier.carrier_text:
            continue
        matcher = SequenceMatcher(None, carrier.carrier_text, alternative, autojunk=False)
        for tag, left_start, left_end, right_start, right_end in matcher.get_opcodes():
            if tag != "replace":
                continue
            left_start, left_end, right_start, right_end = _expand_ascii_token(
                carrier.carrier_text, alternative,
                left_start, left_end, right_start, right_end,
            )
            before = carrier.carrier_text[left_start:left_end]
            after = alternative[right_start:right_end]
            key = (left_start, left_end, after)
            if not before or not after or key in seen:
                continue
            seen.add(key)
            supporting = tuple(
                item.evidence_ref for item in segment.provider_evidence
                if item.text == alternative and item.evidence_ref
            )
            critical = _critical(before, after)
            digest = hashlib.sha256(
                f"{segment.segment_id}|{left_start}|{left_end}|{after}".encode("utf-8")
            ).hexdigest()[:16]
            output.append(CorrectionCandidate(
                candidate_id=f"candidate:{digest}", segment_id=segment.segment_id,
                carrier_fingerprint=carrier.fingerprint,
                before_span=before, after_span=after,
                start_offset=left_start, end_offset=left_end,
                correction_type=_type(before, after, critical),
                proposal_source="deterministic",
                alternatives=tuple(dict.fromkeys((before, after))),
                evidence_refs=supporting,
                evidence_strength="LEVEL_B" if supporting else "LEVEL_C",
                generation_reasons=("provider_alternative",),
                selection_reason="localized literal provider alternative",
                confidence_level="HIGH" if supporting else "MEDIUM",
                affects_critical_token=critical,
                lexical_edit_distance=levenshtein_distance(before, after),
            ))
    return tuple(sorted(output, key=lambda item: (item.start_offset, item.end_offset, item.after_span)))
