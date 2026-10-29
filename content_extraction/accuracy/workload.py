"""Production Quick Review workload generation from live machine results."""

from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path

from .review.clips import generate_review_clips
from .review_metrics import calculate_review_metrics
from .review_queue import build_review_queue, ImmutableReviewQueueStore, ReviewQueue


def _serialize(value):
    return value.to_dict() if hasattr(value, "to_dict") else value


def _atomic_private_json(path, payload):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


@dataclass(frozen=True)
class ReviewWorkloadResult:
    status: str
    queue_path: str
    carrier_path: str
    clip_paths: tuple[str, ...]
    metrics: dict
    audit: tuple[dict, ...] = ()
    accounting: dict = field(default_factory=dict)


def generate_queue_workload(source_video_id, samples, transcript):
    """Shared queue/metric construction for live service and frozen CLI input."""
    queue = build_review_queue(source_video_id, samples, len(str(transcript or "")))
    return {
        "queue": queue,
        "metrics": calculate_review_metrics(queue),
        "dedupe": {"before": len(tuple(samples)), "after": len(queue.items)},
    }


def build_workload_samples(bundle, correction_result, source_video_path):
    correction_by_id = {
        str(item.segment_id): item for item in correction_result.segments
    }
    rejected_by_id = {}
    for item in correction_result.rejected_candidates:
        rejected_by_id.setdefault(str(item.get("segment_id") or ""), []).append(item)
    unresolved_by_id = {}
    for item in correction_result.unresolved_conflicts:
        unresolved_by_id.setdefault(str(item.get("segment_id") or ""), []).append(item)
    rows = []
    for index, segment in enumerate(bundle.segments):
        segment_id = str(segment.segment_id)
        correction = correction_by_id.get(segment_id)
        carrier = str(getattr(correction, "final_text", "") or segment.clean_text or "")
        providers = {
            str(item.provider): str(item.text or "")
            for item in getattr(segment, "provider_evidence", ()) if item.text
        }
        rejected = rejected_by_id.get(segment_id, ())
        qwen = next((
            item for item in rejected
            if item.get("proposal_source") == "local_llm"
            or (item.get("llm_metadata") is not None)
        ), None)
        unresolved = unresolved_by_id.get(segment_id, ())
        rows.append({
            "sample_id": f"job{str(bundle.source_video_id).replace('_', '-')}-{segment_id}",
            "segment_id": segment_id,
            "source": {
                "video_id": str(bundle.source_video_id),
                "media_path": str(Path(source_video_path).resolve()),
                "duration_ms": int(bundle.duration_ms),
                "start_ms": int(segment.start_ms),
                "end_ms": int(segment.end_ms),
            },
            "before_text": carrier,
            "current_clean_raw_transcript": carrier,
            "provider_texts": providers,
            "alternatives": list(getattr(segment, "alternatives", ()) or ()),
            "conflicts": [_serialize(item) for item in getattr(segment, "conflicts", ())],
            "critical_tokens": [
                _serialize(item) for item in getattr(segment, "critical_tokens", ())
            ],
            "qwen_proposal": qwen,
            "possible_completeness_loss": any(
                "completeness" in str(item.get("reason") or "").lower()
                for item in unresolved
            ),
            "context_before": "",
            "context_after": "",
            "machine_evidence_only": True,
            "is_ground_truth": False,
        })
    for index, row in enumerate(rows):
        row["context_before"] = rows[index - 1]["before_text"] if index else ""
        row["context_after"] = rows[index + 1]["before_text"] if index + 1 < len(rows) else ""
    return tuple(rows)


class ReviewWorkloadStage:
    def __init__(
        self, runtime_root, clip_root, *, clip_runner=None,
        refinement_stage=None,
    ):
        self.runtime_root = Path(runtime_root).resolve()
        self.clip_root = Path(clip_root).resolve()
        self.clip_runner = clip_runner
        self.refinement_stage = refinement_stage

    def run(self, crm_job_id, bundle, correction_result, source_video_path):
        samples = build_workload_samples(bundle, correction_result, source_video_path)
        total_characters = len(str(correction_result.final_accurate_transcript or ""))
        provisional = generate_queue_workload(
            bundle.source_video_id, samples,
            correction_result.final_accurate_transcript,
        )["queue"]
        carriers = {
            str(item.segment_id): str(item.final_text or "")
            for item in correction_result.segments if str(item.final_text or "")
        }
        audit = ()
        accounting = {}
        if self.refinement_stage is not None:
            refined = self.refinement_stage.run(provisional, carriers)
            provisional = refined.queue
            carriers = dict(refined.carrier_segments)
            audit = tuple(refined.audit)
            accounting = dict(refined.accounting)
        carrier_path = self.runtime_root / "carriers" / f"job_{int(crm_job_id)}.json"
        _atomic_private_json(carrier_path, {
            "schema_version": "accurate-transcript-carriers-v1",
            "crm_job_id": int(crm_job_id),
            "segments": [
                {"segment_id": segment_id, "text": carriers[segment_id]}
                for segment_id in carriers
            ],
        })
        if not provisional.items:
            metrics = calculate_review_metrics(provisional)
            return ReviewWorkloadResult(
                "TRANSCRIPT_READY", "", str(carrier_path), (), metrics,
                audit, accounting,
            )

        needed = {item.review_id for item in provisional.items}
        dataset = {"samples": [dict(item) for item in samples if item["sample_id"] in needed]}
        updated, clip_map = generate_review_clips(
            dataset, self.clip_root, runner=self.clip_runner,
        )
        by_id = {item["sample_id"]: item for item in updated["samples"]}
        queue_items = tuple(replace(
            item,
            audio_clip_path=str(by_id[item.review_id].get("clip_url") or ""),
            video_clip_path=str(by_id[item.review_id].get("clip_url") or ""),
        ) for item in provisional.items)
        queue = ReviewQueue.create(
            bundle.source_video_id,
            sum(len(value) for value in carriers.values()),
            queue_items,
            provisional.auto_resolved_count,
        )
        queue_path = self.runtime_root / "queues" / f"job_{int(crm_job_id)}.json"
        ImmutableReviewQueueStore(queue_path).create(queue)
        metrics = calculate_review_metrics(queue)
        return ReviewWorkloadResult(
            "TRANSCRIPT_READY", str(queue_path), str(carrier_path),
            tuple(str(path) for path in clip_map.values()), metrics,
            audit, accounting,
        )
