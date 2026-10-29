"""Thin production stage wrappers around accepted extraction algorithms."""

from dataclasses import dataclass

from .models import ASRResult


@dataclass(frozen=True)
class OCRStageResult:
    fusion_result: object
    recommended_text: str
    segment_count: int


@dataclass(frozen=True)
class ASRStageResult:
    funasr_result: object
    whisper_result: object
    fusion_result: object
    accounting: dict


@dataclass(frozen=True)
class AccurateStageResult:
    evidence_bundle: object
    correction_result: object
    accounting: dict


class SubtitleOCRStage:
    def __init__(self, local_pipeline):
        self.local_pipeline = local_pipeline

    def run(self, context):
        result = self.local_pipeline.process(
            context.run_id, context.video_path, complete_job=False,
            source_video_id=context.source_video_id,
        )
        if result.status != "completed" or result.ocr_fusion_result is None:
            raise RuntimeError("ocr_stage_incomplete")
        return OCRStageResult(
            result.ocr_fusion_result, result.recommended_text, result.segment_count,
        )


def _failed_asr(provider, context):
    audio = context.audio
    return ASRResult(
        provider=str(provider), status="failed", full_text="", segments=(), words=(),
        language="", duration_ms=int(getattr(audio, "duration_ms", 0)),
        request_id=f"local:{provider}:failed", request_id_source="local",
        engine_version="", model_name="", model_version="", model_hash="",
        billing_type="local", cost=0, raw_response={},
        error_code="LOCAL_ASR_FAILED", error_message="本地语音识别失败",
        source_video_id=str(context.source_video_id),
        source_audio_id=str(getattr(audio, "path", "")),
        audio_hash=str(getattr(audio, "sha256", "")),
        timestamp_granularity="none", timestamp_quality="none",
        capabilities={}, latency_ms=None, confidence=None,
    )


class ASRStage:
    def __init__(self, funasr_adapter, whisper_adapter, fusion_engine):
        self.funasr_adapter = funasr_adapter
        self.whisper_adapter = whisper_adapter
        self.fusion_engine = fusion_engine

    @staticmethod
    def _transcribe(adapter, context):
        return adapter.transcribe(
            context.audio.path,
            source_audio_id=str(context.audio.path).rsplit("/", 1)[-1],
            source_video_id=context.source_video_id,
        )

    def run(self, context):
        accounting = {}
        try:
            funasr = self._transcribe(self.funasr_adapter, context)
        except Exception:
            funasr = _failed_asr("funasr", context)
        accounting["funasr"] = {
            "current_run_delta": 1, "checkpoint_accumulated": 0,
            "diagnostic_rerun_total": 0,
        }
        try:
            whisper = self._transcribe(self.whisper_adapter, context)
        except Exception:
            whisper = _failed_asr("whispercpp", context)
        accounting["whispercpp"] = {
            "current_run_delta": 1, "checkpoint_accumulated": 0,
            "diagnostic_rerun_total": 0,
        }
        fused = self.fusion_engine.fuse(funasr, whisper)
        return ASRStageResult(funasr, whisper, fused, accounting)


class CrossModalStage:
    def __init__(self, engine):
        self.engine = engine

    def run(self, context):
        return self.engine.fuse(
            context.ocr_fusion_result, context.asr_fusion_result,
        )


class AccurateTranscriptStage:
    def __init__(
        self, *, glossary, runtime_factory, proposal_engine_factory,
        corrector_factory, bundle_builder,
    ):
        self.glossary = glossary
        self.runtime_factory = runtime_factory
        self.proposal_engine_factory = proposal_engine_factory
        self.corrector_factory = corrector_factory
        self.bundle_builder = bundle_builder

    def run(self, context):
        bundle = self.bundle_builder(context.cross_modal_result, self.glossary)
        with self.runtime_factory() as runtime:
            proposal_engine = self.proposal_engine_factory(runtime.endpoint)
            corrector = self.corrector_factory(proposal_engine)
            result = corrector.correct(bundle)
        return AccurateStageResult(
            bundle, result,
            {"qwen3_8b": {
                "current_run_delta": 1, "checkpoint_accumulated": 0,
                "diagnostic_rerun_total": 0,
            }},
        )
