"""Safe canonical transcript and uncertainty boundary for Latest Accurate Transcript."""

import json


_RISK_TYPES = {"P0", "P1", "P2", "P3"}


def _strings(values, *, limit=32):
    if not isinstance(values, (list, tuple)):
        raise ValueError("uncertainty_list_invalid")
    output = []
    for value in values[:limit]:
        text = str(value or "").strip()
        if text and text not in output:
            output.append(text[:500])
    return output


def validate_uncertainty_metadata(transcript, items):
    """Return a sanitized, ordered uncertainty list whose offsets match transcript."""
    text = str(transcript or "")
    if not isinstance(items, (list, tuple)):
        raise ValueError("uncertainty_metadata_invalid")
    output = []
    previous_end = 0
    for raw in sorted(items, key=lambda item: int(item.get("start_offset", -1))):
        if not isinstance(raw, dict):
            raise ValueError("uncertainty_item_invalid")
        start = int(raw.get("start_offset", -1))
        end = int(raw.get("end_offset", -1))
        if start < 0 or end <= start or end > len(text):
            raise ValueError("uncertainty_bounds_invalid")
        if start < previous_end:
            raise ValueError("uncertainty_overlap")
        current = str(raw.get("current_text") or "")
        if text[start:end] != current:
            raise ValueError("uncertainty_text_mismatch")
        risk = str(raw.get("risk_type") or "P2")
        if risk not in _RISK_TYPES:
            risk = "P2"
        evidence = raw.get("evidence_summary") or {}
        if not isinstance(evidence, dict):
            raise ValueError("uncertainty_evidence_invalid")
        output.append({
            "review_id": str(raw.get("review_id") or "")[:160],
            "segment_id": str(raw.get("segment_id") or "")[:160],
            "start_offset": start,
            "end_offset": end,
            "current_text": current,
            "candidate_texts": _strings(raw.get("candidate_texts") or ()),
            "reason": _strings(raw.get("reason") or ()),
            "evidence_summary": {
                "ocr": _strings(evidence.get("ocr") or ()),
                "asr": _strings(evidence.get("asr") or ()),
            },
            "risk_type": risk,
        })
        previous_end = end
    return output


def downstream_transcript_context(row):
    """Build the stable downstream interface from saved canonical fields only."""
    values = dict(row or {})
    try:
        metadata = json.loads(str(values.get("dependency_status_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        metadata = {}
    accurate = metadata.get("accurate_transcript") if isinstance(metadata, dict) else {}
    if not isinstance(accurate, dict):
        accurate = {}
    final_text = str(
        values.get("calibration_confirmed_text")
        or values.get("calibrated_text")
        or values.get("cleaned_text")
        or values.get("transcript_text")
        or values.get("merged_original_text")
        or ""
    )
    return {
        "final_transcript": final_text,
        "job_id": int(values.get("id") or 0),
        "source_url": str(values.get("source_url") or ""),
        "provenance": str(accurate.get("provenance") or "LATEST_ACCURATE_TRANSCRIPT"),
        "uncertainty_metadata": list(accurate.get("uncertainty_metadata") or ()),
    }
