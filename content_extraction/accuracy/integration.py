"""SAFEBOX 181 service boundary for Accurate Transcript and Quick Review."""

from dataclasses import dataclass
import json
from pathlib import Path

from .review_metrics import calculate_review_metrics
from .review_queue import ImmutableReviewQueueStore
from .review_resolution import (
    ResolutionStore,
    ReviewResolution,
    apply_human_resolutions,
)
from .candidate_projection import project_candidate_spans
from .spark_arbiter_queue import map_review_candidates


_FINAL_STATUSES = {
    "TRANSCRIPT_READY",
    "ACCURATE_TRANSCRIPT_COMPLETE", "HUMAN_REVIEW_COMPLETE",
    "AUTO_COMPLETE",  # Read-only compatibility for older frozen artifacts.
}
_MEDIA_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".wav": "audio/wav"}


@dataclass(frozen=True)
class ReviewMedia:
    content_type: str
    body: bytes


class AccurateTranscriptCoordinator:
    """Projects private runtime artifacts without exposing their paths."""

    def __init__(self, runtime_root, clip_root=None, max_media_bytes=16 * 1024 * 1024):
        self.runtime_root = Path(runtime_root).resolve()
        self.clip_root = Path(clip_root or self.runtime_root / "clips").resolve()
        self.max_media_bytes = int(max_media_bytes)

    def _path(self, kind, crm_job_id):
        return self.runtime_root / kind / f"job_{int(crm_job_id)}.json"

    def _queue(self, crm_job_id):
        path = self._path("queues", crm_job_id)
        if not path.is_file():
            return None
        return ImmutableReviewQueueStore(path).load()

    def _carriers(self, crm_job_id):
        path = self._path("carriers", crm_job_id)
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            payload.get("schema_version") != "accurate-transcript-carriers-v1"
            or int(payload.get("crm_job_id", -1)) != int(crm_job_id)
        ):
            raise ValueError("invalid_carrier_manifest")
        ordered = []
        seen = set()
        for item in payload.get("segments") or ():
            segment_id = str(item.get("segment_id") or "")
            if not segment_id or segment_id in seen:
                raise ValueError("invalid_carrier_manifest")
            seen.add(segment_id)
            ordered.append((segment_id, str(item.get("text") or "")))
        if not ordered:
            raise ValueError("invalid_carrier_manifest")
        return tuple(ordered)

    @staticmethod
    def _message(status, review_items):
        return {
            "TRANSCRIPT_READY": (
                "文案已生成，已标出需要留意的位置"
                if int(review_items) else "文案已生成"
            ),
            "ACCURATE_TRANSCRIPT_COMPLETE": "文案已完成",
            "AUTO_COMPLETE": "文案已完成",
            "HUMAN_REVIEW_COMPLETE": "确认完成",
            "REVIEW_REQUIRED": f"有 {int(review_items)} 处需要确认",
            "EXCESSIVE_REVIEW_REQUIRED": f"有 {int(review_items)} 处需要确认",
            "ACCURATE_TRANSCRIPT_INCOMPLETE": "当前无法生成准确文案",
        }[status]

    def _projection(self, status, metrics=None, final_text="", error_code="", audit=(),
                    unresolved=(), uncertainty_metadata=()):
        metrics = dict(metrics or {})
        final_available = status in _FINAL_STATUSES and bool(final_text)
        uncertainty_metadata = list(uncertainty_metadata or ())
        return {
            "mode": "LATEST",
            "product_name": "Accurate Transcript",
            "status": status,
            "readiness": (
                "WITH_UNCERTAINTIES" if uncertainty_metadata else "CLEAN"
            ) if status == "TRANSCRIPT_READY" else "",
            "review_items": int(metrics.get("review_items", 0)),
            "review_characters": int(metrics.get("review_characters", 0)),
            "review_ratio": float(metrics.get("review_ratio", 0.0)),
            "priority_counts": dict(metrics.get("priority_counts") or {
                "P0": 0, "P1": 0, "P2": 0, "P3": 0,
            }),
            "review_completed": int(metrics.get("review_completed", 0)),
            "review_pending": int(metrics.get("review_pending", 0)),
            "historical_limited_review_gate": str(
                metrics.get("historical_limited_review_gate") or "PASS"
            ),
            "final_available": final_available,
            "final_accurate_transcript": final_text if final_available else "",
            "user_message": self._message(status, metrics.get("review_items", 0)),
            "error_code": str(error_code or ""),
            "audit": list(audit),
            "unresolved": list(unresolved),
            "uncertainty_metadata": uncertainty_metadata,
        }

    @staticmethod
    def _uncertainty_metadata(queue, carriers, final_segments, unresolved, audit):
        unresolved_ids = {str(item.get("review_id") or "") for item in unresolved}
        audit_by_segment = {}
        for item in audit:
            audit_by_segment.setdefault(str(item.get("segment_id") or ""), []).append(item)
        segment_offsets = {}
        cursor = 0
        for segment_id, _text in carriers:
            segment_offsets[segment_id] = cursor
            cursor += len(str(final_segments.get(segment_id, ""))) + 1
        output = []
        for item in queue.items:
            if item.review_id not in unresolved_ids:
                continue
            local_start = int(item.start_offset)
            for patch in audit_by_segment.get(item.segment_id, ()):
                if int(patch.get("end_offset", 0)) <= int(item.start_offset):
                    local_start += len(str(patch.get("after_span") or "")) - len(
                        str(patch.get("before_span") or "")
                    )
            text = str(final_segments.get(item.segment_id, item.carrier_text))
            local_end = local_start + len(item.suspect_span)
            if text[local_start:local_end] != item.suspect_span:
                matches = [index for index in range(len(text)) if text.startswith(item.suspect_span, index)]
                if len(matches) == 1:
                    local_start = matches[0]
                    local_end = local_start + len(item.suspect_span)
                else:
                    local_start, local_end = 0, len(text)
            candidates = []
            for value in (item.suspect_span, *(alt.text for alt in item.alternatives)):
                value = str(value or "")
                if value and value not in candidates:
                    candidates.append(value)
            output.append({
                "review_id": item.review_id,
                "segment_id": item.segment_id,
                "start_offset": segment_offsets[item.segment_id] + local_start,
                "end_offset": segment_offsets[item.segment_id] + local_end,
                "current_text": text[local_start:local_end],
                "candidate_texts": candidates,
                "reason": list(item.reason_for_review),
                "evidence_summary": {
                    "ocr": sorted(str(key) for key, value in item.ocr_evidence.items() if value),
                    "asr": sorted(str(key) for key, value in item.asr_evidence.items() if value),
                },
                "risk_type": item.risk_level,
            })
        return output

    def status_for(self, crm_job_id):
        try:
            queue = self._queue(crm_job_id)
            carriers = self._carriers(crm_job_id)
        except (OSError, ValueError, json.JSONDecodeError):
            return self._projection(
                "ACCURATE_TRANSCRIPT_INCOMPLETE",
                error_code="ACCURATE_TRANSCRIPT_RUNTIME_INVALID",
            )
        if queue is None:
            if carriers is None:
                return self._projection(
                    "ACCURATE_TRANSCRIPT_INCOMPLETE",
                    error_code="ACCURATE_TRANSCRIPT_NOT_READY",
                )
            return self._projection(
                "TRANSCRIPT_READY",
                final_text="\n".join(text for _segment_id, text in carriers),
            )

        metrics = calculate_review_metrics(queue)
        resolution_path = self._path("resolutions", crm_job_id)
        try:
            state = ResolutionStore(resolution_path, queue).load()
            resolutions = tuple(
                ReviewResolution.from_dict(item) for item in state.get("resolutions") or ()
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return self._projection(
                "ACCURATE_TRANSCRIPT_INCOMPLETE", metrics,
                error_code="CORRUPT_RESOLUTION",
            )
        completed = len(resolutions)
        metrics.update({
            "review_completed": completed,
            "review_pending": max(0, len(queue.items) - completed),
        })
        if carriers is None:
            return self._projection(
                "ACCURATE_TRANSCRIPT_INCOMPLETE", metrics,
                error_code="CARRIER_TRANSCRIPT_MISSING",
            )
        carrier_map = dict(carriers)
        try:
            applied = apply_human_resolutions(queue, carrier_map, resolutions)
        except (KeyError, ValueError):
            return self._projection(
                "ACCURATE_TRANSCRIPT_INCOMPLETE", metrics,
                error_code="HUMAN_PATCH_REJECTED",
            )
        final_text = "\n".join(
            applied["segments"].get(segment_id, text)
            for segment_id, text in carriers
        )
        uncertainties = self._uncertainty_metadata(
            queue, carriers, applied["segments"], applied["unresolved"], applied["audit"],
        )
        return self._projection(
            "TRANSCRIPT_READY", metrics, final_text=final_text,
            audit=applied["audit"], unresolved=applied["unresolved"],
            uncertainty_metadata=uncertainties,
        )

    def review_for(self, crm_job_id):
        queue = self._queue(crm_job_id)
        if queue is None:
            raise KeyError("REVIEW_NOT_AVAILABLE")
        state = ResolutionStore(self._path("resolutions", crm_job_id), queue).load()
        resolution_map = {
            str(item.get("review_id")): item for item in state.get("resolutions") or ()
        }
        items = []
        for item in queue.items:
            resolution = resolution_map.get(item.review_id)
            clip_id = Path(item.video_clip_path or item.audio_clip_path).name
            localized_alternatives = []
            seen_alternatives = set()
            for candidate in map_review_candidates(item):
                if candidate.text == item.suspect_span:
                    continue
                projection = project_candidate_spans(item.suspect_span, candidate.text)
                substantive = tuple(projection.substantive_spans)
                if projection.status != "PROJECTED" or len(substantive) != 1:
                    continue
                value = substantive[0].after_span
                if not value or value in seen_alternatives:
                    continue
                seen_alternatives.add(value)
                localized_alternatives.append({
                    "text": value,
                    "sources": list(candidate.sources),
                    "reason": candidate.reason,
                })
            items.append({
                "review_id": item.review_id,
                "segment_id": item.segment_id,
                "start_ms": item.start_ms,
                "end_ms": item.end_ms,
                "context_before": item.context_before,
                "carrier_text": item.carrier_text,
                "context_after": item.context_after,
                "suspect_span": item.suspect_span,
                "start_offset": item.start_offset,
                "end_offset": item.end_offset,
                "alternatives": localized_alternatives,
                "reason_for_review": list(item.reason_for_review),
                "risk_level": item.risk_level,
                "critical_token_type": item.critical_token_type,
                "evidence": {
                    "ocr": dict(item.ocr_evidence),
                    "asr": dict(item.asr_evidence),
                },
                "clip_id": clip_id,
                "status": str((resolution or {}).get("status") or "PENDING"),
                "selected_text": str((resolution or {}).get("selected_text") or ""),
            })
        return {
            "mode": "LATEST",
            "queue_hash": queue.queue_hash,
            "revision": int(state.get("revision", 0)),
            "source_video_id": queue.source_video_id,
            "items": items,
        }

    def submit_review(self, crm_job_id, review_id, source_queue_hash,
                      expected_revision, action, selected_text,
                      candidate_references, reviewer_source, now):
        queue = self._queue(crm_job_id)
        if queue is None:
            raise KeyError("REVIEW_NOT_AVAILABLE")
        store = ResolutionStore(self._path("resolutions", crm_job_id), queue)
        store.submit(
            str(review_id), str(source_queue_hash), int(expected_revision),
            str(action), str(selected_text), tuple(candidate_references or ()),
            str(reviewer_source), str(now),
        )
        return self.status_for(crm_job_id)

    def media_for(self, crm_job_id, clip_id):
        projection = self.review_for(crm_job_id)
        allowed = {item["clip_id"] for item in projection["items"] if item["clip_id"]}
        name = str(clip_id or "")
        if Path(name).name != name or name not in allowed:
            raise KeyError("MEDIA_NOT_FOUND")
        path = (self.clip_root / name).resolve()
        if path.parent != self.clip_root or path.suffix.lower() not in _MEDIA_TYPES:
            raise KeyError("MEDIA_NOT_FOUND")
        try:
            if path.stat().st_size > self.max_media_bytes:
                raise ValueError("MEDIA_TOO_LARGE")
            body = path.read_bytes()
        except OSError as exc:
            raise KeyError("MEDIA_NOT_FOUND") from exc
        return ReviewMedia(_MEDIA_TYPES[path.suffix.lower()], body)
