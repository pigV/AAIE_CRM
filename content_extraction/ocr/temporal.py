from dataclasses import dataclass, field

from .normalization import normalize_for_comparison


@dataclass(frozen=True)
class TemporalOCRSegment:
    start_ms: int
    end_ms: int
    text: str
    frame_ids: tuple
    fusion_statuses: tuple
    conflicts: tuple


@dataclass(frozen=True)
class OCRFusedTranscript:
    segments: tuple
    frame_fusions: tuple = ()
    source_video_id: str = ""
    duration_ms: int = 0
    timebase: str = "milliseconds"
    segment_roles: dict = field(default_factory=dict)
    evidence_decisions: dict = field(default_factory=dict)
    excluded_frame_ids: tuple = ()

    @property
    def text(self):
        return "\n".join(segment.text for segment in self.segments if segment.text)

    def to_dict(self):
        return {
            "text": self.text,
            "source_video_id": self.source_video_id,
            "duration_ms": int(self.duration_ms),
            "timebase": self.timebase,
            "evidence_decisions": {
                str(frame_id): (
                    decision.to_dict() if hasattr(decision, "to_dict")
                    else dict(decision)
                )
                for frame_id, decision in self.evidence_decisions.items()
            },
            "excluded_frame_ids": list(self.excluded_frame_ids),
            "segments": [
                {
                    "start_ms": item.start_ms,
                    "end_ms": item.end_ms,
                    "text": item.text,
                    "frame_ids": list(item.frame_ids),
                    "fusion_statuses": list(item.fusion_statuses),
                    "conflicts": [
                        {"type": conflict.type, "values": conflict.values}
                        for conflict in item.conflicts
                    ],
                }
                for item in self.segments
            ],
        }


def _append_unique(values, additions):
    output = list(values)
    for item in additions:
        if item not in output:
            output.append(item)
    return tuple(output)


def _overlap_size(first, second, minimum=2):
    limit = min(len(first), len(second))
    for size in range(limit, minimum - 1, -1):
        if first[-size:] == second[:size]:
            return size
    return 0


def _extend(current, frame, text):
    return TemporalOCRSegment(
        current.start_ms,
        max(current.end_ms, frame.end_ms),
        text,
        current.frame_ids + (frame.frame_id,),
        _append_unique(current.fusion_statuses, (frame.fusion_status,)),
        _append_unique(current.conflicts, frame.conflicts),
    )


def merge_fused_frames(
    frames,
    *,
    source_video_id="",
    duration_ms=0,
    timebase="milliseconds",
    segment_roles=None,
    evidence_decisions=None,
):
    all_frames = tuple(sorted(
        frames,
        key=lambda frame: (frame.start_ms, frame.end_ms, frame.frame_id),
    ))
    evidence_decisions = dict(evidence_decisions or {})
    excluded = tuple(
        frame.frame_id for frame in all_frames
        if frame.frame_id in evidence_decisions
        and not evidence_decisions[frame.frame_id].accepted
    )
    ordered = tuple(
        frame for frame in all_frames
        if frame.fused_text
        and (
            frame.frame_id not in evidence_decisions
            or evidence_decisions[frame.frame_id].accepted
        )
    )
    segments = []
    for frame in ordered:
        if not segments:
            segments.append(TemporalOCRSegment(
                frame.start_ms, frame.end_ms, frame.fused_text,
                (frame.frame_id,), (frame.fusion_status,), tuple(frame.conflicts),
            ))
            continue
        current = segments[-1]
        current_normalized = normalize_for_comparison(current.text)
        frame_normalized = normalize_for_comparison(frame.fused_text)
        if current_normalized == frame_normalized:
            segments[-1] = _extend(current, frame, current.text)
            continue
        if frame_normalized.startswith(current_normalized):
            segments[-1] = _extend(current, frame, frame.fused_text)
            continue
        if current_normalized.startswith(frame_normalized):
            segments[-1] = _extend(current, frame, current.text)
            continue
        overlap = _overlap_size(current.text, frame.fused_text)
        if overlap:
            segments[-1] = _extend(
                current, frame, current.text + frame.fused_text[overlap:]
            )
            continue
        segments.append(TemporalOCRSegment(
            frame.start_ms, frame.end_ms, frame.fused_text,
            (frame.frame_id,), (frame.fusion_status,), tuple(frame.conflicts),
        ))
    return OCRFusedTranscript(
        tuple(segments),
        frame_fusions=all_frames,
        source_video_id=str(source_video_id or ""),
        duration_ms=int(duration_ms or 0),
        timebase=str(timebase or "milliseconds"),
        segment_roles=dict(segment_roles or {}),
        evidence_decisions=evidence_decisions,
        excluded_frame_ids=excluded,
    )
