from abc import ABC, abstractmethod

from ..models import ASRResult


class ASRAdapter(ABC):
    provider = ""
    engine_version = ""
    model_name = ""
    model_version = ""
    request_id_source = "local"
    billing_type = "local"
    cost = 0

    def failed(
        self,
        *,
        status,
        error_code,
        error_message,
        source_audio_id,
        source_video_id="",
        duration_ms=0,
        audio_hash="",
        model_hash="",
        raw_response=None,
        latency_ms=None,
    ):
        return ASRResult(
            provider=self.provider,
            status=str(status),
            full_text="",
            segments=(),
            words=(),
            language="",
            duration_ms=int(duration_ms or 0),
            request_id="",
            request_id_source=self.request_id_source,
            engine_version=self.engine_version,
            model_name=self.model_name,
            model_version=self.model_version,
            model_hash=str(model_hash or ""),
            billing_type=self.billing_type,
            cost=self.cost,
            raw_response=dict(raw_response or {}),
            error_code=str(error_code),
            error_message=str(error_message or "")[:500],
            source_video_id=str(source_video_id or ""),
            source_audio_id=str(source_audio_id or ""),
            audio_hash=str(audio_hash or ""),
            timestamp_granularity="none",
            timestamp_quality="none",
            capabilities={
                "vad": False,
                "punctuation": "unknown",
                "confidence": False,
                "word_timestamps": False,
            },
            latency_ms=latency_ms,
            confidence=None,
        )

    @abstractmethod
    def transcribe(self, audio_path, *, source_audio_id, source_video_id=""):
        raise NotImplementedError
