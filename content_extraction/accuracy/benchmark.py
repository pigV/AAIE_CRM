"""Human Ground Truth validation and privacy-safe corpus fingerprinting."""

import hashlib
import json
from pathlib import Path


def validate_ground_truth(payload: dict, media_path: Path | None = None) -> None:
    if payload.get("schema_version") != "accurate-ground-truth-v1":
        raise ValueError("unsupported_ground_truth_schema")
    if payload.get("is_ground_truth") is not True:
        raise ValueError("is_ground_truth_required")
    if payload.get("machine_generated") is not False:
        raise ValueError("machine_generated_reference_forbidden")
    media = payload.get("media") or {}
    if not media.get("rights_confirmed"):
        raise ValueError("rights_confirmation_required")
    digest = str(media.get("sha256", ""))
    if len(digest) != 64:
        raise ValueError("media_sha256_required")
    verification = payload.get("verification") or {}
    if verification.get("status") != "HUMAN_VERIFIED":
        raise ValueError("human_verified_status_required")
    if not (verification.get("first_pass_completed") and verification.get("second_pass_completed")):
        raise ValueError("two_pass_human_verification_required")
    if verification.get("verification_method") != "two_pass_audio_video_review":
        raise ValueError("invalid_verification_method")
    if not payload.get("human_verified_transcript"):
        raise ValueError("human_verified_transcript_required")
    if media_path is not None:
        actual = hashlib.sha256(Path(media_path).read_bytes()).hexdigest()
        if actual != digest:
            raise ValueError("media_sha256_mismatch")


def corpus_fingerprint(samples: tuple[dict, ...]) -> str:
    canonical = []
    for item in sorted(samples, key=lambda value: str(value.get("sample_id", ""))):
        validate_ground_truth(item)
        canonical.append(item)
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_phase_one_span_set(payload: dict) -> None:
    samples = payload.get("samples") or []
    if not 30 <= len(samples) <= 50:
        raise ValueError("phase_one_requires_30_to_50_spans")
    if payload.get("status") != "HUMAN_VERIFIED":
        raise ValueError("insufficient_human_verified_ground_truth")
    for item in samples:
        if item.get("machine_evidence_only") or item.get("is_ground_truth") is not True:
            raise ValueError("machine_evidence_cannot_be_ground_truth")
        review = item.get("review") or {}
        if review.get("decision") not in {"CORRECTION_NEEDED", "NO_CORRECTION_NEEDED"}:
            raise ValueError("ambiguous_or_pending_sample")
        if not review.get("human_verified_text"):
            raise ValueError("missing_human_verified_text")
        if not (review.get("first_pass_completed") and review.get("second_pass_completed")):
            raise ValueError("two_pass_human_verification_required")
