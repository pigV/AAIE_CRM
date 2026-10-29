"""Pure state transitions for independent two-pass Human Ground Truth review."""

import copy
import unicodedata


_TERMINAL = {"HUMAN_VERIFIED", "AMBIGUOUS", "SKIPPED"}
_EVIDENCE_FIELDS = (
    "before_text", "current_clean_raw_transcript", "provider_texts",
    "alternatives", "conflicts", "critical_tokens",
)


def normalize_review_text(value: str) -> str:
    """Normalize compatibility characters and ignore punctuation/spacing only."""
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(
        char for char in normalized
        if not unicodedata.category(char).startswith(("P", "Z"))
        and not char.isspace()
    )


def review_stage(item: dict) -> str:
    status = (item.get("review") or {}).get("verification_status")
    return {
        "PENDING_FIRST_PASS": "FIRST_PASS",
        "PENDING_SECOND_PASS": "SECOND_PASS",
        "RECONCILIATION_REQUIRED": "RECONCILIATION",
    }.get(status, "COMPLETE")


def _decision(item: dict, human_text: str) -> str:
    return (
        "NO_CORRECTION_NEEDED"
        if normalize_review_text(human_text) == normalize_review_text(item.get("before_text", ""))
        else "CORRECTION_NEEDED"
    )


def _audit(review: dict, stage: str, action: str, now: str) -> None:
    review.setdefault("audit", []).append({"stage": stage, "action": action, "at": now})


def apply_review_action(item: dict, stage: str, action: str, text: str, now: str) -> dict:
    current_stage = review_stage(item)
    if stage != current_stage:
        raise ValueError("review_stage_mismatch")
    if current_stage == "COMPLETE":
        raise ValueError("review_already_complete")
    if action not in {"CONFIRM", "AMBIGUOUS", "SKIP"}:
        raise ValueError("unsupported_review_action")
    output = copy.deepcopy(item)
    review = output["review"]
    if action == "AMBIGUOUS":
        review["verification_status"] = "AMBIGUOUS"
        review["human_verified_text"] = None
        review["decision"] = None
        _audit(review, stage, action, now)
        return output
    if action == "SKIP":
        review["verification_status"] = "SKIPPED"
        review["human_verified_text"] = None
        review["decision"] = None
        _audit(review, stage, action, now)
        return output
    cleaned = str(text or "").strip()
    if not cleaned:
        raise ValueError("human_text_required")
    if stage == "FIRST_PASS":
        review["first_pass_text"] = cleaned
        review["verification_status"] = "PENDING_SECOND_PASS"
    elif stage == "SECOND_PASS":
        review["second_pass_text"] = cleaned
        first = review.get("first_pass_text") or ""
        if first == cleaned:
            review["passes_match"] = True
            review["match_type"] = "exact"
        elif normalize_review_text(first) == normalize_review_text(cleaned):
            review["passes_match"] = True
            review["match_type"] = "normalized"
        else:
            review["passes_match"] = False
            review["match_type"] = "substantive_difference"
            review["reconciliation_required"] = True
            review["verification_status"] = "RECONCILIATION_REQUIRED"
            _audit(review, stage, action, now)
            return output
        review["reconciliation_required"] = False
        review["verification_status"] = "HUMAN_VERIFIED"
        review["human_verified_text"] = cleaned
        review["decision"] = _decision(output, cleaned)
    elif stage == "RECONCILIATION":
        review["reconciled_text"] = cleaned
        review["human_verified_text"] = cleaned
        review["reconciliation_required"] = False
        review["verification_status"] = "HUMAN_VERIFIED"
        review["decision"] = _decision(output, cleaned)
    _audit(review, stage, action, now)
    return output


def project_review_item(item: dict) -> dict:
    """Return only fields permitted for the item's current review stage."""
    stage = review_stage(item)
    source = item.get("source") or {}
    projected = {
        "sample_id": item.get("sample_id"),
        "review_stage": stage,
        "verification_status": (item.get("review") or {}).get("verification_status"),
        "source": {
            "video_id": source.get("video_id"),
            "start_ms": source.get("start_ms"),
            "end_ms": source.get("end_ms"),
        },
        "clip_url": item.get("clip_url"),
    }
    if stage == "SECOND_PASS":
        return projected
    for field in _EVIDENCE_FIELDS:
        projected[field] = copy.deepcopy(item.get(field))
    if stage == "RECONCILIATION":
        review = item.get("review") or {}
        projected["first_pass_text"] = review.get("first_pass_text")
        projected["second_pass_text"] = review.get("second_pass_text")
    if stage == "COMPLETE":
        review = item.get("review") or {}
        projected["human_verified_text"] = review.get("human_verified_text")
        projected["decision"] = review.get("decision")
        projected["passes_match"] = review.get("passes_match")
        projected["match_type"] = review.get("match_type")
    return projected


def evaluate_review_readiness(dataset: dict) -> dict:
    samples = dataset.get("samples") or []
    verified = [
        item for item in samples
        if (item.get("review") or {}).get("verification_status") == "HUMAN_VERIFIED"
    ]
    no_correction = [
        item for item in verified
        if (item.get("review") or {}).get("decision") == "NO_CORRECTION_NEEDED"
    ]
    videos = {
        (item.get("source") or {}).get("media_sha256") for item in verified
        if (item.get("source") or {}).get("media_sha256")
    }
    reasons = []
    if len(verified) < 30:
        reasons.append("human_verified_below_30")
    if len(no_correction) < 10:
        reasons.append("no_correction_needed_below_10")
    if len(videos) < 2:
        reasons.append("source_video_count_below_2")
    return {
        "status": "BLOCKED" if reasons else "READY",
        "human_verified_count": len(verified),
        "no_correction_needed_count": len(no_correction),
        "source_video_count": len(videos),
        "ambiguous_count": sum(
            (item.get("review") or {}).get("verification_status") == "AMBIGUOUS"
            for item in samples
        ),
        "skipped_count": sum(
            (item.get("review") or {}).get("verification_status") == "SKIPPED"
            for item in samples
        ),
        "reasons": reasons,
    }
