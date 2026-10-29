from dataclasses import dataclass, field
from enum import Enum


class Platform(str, Enum):
    XIAOHONGSHU = "xiaohongshu"
    DOUYIN = "douyin"
    WECHAT_CHANNELS = "wechat_channels"
    LOCAL_UPLOAD = "local_upload"


@dataclass(frozen=True)
class LinkCandidate:
    raw_input: str
    cleaned_input: str
    extracted_url: str


@dataclass(frozen=True)
class RedirectHop:
    source_url: str
    target_url: str
    status_code: int


@dataclass(frozen=True)
class NormalizedLink:
    raw_input: str
    cleaned_input: str
    extracted_url: str
    resolved_url: str
    platform: Platform
    redirect_hops: tuple[RedirectHop, ...] = ()


@dataclass(frozen=True)
class RunRecord:
    id: str
    crm_job_id: int
    raw_input: str
    extracted_url: str
    resolved_url: str
    platform: str
    upload_path: str
    video_path: str
    file_hash: str
    duration_ms: int
    status: str
    current_stage: str
    error_code: str
    error_message: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class StageEvent:
    id: int
    run_id: str
    stage: str
    status: str
    error_code: str
    error_message: str
    started_at: str
    finished_at: str


@dataclass(frozen=True)
class DownloadRequest:
    run_id: str
    crm_job_id: int
    platform: Platform
    source_url: str = ""
    resolved_url: str = ""
    upload_path: str = ""


@dataclass(frozen=True)
class DownloadResult:
    platform: Platform
    source_url: str
    resolved_url: str
    title: str
    author: str
    video_path: str
    duration_ms: int
    file_hash: str
    status: str


@dataclass(frozen=True)
class ClientResult:
    ok: bool
    status_code: int
    payload: dict
    error_code: str = ""
    user_message: str = ""

    @classmethod
    def accepted(cls, crm_job_id: int, status: str):
        return cls(
            True,
            202,
            {"crm_job_id": int(crm_job_id), "status": str(status)},
        )

    @classmethod
    def unavailable(cls):
        return cls(
            False,
            0,
            {},
            "SERVICE_UNAVAILABLE",
            "提取服务不可用",
        )


@dataclass(frozen=True)
class SubtitleFrame:
    index: int
    timestamp_ms: int
    path: str
    image_hash: str
    width: int
    height: int


@dataclass(frozen=True)
class SubtitleSegment:
    id: str
    run_id: str
    start_ms: int
    end_ms: int
    representative_path: str
    image_hash: str
    preprocessing_version: str


@dataclass(frozen=True)
class OCRResult:
    provider: str
    start_ms: int
    end_ms: int
    text: str
    confidence: float
    boxes: tuple
    status: str
    model_version: str
    error_code: str
    error_message: str
    request_id: str = ""
    raw_response: dict = field(default_factory=dict)
    bounding_boxes: tuple = ()
    polygons: tuple = ()
    timestamp_ms: int | None = None
    frame_id: str = ""
    source_video_id: str = ""
    request_id_source: str = ""
    billing_type: str = ""
    cost: float | int | None = None

    @property
    def error(self):
        if not self.error_code and not self.error_message:
            return None
        return {"code": self.error_code, "message": self.error_message}

    def to_unified_dict(self):
        return {
            "provider": self.provider,
            "text": self.text,
            "confidence": self.confidence,
            "bounding_box": self.bounding_boxes,
            "polygon": self.polygons,
            "request_id": self.request_id,
            "raw_response": self.raw_response,
            "error": self.error,
            "timestamp": self.timestamp_ms,
            "frame_id": self.frame_id,
            "source_video_id": self.source_video_id,
            "request_id_source": self.request_id_source,
            "billing_type": self.billing_type,
            "cost": self.cost,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "status": self.status,
            "model_version": self.model_version,
        }


@dataclass(frozen=True)
class OCRProviderAttempt:
    segment_id: str
    provider: str
    attempt_no: int
    status: str
    error_code: str
    error_message: str


@dataclass(frozen=True)
class OCRConflict:
    segment_id: str
    category: str
    severity: str
    recommended_text: str
    variants: tuple[str, ...]


@dataclass(frozen=True)
class RecognitionAlignment:
    segment_id: str
    start_ms: int
    end_ms: int
    recommended_text: str
    reliability: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class ASRWord:
    start_ms: int
    end_ms: int
    text: str
    confidence: float | None = None

    def to_dict(self):
        return {
            "start_ms": int(self.start_ms),
            "end_ms": int(self.end_ms),
            "text": self.text,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class ASRSegment:
    start_ms: int
    end_ms: int
    text: str
    confidence: float | None = None

    def to_dict(self):
        return {
            "start_ms": int(self.start_ms),
            "end_ms": int(self.end_ms),
            "text": self.text,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class ASRResult:
    provider: str
    status: str
    full_text: str
    segments: tuple[ASRSegment, ...]
    words: tuple[ASRWord, ...]
    language: str
    duration_ms: int
    request_id: str
    request_id_source: str
    engine_version: str
    model_name: str
    model_version: str
    model_hash: str
    billing_type: str
    cost: float | int
    raw_response: dict
    error_code: str
    error_message: str
    source_video_id: str
    source_audio_id: str
    audio_hash: str
    timestamp_granularity: str
    timestamp_quality: str
    capabilities: dict
    latency_ms: float | None
    confidence: float | None = None

    @property
    def text(self):
        return self.full_text

    @property
    def error(self):
        if not self.error_code and not self.error_message:
            return None
        return {"code": self.error_code, "message": self.error_message}

    @property
    def start_ms(self):
        return self.segments[0].start_ms if self.segments else 0

    @property
    def end_ms(self):
        return self.segments[-1].end_ms if self.segments else self.duration_ms

    def to_unified_dict(self):
        return {
            "provider": self.provider,
            "status": self.status,
            "full_text": self.full_text,
            "segments": [item.to_dict() for item in self.segments],
            "words": [item.to_dict() for item in self.words],
            "language": self.language,
            "duration_ms": int(self.duration_ms),
            "request_id": self.request_id,
            "request_id_source": self.request_id_source,
            "engine_version": self.engine_version,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "model_hash": self.model_hash,
            "billing_type": self.billing_type,
            "cost": self.cost,
            "raw_response": self.raw_response,
            "error": self.error,
            "source_video_id": self.source_video_id,
            "source_audio_id": self.source_audio_id,
            "audio_hash": self.audio_hash,
            "timestamp_granularity": self.timestamp_granularity,
            "timestamp_quality": self.timestamp_quality,
            "capabilities": self.capabilities,
            "latency_ms": self.latency_ms,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class ASRAlignmentMetrics:
    alignment_score: float
    matched_chars: int
    substituted_chars: int
    inserted_chars: int
    deleted_chars: int
    unmatched_chars: int
    coverage_ratio: float
    char_match_ratio: float
    boundary_anchor_score: float
    quality_level: str
    warnings: tuple[str, ...] = ()

    def to_dict(self):
        return {
            "alignment_score": float(self.alignment_score),
            "matched_chars": int(self.matched_chars),
            "substituted_chars": int(self.substituted_chars),
            "inserted_chars": int(self.inserted_chars),
            "deleted_chars": int(self.deleted_chars),
            "unmatched_chars": int(self.unmatched_chars),
            "coverage_ratio": float(self.coverage_ratio),
            "char_match_ratio": float(self.char_match_ratio),
            "boundary_anchor_score": float(self.boundary_anchor_score),
            "quality_level": self.quality_level,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ASRFusionConflict:
    conflict_type: str
    severity: str
    category: str
    provider_values: dict
    alternatives: tuple[str, ...]
    selected_value: str
    selection_reason: str
    requires_cross_modal_evidence: bool

    def to_dict(self):
        return {
            "conflict_type": self.conflict_type,
            "severity": self.severity,
            "category": self.category,
            "provider_values": {
                provider: list(values)
                for provider, values in self.provider_values.items()
            },
            "alternatives": list(self.alternatives),
            "selected_value": self.selected_value,
            "selection_reason": self.selection_reason,
            "requires_cross_modal_evidence": bool(
                self.requires_cross_modal_evidence
            ),
        }


@dataclass(frozen=True)
class ASRFusedSegment:
    start_ms: int
    end_ms: int
    raw_start_ms: int
    raw_end_ms: int
    fused_text: str
    text_source: str
    timestamp_source: str
    timestamp_quality: str
    timestamp_adjustment: str | None
    provider_texts: dict
    alternatives: tuple[str, ...]
    conflicts: tuple[ASRFusionConflict, ...]
    critical_tokens: tuple
    selection_reason: str
    alignment_score: float | None
    alignment_metrics: ASRAlignmentMetrics | None
    fusion_status: str
    confidence_level: str
    asr_consensus: bool
    final_truth: bool
    domain_candidates: tuple[str, ...]
    raw_metadata: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "start_ms": int(self.start_ms),
            "end_ms": int(self.end_ms),
            "raw_start_ms": int(self.raw_start_ms),
            "raw_end_ms": int(self.raw_end_ms),
            "fused_text": self.fused_text,
            "text_source": self.text_source,
            "timestamp_source": self.timestamp_source,
            "timestamp_quality": self.timestamp_quality,
            "timestamp_adjustment": self.timestamp_adjustment,
            "provider_texts": dict(self.provider_texts),
            "alternatives": list(self.alternatives),
            "conflicts": [item.to_dict() for item in self.conflicts],
            "critical_tokens": [
                item.to_dict() if hasattr(item, "to_dict") else item.__dict__
                for item in self.critical_tokens
            ],
            "selection_reason": self.selection_reason,
            "alignment_score": self.alignment_score,
            "alignment_metrics": (
                self.alignment_metrics.to_dict()
                if self.alignment_metrics is not None else None
            ),
            "fusion_status": self.fusion_status,
            "confidence_level": self.confidence_level,
            "asr_consensus": bool(self.asr_consensus),
            "final_truth": bool(self.final_truth),
            "domain_candidates": list(self.domain_candidates),
            "raw_metadata": dict(self.raw_metadata),
        }


@dataclass(frozen=True)
class ASRFusionResult:
    full_text: str
    segments: tuple[ASRFusedSegment, ...]
    words: tuple
    providers_used: tuple[str, ...]
    provider_results: tuple[ASRResult, ...]
    language: str
    duration_ms: int
    timestamp_granularity: str
    fusion_status: str
    confidence_level: str
    asr_consensus: bool
    final_truth: bool
    raw_metadata: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "modality": "asr",
            "full_text": self.full_text,
            "segments": [item.to_dict() for item in self.segments],
            "words": [
                item.to_dict() if hasattr(item, "to_dict") else item
                for item in self.words
            ],
            "providers_used": list(self.providers_used),
            "provider_results": [
                item.to_unified_dict() for item in self.provider_results
            ],
            "language": self.language,
            "duration_ms": int(self.duration_ms),
            "timestamp_granularity": self.timestamp_granularity,
            "fusion_status": self.fusion_status,
            "confidence_level": self.confidence_level,
            "asr_consensus": bool(self.asr_consensus),
            "final_truth": bool(self.final_truth),
            "raw_metadata": dict(self.raw_metadata),
        }


@dataclass(frozen=True)
class RecognitionSourceResult:
    provider: str
    modality: str
    start_ms: int
    end_ms: int
    text: str
    confidence: float
    status: str
