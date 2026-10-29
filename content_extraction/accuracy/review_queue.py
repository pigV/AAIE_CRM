"""Deterministic, immutable Review Queue contracts for SAFEBOX 181."""

from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .candidate_projection import project_candidate_spans


_PRIORITY = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_P0_TYPES = {
    "AGE", "DATE", "MONEY", "NUMBER", "PERCENTAGE", "PHONE", "VISA_CLASS",
    "YEAR",
}


@dataclass(frozen=True)
class ReviewAlternative:
    text: str
    sources: tuple[str, ...] = ()
    reason: str = "machine_alternative"

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ReviewRequiredItem:
    review_id: str
    segment_id: str
    start_ms: int
    end_ms: int
    context_before: str
    carrier_text: str
    context_after: str
    suspect_span: str
    start_offset: int
    end_offset: int
    alternatives: tuple[ReviewAlternative, ...]
    critical_token_type: str | None
    ocr_evidence: dict
    asr_evidence: dict
    qwen_proposal: dict | None
    reason_for_review: tuple[str, ...]
    risk_level: str
    audio_clip_path: str
    video_clip_path: str
    status: str = "PENDING"
    selected_text: str = ""

    def __post_init__(self):
        if self.risk_level not in _PRIORITY:
            raise ValueError("invalid_review_priority")
        if not 0 <= self.start_offset <= self.end_offset <= len(self.carrier_text):
            raise ValueError("invalid_suspect_offsets")
        if self.carrier_text[self.start_offset:self.end_offset] != self.suspect_span:
            raise ValueError("suspect_span_mismatch")

    def to_dict(self):
        payload = asdict(self)
        payload["alternatives"] = [item.to_dict() for item in self.alternatives]
        payload["reason_for_review"] = list(self.reason_for_review)
        return payload

    @classmethod
    def from_dict(cls, payload):
        values = dict(payload)
        values["alternatives"] = tuple(ReviewAlternative(
            text=item["text"], sources=tuple(item.get("sources") or ()),
            reason=item.get("reason", "machine_alternative"),
        ) for item in values.get("alternatives") or ())
        values["reason_for_review"] = tuple(values.get("reason_for_review") or ())
        return cls(**values)


def canonical_queue_hash(payload: dict) -> str:
    content = dict(payload)
    content.pop("queue_hash", None)
    encoded = json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ReviewQueue:
    source_video_id: str
    total_transcript_characters: int
    items: tuple[ReviewRequiredItem, ...]
    auto_resolved_count: int
    queue_version: str = "quick-review-queue-v1"
    queue_hash: str = ""
    schema_version: str = "quick-review-queue-v1"
    machine_generated: bool = True
    private: bool = True
    is_ground_truth: bool = False
    metadata: dict = field(default_factory=lambda: {
        "provider_execution_count": 0, "model_execution_count": 0,
        "network_request_count": 0, "cost": 0,
    })

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "queue_version": self.queue_version,
            "queue_hash": self.queue_hash,
            "source_video_id": self.source_video_id,
            "total_transcript_characters": self.total_transcript_characters,
            "auto_resolved_count": self.auto_resolved_count,
            "machine_generated": self.machine_generated,
            "private": self.private,
            "is_ground_truth": False,
            "metadata": self.metadata,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def create(cls, source_video_id, total_transcript_characters, items, auto_resolved_count):
        provisional = cls(
            source_video_id=str(source_video_id),
            total_transcript_characters=max(0, int(total_transcript_characters)),
            items=tuple(items), auto_resolved_count=int(auto_resolved_count),
        )
        return replace(provisional, queue_hash=canonical_queue_hash(provisional.to_dict()))

    @classmethod
    def from_dict(cls, payload):
        if canonical_queue_hash(payload) != payload.get("queue_hash", ""):
            raise ValueError("queue_hash_mismatch")
        queue = cls(
            source_video_id=payload["source_video_id"],
            total_transcript_characters=int(payload["total_transcript_characters"]),
            items=tuple(ReviewRequiredItem.from_dict(item) for item in payload.get("items") or ()),
            auto_resolved_count=int(payload.get("auto_resolved_count", 0)),
            queue_version=payload.get("queue_version", ""),
            queue_hash=payload.get("queue_hash", ""),
            schema_version=payload.get("schema_version", ""),
            machine_generated=bool(payload.get("machine_generated", True)),
            private=bool(payload.get("private", True)),
            is_ground_truth=bool(payload.get("is_ground_truth", False)),
            metadata=dict(payload.get("metadata") or {}),
        )
        if queue.is_ground_truth:
            raise ValueError("review_queue_cannot_be_ground_truth")
        return queue


def _atomic_write_new(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    lock = path.with_name(f".{path.name}.create-lock")
    try:
        try:
            lock.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise FileExistsError("immutable_queue_exists") from exc
        if path.exists():
            raise FileExistsError("immutable_queue_exists")
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
        if lock.exists():
            lock.rmdir()


class ImmutableReviewQueueStore:
    def __init__(self, path):
        self.path = Path(path).resolve()

    def create(self, queue: ReviewQueue) -> None:
        if self.path.exists():
            raise FileExistsError("immutable_queue_exists")
        try:
            _atomic_write_new(self.path, queue.to_dict())
        except FileExistsError as exc:
            raise FileExistsError("immutable_queue_exists") from exc

    def load(self) -> ReviewQueue:
        return ReviewQueue.from_dict(json.loads(self.path.read_text(encoding="utf-8")))


def _sources_for_text(provider_texts, text):
    return tuple(sorted(
        str(provider) for provider, value in dict(provider_texts or {}).items()
        if text and text in str(value or "")
    ))


def _localized_span(sample):
    carrier = str(sample.get("before_text") or sample.get("current_clean_raw_transcript") or "")
    critical = tuple(sample.get("critical_tokens") or ())
    for token in critical:
        value = str(token.get("value") or "") if isinstance(token, dict) else str(token)
        if value and value in carrier:
            start = carrier.index(value)
            return start, start + len(value), value
    alternatives = [str(value) for value in sample.get("alternatives") or () if value]
    for value in alternatives:
        if value != carrier and value in carrier:
            start = carrier.index(value)
            return start, start + len(value), value
    for value in alternatives:
        projection = project_candidate_spans(carrier, value)
        substantive = tuple(projection.substantive_spans)
        if projection.status == "PROJECTED" and len(substantive) == 1:
            span = substantive[0]
            if span.before_span:
                return span.start_offset, span.end_offset, span.before_span
    return 0, len(carrier), carrier


def _review_reasons(sample):
    reasons = []
    conflicts = tuple(sample.get("conflicts") or ())
    critical = tuple(sample.get("critical_tokens") or ())
    if critical:
        reasons.append("critical_token_conflict")
    labels = " ".join(
        str(item.get("conflict_type", "")) if isinstance(item, dict) else str(item)
        for item in conflicts
    ).upper()
    if "ASR" in labels:
        reasons.append("asr_conflict")
    if "CROSS_MODAL" in labels or ("OCR" in labels and "ASR" in labels):
        reasons.append("strong_ocr_asr_conflict")
    if sample.get("qwen_proposal"):
        reasons.append("guard_rejected_high_value_proposal")
    if sample.get("possible_completeness_loss"):
        reasons.append("possible_completeness_loss")
    if sample.get("obvious_language_abnormality"):
        reasons.append("obvious_language_abnormality")
    if sample.get("uncertain_proper_noun"):
        reasons.append("uncertain_proper_noun_or_acronym")
    if conflicts and not reasons:
        reasons.append("unresolved_machine_conflict")
    provider_values = {
        str(value) for value in dict(sample.get("provider_texts") or {}).values() if value
    }
    if len(provider_values) > 1 and not reasons:
        reasons.append("provider_text_disagreement")
    return tuple(dict.fromkeys(reasons))


def _risk(sample, reasons):
    token_types = {
        str(item.get("token_type") or item.get("category") or "").upper()
        for item in sample.get("critical_tokens") or () if isinstance(item, dict)
    }
    if token_types & _P0_TYPES:
        return "P0", sorted(token_types & _P0_TYPES)[0]
    if "possible_completeness_loss" in reasons or "strong_ocr_asr_conflict" in reasons:
        return "P1", None
    if "uncertain_proper_noun_or_acronym" in reasons:
        return "P1", "PROPER_NOUN_OR_ACRONYM"
    if reasons:
        return "P2", None
    return "P3", None


def _make_item(sample, index):
    reasons = _review_reasons(sample)
    if not reasons:
        return None
    carrier = str(sample.get("before_text") or sample.get("current_clean_raw_transcript") or "")
    start_offset, end_offset, suspect = _localized_span(sample)
    risk, token_type = _risk(sample, reasons)
    critical = tuple(sample.get("critical_tokens") or ())
    if token_type is None and critical and isinstance(critical[0], dict):
        token_type = str(critical[0].get("token_type") or critical[0].get("category") or "") or None
    providers = dict(sample.get("provider_texts") or {})
    alternatives = tuple(ReviewAlternative(
        text=str(text), sources=_sources_for_text(providers, str(text)),
    ) for text in dict.fromkeys(str(value) for value in sample.get("alternatives") or () if value))
    source = dict(sample.get("source") or {})
    review_id = str(sample.get("sample_id") or f"review-{index:04d}")
    clip = str(sample.get("clip_url") or "")
    return ReviewRequiredItem(
        review_id=review_id,
        segment_id=str(sample.get("segment_id") or review_id),
        start_ms=int(source.get("start_ms", sample.get("start_ms", 0))),
        end_ms=int(source.get("end_ms", sample.get("end_ms", 0))),
        context_before=str(sample.get("context_before") or ""),
        carrier_text=carrier,
        context_after=str(sample.get("context_after") or ""),
        suspect_span=suspect, start_offset=start_offset, end_offset=end_offset,
        alternatives=alternatives, critical_token_type=token_type,
        ocr_evidence={key: value for key, value in providers.items() if key in {"paddle", "apple_vision"}},
        asr_evidence={key: value for key, value in providers.items() if key in {"funasr", "whispercpp", "legacy_asr"}},
        qwen_proposal=sample.get("qwen_proposal"),
        reason_for_review=reasons, risk_level=risk,
        audio_clip_path=clip, video_clip_path=clip,
    )


def deduplicate_review_items(items):
    result = []
    for item in sorted(items, key=lambda value: (value.segment_id, value.start_offset, value.end_offset, value.review_id)):
        match_index = next((
            index for index, existing in enumerate(result)
            if existing.segment_id == item.segment_id
            and item.start_offset <= existing.end_offset
            and item.end_offset >= existing.start_offset
        ), None)
        if match_index is None:
            result.append(item)
            continue
        existing = result[match_index]
        start = min(existing.start_offset, item.start_offset)
        end = max(existing.end_offset, item.end_offset)
        alternative_map = {alt.text: alt for alt in (*existing.alternatives, *item.alternatives)}
        result[match_index] = replace(
            existing, start_offset=start, end_offset=end,
            suspect_span=existing.carrier_text[start:end],
            alternatives=tuple(alternative_map.values()),
            reason_for_review=tuple(dict.fromkeys((*existing.reason_for_review, *item.reason_for_review))),
            risk_level=min((existing.risk_level, item.risk_level), key=_PRIORITY.get),
            critical_token_type=existing.critical_token_type or item.critical_token_type,
        )
    return tuple(result)


def build_review_queue(source_video_id, samples, total_transcript_characters):
    candidates = []
    auto_resolved = 0
    for index, sample in enumerate(samples, 1):
        item = _make_item(sample, index)
        if item is None:
            auto_resolved += 1
        else:
            candidates.append(item)
    return ReviewQueue.create(
        source_video_id, total_transcript_characters,
        deduplicate_review_items(tuple(candidates)), auto_resolved,
    )
