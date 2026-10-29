import hashlib
from dataclasses import dataclass

from ..models import SubtitleSegment


@dataclass(frozen=True)
class SubtitleObservation:
    timestamp_ms: int
    crop_path: str
    image_hash: str
    perceptual_signature: int
    content_score: float


def _similarity(first, second):
    differing_bits = (int(first) ^ int(second)).bit_count()
    return 1.0 - differing_bits / 64.0


def _make_segment(run_id, members, end_ms, preprocessing_version):
    representative = max(members, key=lambda item: item.content_score)
    start_ms = members[0].timestamp_ms
    identity = hashlib.sha256(
        f"{run_id}:{start_ms}:{end_ms}:{representative.image_hash}:{preprocessing_version}".encode()
    ).hexdigest()[:24]
    return SubtitleSegment(
        identity, run_id, start_ms, end_ms, representative.crop_path,
        representative.image_hash, preprocessing_version,
    )


def build_subtitle_segments(
    run_id, observations, duration_ms, preprocessing_version,
    similarity_threshold=0.92,
):
    observations = tuple(sorted(observations, key=lambda item: item.timestamp_ms))
    if not observations:
        return ()
    if duration_ms <= observations[-1].timestamp_ms:
        raise ValueError("duration must extend past the final observation")

    groups = [[observations[0]]]
    for observation in observations[1:]:
        previous = groups[-1][-1]
        same = observation.image_hash == previous.image_hash
        near = _similarity(observation.perceptual_signature, previous.perceptual_signature)
        if same or near >= similarity_threshold:
            groups[-1].append(observation)
        else:
            groups.append([observation])

    segments = []
    for index, members in enumerate(groups):
        end_ms = groups[index + 1][0].timestamp_ms if index + 1 < len(groups) else duration_ms
        segments.append(_make_segment(run_id, members, end_ms, preprocessing_version))
    return tuple(segments)


def classify_subtitle_segments(segments, evidence_by_path):
    """Classify temporal candidates without inspecting or rewriting OCR text."""

    output = {}
    for segment in segments:
        evidence = evidence_by_path.get(segment.representative_path)
        if evidence is None:
            output[segment.id] = "unknown"
            continue
        if evidence.classification in {"screen_document", "ui"}:
            output[segment.id] = evidence.classification
            continue
        if not evidence.accepted:
            output[segment.id] = "unknown"
            continue
        duration_ms = segment.end_ms - segment.start_ms
        if duration_ms >= 12000:
            output[segment.id] = "fixed_overlay"
        elif evidence.region.height_ratio >= 0.18 and duration_ms >= 3000:
            output[segment.id] = "title"
        else:
            output[segment.id] = "spoken_subtitle"
    return output
