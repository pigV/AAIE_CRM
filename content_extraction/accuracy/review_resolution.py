"""Separate atomic human resolutions and bounded patch application."""

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import threading

from ..shared.recognition_text import levenshtein_distance
from .completeness import CompletenessGuard, validate_corrected_segment
from .minimal_edit import MinimalEditGuard, apply_non_overlapping_candidates
from .schemas import CarrierSelection, CorrectionCandidate


@dataclass(frozen=True)
class ReviewResolution:
    review_id: str
    source_queue_hash: str
    selected_text: str
    action: str
    status: str
    resolved_at: str
    reviewer_source: str
    original_candidate_references: tuple[str, ...] = ()
    revision: int = 1
    resolution_source: str = "HUMAN_REVIEW"
    ground_truth: bool = False

    def __post_init__(self):
        if self.status not in {"CONFIRMED", "EDITED", "AMBIGUOUS"}:
            raise ValueError("invalid_resolution_status")
        if self.resolution_source != "HUMAN_REVIEW" or self.ground_truth:
            raise ValueError("invalid_human_review_provenance")

    def to_dict(self):
        payload = asdict(self)
        payload["original_candidate_references"] = list(self.original_candidate_references)
        payload["ground_truth"] = False
        return payload

    @classmethod
    def from_dict(cls, payload):
        values = dict(payload)
        values["original_candidate_references"] = tuple(values.get("original_candidate_references") or ())
        return cls(**values)


class ResolutionStore:
    def __init__(self, path, queue):
        self.path = Path(path).resolve()
        self.queue = queue
        self._lock = threading.RLock()

    def _empty(self):
        return {
            "schema_version": "quick-review-resolution-v1",
            "source_queue_hash": self.queue.queue_hash,
            "revision": 0,
            "private": True,
            "is_ground_truth": False,
            "resolutions": [],
        }

    def load(self):
        with self._lock:
            if not self.path.exists():
                return self._empty()
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("source_queue_hash") != self.queue.queue_hash:
                raise ValueError("STALE_RESOLUTION")
            if payload.get("is_ground_truth"):
                raise ValueError("resolution_cannot_be_ground_truth")
            return payload

    def _write(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
            self.path.chmod(0o600)
        finally:
            if temporary.exists():
                temporary.unlink()

    def submit(self, review_id, source_queue_hash, expected_revision, action,
               selected_text, candidate_references, reviewer_source, now):
        with self._lock:
            if source_queue_hash != self.queue.queue_hash:
                raise ValueError("STALE_RESOLUTION")
            if not any(item.review_id == review_id for item in self.queue.items):
                raise KeyError("unknown_review_id")
            if action not in {"CONFIRMED", "EDITED", "AMBIGUOUS"}:
                raise ValueError("invalid_resolution_action")
            cleaned = str(selected_text or "").strip()
            if action != "AMBIGUOUS" and not cleaned:
                raise ValueError("selected_text_required")
            payload = self.load()
            revision = int(payload.get("revision", 0))
            existing = next((
                ReviewResolution.from_dict(item) for item in payload["resolutions"]
                if item.get("review_id") == review_id
            ), None)
            if int(expected_revision) != revision:
                raise ValueError("revision_conflict")
            if existing is not None:
                same = (
                    existing.action == action and existing.selected_text == cleaned
                    and existing.status == action
                )
                if same:
                    return existing
                raise ValueError("resolution_conflict")
            resolution = ReviewResolution(
                review_id=str(review_id), source_queue_hash=self.queue.queue_hash,
                selected_text=cleaned, action=action, status=action,
                resolved_at=str(now), reviewer_source=str(reviewer_source),
                original_candidate_references=tuple(candidate_references or ()),
                revision=revision + 1,
            )
            payload["revision"] = revision + 1
            payload["resolutions"].append(resolution.to_dict())
            self._write(payload)
            return resolution


def _human_candidate(item, selected_text):
    return CorrectionCandidate(
        candidate_id=f"human:{item.review_id}", segment_id=item.segment_id,
        carrier_fingerprint=CarrierSelection(item.segment_id, item.carrier_text, "quick_review").fingerprint,
        before_span=item.suspect_span, after_span=selected_text,
        start_offset=item.start_offset, end_offset=item.end_offset,
        correction_type=("NUMBER_CORRECTION" if item.critical_token_type else "TYPO_CORRECTION"),
        proposal_source="deterministic", alternatives=tuple(alt.text for alt in item.alternatives),
        evidence_refs=("human_review",), evidence_strength="HUMAN_AUTHORIZED",
        generation_reasons=("quick_review_resolution",),
        selection_reason="human_confirmed_local_span", confidence_level="HUMAN_CONFIRMED",
        affects_critical_token=bool(item.critical_token_type),
        lexical_edit_distance=levenshtein_distance(item.suspect_span, selected_text),
    )


def apply_human_resolutions(queue, carrier_segments, resolutions):
    resolution_map = {item.review_id: item for item in resolutions}
    output = dict(carrier_segments)
    audit = []
    unresolved = []
    unresolved_p0 = False
    blocking_unresolved = False
    for item in queue.items:
        resolution = resolution_map.get(item.review_id)
        if resolution is None:
            unresolved.append({"review_id": item.review_id, "risk_level": item.risk_level, "status": "PENDING"})
            unresolved_p0 |= item.risk_level == "P0"
            blocking_unresolved = True
            continue
        if resolution.source_queue_hash != queue.queue_hash:
            raise ValueError("STALE_RESOLUTION")
        carrier_text = output.get(item.segment_id)
        if carrier_text != item.carrier_text:
            raise ValueError("carrier_text_mismatch")
        if resolution.status == "AMBIGUOUS":
            unresolved.append({"review_id": item.review_id, "risk_level": item.risk_level, "status": "AMBIGUOUS"})
            unresolved_p0 |= item.risk_level == "P0"
            blocking_unresolved = True
            continue
        candidate = _human_candidate(item, resolution.selected_text)
        carrier = CarrierSelection(item.segment_id, carrier_text, "quick_review")
        minimal = MinimalEditGuard().evaluate(candidate, carrier, None)
        completeness = CompletenessGuard().evaluate(candidate, carrier, None)
        if minimal.status != "PASS" or completeness.status != "PASS":
            unresolved.append({
                "review_id": item.review_id, "risk_level": item.risk_level,
                "status": "GUARD_REJECTED",
            })
            unresolved_p0 |= item.risk_level == "P0"
            blocking_unresolved = True
            continue
        patched = apply_non_overlapping_candidates(carrier_text, (candidate,))
        final_check = validate_corrected_segment(carrier, patched, (candidate,))
        if final_check.status != "PASS":
            unresolved.append({
                "review_id": item.review_id, "risk_level": item.risk_level,
                "status": "GUARD_REJECTED",
            })
            unresolved_p0 |= item.risk_level == "P0"
            blocking_unresolved = True
            continue
        output[item.segment_id] = patched
        audit.append({
            "review_id": item.review_id, "segment_id": item.segment_id,
            "before_span": item.suspect_span, "after_span": resolution.selected_text,
            "start_offset": item.start_offset, "end_offset": item.end_offset,
            "resolution_source": "HUMAN_REVIEW", "ground_truth": False,
            "critical_token_authorization": "HUMAN_REVIEW" if item.critical_token_type else None,
            "minimal_edit": minimal.status, "completeness": final_check.status,
        })
    if unresolved_p0:
        status = "ACCURATE_TRANSCRIPT_INCOMPLETE"
    elif blocking_unresolved:
        status = "REVIEW_REQUIRED"
    elif queue.items:
        status = "HUMAN_REVIEW_COMPLETE"
    else:
        status = "ACCURATE_TRANSCRIPT_COMPLETE"
    return {"status": status, "segments": output, "audit": audit, "unresolved": unresolved}
