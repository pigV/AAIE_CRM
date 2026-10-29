"""Prepare private multi-video review candidates without declaring Ground Truth."""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from ...shared.critical_tokens import extract_critical_tokens


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _review_state() -> dict:
    return {
        "first_pass_text": None, "second_pass_text": None,
        "passes_match": None, "match_type": None,
        "reconciliation_required": False, "reconciled_text": None,
        "human_verified_text": None,
        "verification_status": "PENDING_FIRST_PASS",
        "decision": None, "audit": [],
    }


def _critical_payload(text: str) -> list[dict]:
    return [item.to_dict() for item in extract_critical_tokens(text)]


def _provider_text_for_frames(frame_ids, frame_map, provider):
    values = []
    for frame_id in frame_ids:
        for result in (frame_map.get(frame_id) or {}).get("provider_results") or []:
            text = str(result.get("text") or "").strip()
            if result.get("provider") == provider and text and text not in values:
                values.append(text)
    return " / ".join(values) or None


def _job18_items(payload, media_path: Path, count: int) -> list[dict]:
    segments = list((payload.get("cross_modal_result") or {}).get("segments") or [])
    ranked = sorted(segments, key=lambda item: (
        not bool(item.get("conflicts")),
        item.get("confidence_level") == "HIGH",
        -len(item.get("alternatives") or []),
        int(item.get("start_ms", 0)),
    ))[:count]
    ranked.sort(key=lambda item: int(item.get("start_ms", 0)))
    frame_map = {item.get("frame_id"): item for item in payload.get("ocr_frame_results") or []}
    media_hash = _hash_file(media_path)
    duration_ms = int((payload.get("cross_modal_result") or {}).get("duration_ms") or 0)
    output = []
    for index, segment in enumerate(ranked, 1):
        asr_values = (segment.get("asr_provenance") or {}).get("provider_texts") or {}
        frame_ids = (segment.get("ocr_provenance") or {}).get("frame_ids") or []
        start_ms, end_ms = int(segment["start_ms"]), int(segment["end_ms"])
        output.append({
            "sample_id": f"review-job18-{index:03d}",
            "source": {
                "video_id": "job_18", "media_sha256": media_hash,
                "media_path": str(media_path.resolve()), "duration_ms": duration_ms,
                "start_ms": start_ms, "end_ms": end_ms,
            },
            "before_text": segment.get("clean_text") or "",
            "current_clean_raw_transcript": segment.get("clean_text") or "",
            "provider_texts": {
                "funasr": asr_values.get("funasr"),
                "whispercpp": asr_values.get("whispercpp"),
                "paddle": _provider_text_for_frames(frame_ids, frame_map, "paddle"),
                "apple_vision": _provider_text_for_frames(frame_ids, frame_map, "apple_vision"),
                "legacy_asr": None,
            },
            "alternatives": list(segment.get("alternatives") or []),
            "conflicts": list(segment.get("conflicts") or []),
            "critical_tokens": list(segment.get("critical_tokens") or []),
            "sampling_role": "machine_evidence_difficult_candidate",
            "machine_evidence_only": True, "is_ground_truth": False,
            "review": _review_state(),
        })
    return output


def _evenly_spaced(values, count):
    if len(values) <= count:
        return list(values)
    if count == 1:
        return [values[len(values) // 2]]
    indices = [round(index * (len(values) - 1) / (count - 1)) for index in range(count)]
    return [values[index] for index in dict.fromkeys(indices)]


def _legacy_items(record: dict, media_path: Path, count: int) -> list[dict]:
    segments = json.loads(record.get("speech_transcript_with_timestamps") or "[]")
    valid = [
        item for item in segments
        if str(item.get("text") or "").strip()
        and float(item.get("end", 0)) > float(item.get("start", 0))
    ]
    selected = _evenly_spaced(valid, count)
    media_hash = _hash_file(media_path)
    duration_ms = int(max((float(item["end"]) for item in valid), default=0) * 1000)
    job_id = int(record["id"])
    output = []
    for index, segment in enumerate(selected, 1):
        text = str(segment["text"]).strip()
        tokens = _critical_payload(text)
        output.append({
            "sample_id": f"review-job{job_id}-{index:03d}",
            "source": {
                "video_id": f"job_{job_id}", "media_sha256": media_hash,
                "media_path": str(media_path.resolve()), "duration_ms": duration_ms,
                "start_ms": int(round(float(segment["start"]) * 1000)),
                "end_ms": int(round(float(segment["end"]) * 1000)),
            },
            "before_text": text, "current_clean_raw_transcript": text,
            "provider_texts": {
                "funasr": None, "whispercpp": None, "paddle": None,
                "apple_vision": None, "legacy_asr": text,
            },
            "alternatives": [text], "conflicts": [], "critical_tokens": tokens,
            "sampling_role": (
                "critical_or_mixed_text_candidate" if tokens
                else "control_candidate_unverified"
            ),
            "machine_evidence_only": True, "is_ground_truth": False,
            "review": _review_state(),
        })
    return output


def prepare_multivideo_review_set(
    job18_payload: dict,
    job18_media_path,
    legacy_records: list[dict],
    legacy_media_paths: dict[int, Path],
    job18_count: int = 15,
    legacy_count: int = 10,
) -> dict:
    job18_path = Path(job18_media_path).resolve()
    all_paths = [job18_path] + [Path(legacy_media_paths[int(item["id"])]).resolve()
                                    for item in legacy_records]
    if not all(path.is_file() for path in all_paths):
        raise FileNotFoundError("source_video_missing")
    hashes = [_hash_file(path) for path in all_paths]
    if len(set(hashes)) != len(hashes):
        raise ValueError("duplicate_source_video")
    samples = _job18_items(job18_payload, job18_path, job18_count)
    for record in sorted(legacy_records, key=lambda item: int(item["id"])):
        samples.extend(_legacy_items(
            record, Path(legacy_media_paths[int(record["id"])]).resolve(), legacy_count,
        ))
    distribution = Counter(item["source"]["video_id"] for item in samples)
    return {
        "schema_version": "ground-truth-review-v1",
        "dataset_id": "safebox181-phase1-multivideo",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "PENDING_HUMAN_VERIFICATION",
        "is_ground_truth": False, "machine_generated": False,
        "sample_count": len(samples),
        "source_distribution": dict(distribution),
        "warning": "No item is Ground Truth until independent review completes.",
        "samples": samples,
    }


def load_legacy_records(database_path, job_ids) -> list[dict]:
    path = Path(database_path).resolve()
    normalized_ids = tuple(int(value) for value in job_ids)
    if not normalized_ids:
        raise ValueError("legacy_job_ids_required")
    placeholders = ",".join("?" for _ in normalized_ids)
    uri = f"file:{path}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"SELECT id, speech_transcript_with_timestamps "
            f"FROM content_extraction_jobs WHERE id IN ({placeholders}) ORDER BY id",
            normalized_ids,
        ).fetchall()
    if len(rows) != len(normalized_ids):
        raise ValueError("legacy_job_record_missing")
    return [dict(row) for row in rows]
