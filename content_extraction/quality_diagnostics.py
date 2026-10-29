import re
from dataclasses import dataclass

from .shared.recognition_text import normalize_for_comparison


_ACRONYM = re.compile(r"[A-Z]{2,10}")
_CJK = re.compile(r"[\u4e00-\u9fff]")


@dataclass(frozen=True)
class ConflictDiagnosis:
    category: str
    reason: str
    frame_id: str
    accepted_by_evidence_gate: bool

    def to_dict(self):
        return {
            "category": self.category,
            "reason": self.reason,
            "frame_id": self.frame_id,
            "accepted_by_evidence_gate": bool(self.accepted_by_evidence_gate),
        }


def _value(item, name, default=None):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _box_union(result):
    boxes = _value(result, "bounding_boxes", ()) or ()
    valid = []
    for item in boxes:
        try:
            x = float(_value(item, "x"))
            y = float(_value(item, "y"))
            width = float(_value(item, "width"))
            height = float(_value(item, "height"))
        except (TypeError, ValueError):
            continue
        if width > 0 and height > 0:
            valid.append((x, y, x + width, y + height))
    if not valid:
        return None
    return (
        min(item[0] for item in valid), min(item[1] for item in valid),
        max(item[2] for item in valid), max(item[3] for item in valid),
    )


def _iou(first, second):
    if first is None or second is None:
        return None
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    first_area = max(0.0, first[2] - first[0]) * max(0.0, first[3] - first[1])
    second_area = max(0.0, second[2] - second[0]) * max(0.0, second[3] - second[1])
    union = first_area + second_area - intersection
    return intersection / union if union else 0.0


def _successful_texts(frame):
    output = []
    for result in _value(frame, "provider_results", ()) or ():
        if _value(result, "status", "") != "completed":
            continue
        text = str(_value(result, "text", "") or "")
        if text:
            output.append((result, text, normalize_for_comparison(text)))
    return tuple(output)


def classify_ocr_conflict(frame, evidence, previous_frame=None):
    """Classify a frame in the approved E/F/B/D/G/C/A/H order."""

    frame_id = str(_value(frame, "frame_id", "") or "")
    accepted = bool(_value(evidence, "accepted", False))
    role = str(_value(evidence, "role", "unknown") or "unknown")
    reason = str(_value(evidence, "rejection_reason", "") or "")
    texts = _successful_texts(frame)

    if role in {"screen_document", "ui", "fixed_overlay", "title"} or reason in {
        "dense_text_not_subtitle", "role_screen_document", "role_ui",
        "role_fixed_overlay", "role_title",
    }:
        return ConflictDiagnosis("E", f"explicit_non_subtitle_{role}", frame_id, accepted)

    if len(texts) >= 2:
        overlap = _iou(_box_union(texts[0][0]), _box_union(texts[1][0]))
        if overlap is not None and overlap < 0.15:
            return ConflictDiagnosis("F", "provider_box_union_mismatch", frame_id, accepted)

        first_normalized, second_normalized = texts[0][2], texts[1][2]
        if (
            first_normalized and second_normalized
            and first_normalized != second_normalized
            and (
                first_normalized in second_normalized
                or second_normalized in first_normalized
            )
        ):
            return ConflictDiagnosis("B", "normalized_text_containment", frame_id, accepted)

        first_acronyms = set(_ACRONYM.findall(texts[0][1].upper()))
        second_acronyms = set(_ACRONYM.findall(texts[1][1].upper()))
        if first_acronyms != second_acronyms and (first_acronyms or second_acronyms):
            return ConflictDiagnosis("D", "aligned_acronym_difference", frame_id, accepted)

    current_text = normalize_for_comparison(_value(frame, "fused_text", ""))
    previous_text = normalize_for_comparison(
        _value(previous_frame, "fused_text", "") if previous_frame else ""
    )
    if (
        previous_text and current_text and previous_text != current_text
        and (previous_text in current_text or current_text in previous_text)
    ):
        return ConflictDiagnosis("G", "progressive_or_replacement_sequence", frame_id, accepted)

    if accepted and len(texts) >= 2 and texts[0][2] != texts[1][2] and (
        _CJK.search(texts[0][1]) or _CJK.search(texts[1][1])
    ):
        return ConflictDiagnosis("C", "valid_geometry_chinese_difference", frame_id, accepted)

    if not accepted:
        return ConflictDiagnosis("A", reason or "evidence_gate_rejected", frame_id, accepted)
    return ConflictDiagnosis("H", "unclassified_or_no_provider_difference", frame_id, accepted)


def summarize_conflict_diagnoses(diagnoses):
    diagnoses = tuple(diagnoses)
    total = len(diagnoses)
    counts = {category: 0 for category in "ABCDEFGH"}
    for diagnosis in diagnoses:
        category = str(_value(diagnosis, "category", "H") or "H")
        counts[category if category in counts else "H"] += 1

    def item(count):
        return {
            "count": count,
            "ratio": round(count / total, 6) if total else 0.0,
        }

    input_count = sum(counts[value] for value in ("E", "G", "A"))
    recognition_count = sum(counts[value] for value in ("B", "C", "D", "F"))
    return {
        "total": total,
        "categories": {key: item(value) for key, value in counts.items()},
        "INPUT_SELECTION_ERROR": item(input_count),
        "TRUE_OCR_RECOGNITION_DIFFERENCE": item(recognition_count),
        "unclassified": item(counts["H"]),
    }


def build_provider_accounting(current, checkpoint, diagnostic):
    providers = sorted(set(current or {}) | set(checkpoint or {}))
    current_values = {name: int((current or {}).get(name, 0) or 0) for name in providers}
    checkpoint_values = {
        name: int((checkpoint or {}).get(name, 0) or 0) for name in providers
    }
    diagnostic = dict(diagnostic or {})
    diagnostic_calls = dict(diagnostic.get("calls") or {})
    for name in set(providers) | set(diagnostic_calls):
        diagnostic_calls[name] = int(diagnostic_calls.get(name, 0) or 0)
    return {
        "current_run_delta": current_values,
        "checkpoint_accumulated": checkpoint_values,
        "diagnostic_rerun_total": {
            "calls": dict(sorted(diagnostic_calls.items())),
            "exact": bool(diagnostic.get("exact", True)),
            "note": str(diagnostic.get("note", "") or ""),
        },
    }
