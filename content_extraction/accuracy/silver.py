"""Conversion of retained review candidates into non-Ground-Truth Silver data."""

from .schemas import SilverValidationItem, SilverValidationSet


def _role(item: dict) -> str:
    sampling_role = str(item.get("sampling_role") or "")
    conflicts = tuple(item.get("conflicts") or ())
    if sampling_role == "control_candidate_unverified" and not conflicts:
        return "NO_CORRECTION_CONTROL"
    return "SUSPECT_CONFLICT"


def build_silver_validation_set(payload: dict) -> SilverValidationSet:
    samples = payload.get("samples") if isinstance(payload, dict) else None
    if not isinstance(samples, list) or not samples:
        raise ValueError("silver_samples_required")
    output = []
    for item in samples:
        if item.get("is_ground_truth"):
            raise ValueError("silver_item_cannot_be_ground_truth")
        source = item.get("source") or {}
        output.append(SilverValidationItem(
            sample_id=str(item.get("sample_id") or ""),
            source_video_id=str(source.get("video_id") or ""),
            media_sha256=str(source.get("media_sha256") or ""),
            start_ms=int(source.get("start_ms") or 0),
            end_ms=int(source.get("end_ms") or 0),
            before_text=str(item.get("before_text") or ""),
            validation_role=_role(item),
            provider_texts={
                str(key): str(value) for key, value in (item.get("provider_texts") or {}).items()
                if value
            },
            alternatives=tuple(str(value) for value in item.get("alternatives") or () if value),
            conflicts=tuple(item.get("conflicts") or ()),
            critical_tokens=tuple(item.get("critical_tokens") or ()),
            is_ground_truth=False,
        ))
    return SilverValidationSet(
        dataset_id="safebox181-phase1b-silver", items=tuple(output),
        is_ground_truth=False,
    )
