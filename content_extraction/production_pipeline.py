"""Dependency assembly for the one production ServicePipelineRunner."""

import hashlib
import threading
from pathlib import Path

from .accuracy.corrector import AccurateTranscriptCorrector
from .accuracy.evidence import build_evidence_bundle
from .accuracy.glossary import AccurateDomainGlossary
from .accuracy.llama_cpp import LlamaCppCorrectionProposalEngine
from .accuracy.llama_runtime import LlamaCppRuntime, LlamaRuntimeConfig
from .accuracy.workload import ReviewWorkloadStage
from .accuracy.review_refinement import ReviewRefinementStage
from .accuracy.external.base import AtomicSparkCache
from .accuracy.external.iflytek_spark_arbiter import SparkLiteArbiter
from .accuracy.external.schemas import SparkLiteConfig
from .asr.funasr import FunASRAdapter, FunASRRuntimeConfig
from .asr.fusion import ASRFusionEngine
from .asr.whispercpp import WhisperCppAdapter, WhisperCppRuntimeConfig
from .downloaders import build_phase2_adapter_registry
from .fusion import CrossModalFusionEngine
from .link_ingest import LinkIngestStage, UploadIngestStage
from .models import Platform
from .pipeline_stages import (
    ASRStage, AccurateTranscriptStage, CrossModalStage, SubtitleOCRStage,
)
from .preprocessing.audio import AudioNormalizationStage
from .service_pipeline import ServicePipelineRunner, _fingerprint
from .transports import PublicDNSResolver, SafeHTTPTransport


QWEN3_8B_MODEL_SHA256 = "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"
QWEN3_8B_BINARY_SHA256 = "09a85ab72a9d400fd5e1d6dbca69b48b55213b589a131a8a10d01170d938b110"


def _sha256_file(path):
    candidate = Path(path)
    if not candidate.is_file():
        raise RuntimeError("model_missing")
    digest = hashlib.sha256()
    with candidate.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_production_pipeline_runner(repository, workspace, environ, local_workflow):
    billing_mode = str(environ.get("BILLING_MODE") or "FREE_ONLY").strip().upper()
    if billing_mode != "FREE_ONLY":
        raise RuntimeError("billing_mode_must_be_free_only")
    workspace = Path(workspace).resolve()

    def project_path(name, default):
        value = Path(str(environ.get(name) or default))
        return value if value.is_absolute() else workspace / value

    runtime_root = project_path(
        "AAIE_CRM_CONTENT_EXTRACTION_RUNTIME_ROOT", "cache/content_extractions/runtime",
    )
    media_root = project_path(
        "AAIE_CRM_CONTENT_EXTRACTION_DATA_ROOT", "data/content_extraction",
    ) / "media"
    ffmpeg_path = project_path(
        "AAIE_CRM_CONTENT_EXTRACTION_FFMPEG_PATH",
        "vendor/content_extraction/imageio_ffmpeg/binaries/ffmpeg-macos-aarch64-v7.1",
    )
    resolver = PublicDNSResolver()
    transport = SafeHTTPTransport(
        resolver,
        timeout=float(environ.get("AAIE_CRM_CONTENT_EXTRACTION_HTTP_TIMEOUT_SECONDS") or 30),
    )
    registry = build_phase2_adapter_registry(
        media_root,
        {
            Platform.XIAOHONGSHU: transport,
            Platform.DOUYIN: transport,
            Platform.WECHAT_CHANNELS: transport,
        },
        transport,
    )

    fun_root = runtime_root / "asr/funasr-gguf"
    whisper_root = runtime_root / "asr/whispercpp"
    funasr = FunASRAdapter(FunASRRuntimeConfig(
        binary_path=str(fun_root / "bin/llama-funasr-sensevoice"),
        model_path=str(fun_root / "models/sensevoice-small-q8.gguf"),
        vad_path=str(fun_root / "models/fsmn-vad.gguf"),
        state_dir=str(fun_root / "state"), timeout_seconds=300,
        engine_version="runtime-llamacpp-v0.1.9+funasr-v1.4.1",
        model_name="SenseVoiceSmall", model_version="q8",
        download_enabled=False, require_offline_guard=True,
    ))
    whisper = WhisperCppAdapter(WhisperCppRuntimeConfig(
        binary_path=str(whisper_root / "bin/whisper-cli"),
        model_path=str(whisper_root / "models/ggml-medium-q5_0.bin"),
        state_dir=str(whisper_root / "state"), timeout_seconds=600,
        engine_version="1.9.2", model_name="medium", model_version="q5_0",
        language="zh", download_enabled=False, require_offline_guard=True,
    ))

    qwen_root = project_path(
        "AAIE_CRM_ACCURATE_TRANSCRIPT_QWEN_RUNTIME_ROOT",
        "cache/content_extractions/runtime/accuracy/llamacpp",
    )
    qwen_binary = qwen_root / "bin/llama-server"
    qwen_model = qwen_root / "models/Qwen3-8B-Q4_K_M.gguf"
    runtime_integrity_lock = threading.Lock()
    runtime_integrity_verified = False

    def runtime_factory():
        nonlocal runtime_integrity_verified
        with runtime_integrity_lock:
            if not runtime_integrity_verified:
                if _sha256_file(qwen_binary) != QWEN3_8B_BINARY_SHA256:
                    raise RuntimeError("llama_binary_fingerprint_mismatch")
                if _sha256_file(qwen_model) != QWEN3_8B_MODEL_SHA256:
                    raise RuntimeError("qwen_model_fingerprint_mismatch")
                runtime_integrity_verified = True
        return LlamaCppRuntime(LlamaRuntimeConfig(
            runtime_root=qwen_root, binary_path=qwen_binary, model_path=qwen_model,
            port=int(environ.get("AAIE_CRM_ACCURATE_TRANSCRIPT_QWEN_PORT") or 18091),
            startup_timeout_seconds=180, shutdown_timeout_seconds=60,
        ))

    glossary = AccurateDomainGlossary.default()
    accurate_stage = AccurateTranscriptStage(
        glossary=glossary, runtime_factory=runtime_factory,
        proposal_engine_factory=lambda endpoint: LlamaCppCorrectionProposalEngine(
            endpoint, "sha256:" + QWEN3_8B_MODEL_SHA256, timeout_seconds=180,
        ),
        corrector_factory=lambda engine: AccurateTranscriptCorrector(
            glossary=glossary, proposal_engine=engine,
        ),
        bundle_builder=build_evidence_bundle,
    )
    quick_review_root = project_path(
        "AAIE_CRM_ACCURATE_TRANSCRIPT_RUNTIME_ROOT",
        "cache/content_extractions/runtime/accurate_transcript/quick_review",
    )
    clip_root = project_path(
        "AAIE_CRM_ACCURATE_TRANSCRIPT_CLIP_ROOT",
        "cache/content_extractions/runtime/accurate_transcript/quick_review/clips",
    )
    spark_cache = AtomicSparkCache(quick_review_root / "spark_cache")
    refinement_stage = ReviewRefinementStage(
        lambda queue_hash: SparkLiteArbiter(
            SparkLiteConfig.from_environ(environ),
            cache=spark_cache,
            queue_hash=queue_hash,
        )
    )
    config_fingerprint = _fingerprint(
        "service-pipeline-v1", "paddle+apple-vision", "funasr+whispercpp",
        QWEN3_8B_MODEL_SHA256, QWEN3_8B_BINARY_SHA256, billing_mode,
    )
    return ServicePipelineRunner(
        repository, runtime_root, config_fingerprint,
        link_stage=LinkIngestStage(repository, transport, resolver, registry),
        upload_stage=UploadIngestStage(repository, local_workflow.upload_adapter),
        audio_stage=AudioNormalizationStage(ffmpeg_path, runtime_root),
        ocr_stage=SubtitleOCRStage(local_workflow.subtitle_pipeline),
        asr_stage=ASRStage(funasr, whisper, ASRFusionEngine()),
        cross_modal_stage=CrossModalStage(CrossModalFusionEngine()),
        accurate_stage=accurate_stage,
        review_stage=ReviewWorkloadStage(
            quick_review_root, clip_root,
            refinement_stage=refinement_stage,
        ),
    )
