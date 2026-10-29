"""Sanitized text-only projection of persisted OCR and ASR provider outputs."""


SCHEMA_VERSION = "raw-provider-artifacts-v1"


def _text(value):
    return str(value or "").strip()


def _ocr_provider_texts(ocr_stage_result):
    fusion = getattr(ocr_stage_result, "fusion_result", None)
    frames = tuple(getattr(fusion, "frame_fusions", ()) or ())
    collected = {}
    for frame in sorted(
        frames,
        key=lambda item: (
            int(getattr(item, "start_ms", 0)),
            int(getattr(item, "end_ms", 0)),
            str(getattr(item, "frame_id", "")),
        ),
    ):
        for result in tuple(getattr(frame, "provider_results", ()) or ()):
            provider = _text(getattr(result, "provider", ""))
            text = _text(getattr(result, "text", ""))
            status = _text(getattr(result, "status", ""))
            if provider and text and status == "completed":
                collected.setdefault(provider, []).append(text)
    return {
        provider: "\n".join(values)
        for provider, values in collected.items()
        if values
    }


def _asr_provider_projection(asr_stage_result):
    provider_texts = {}
    provider_segments = {}
    for result in (
        getattr(asr_stage_result, "funasr_result", None),
        getattr(asr_stage_result, "whisper_result", None),
    ):
        provider = _text(getattr(result, "provider", ""))
        text = _text(getattr(result, "full_text", ""))
        status = _text(getattr(result, "status", ""))
        if not provider or not text or status != "completed":
            continue
        provider_texts[provider] = text
        segments = []
        for segment in tuple(getattr(result, "segments", ()) or ()):
            segment_text = _text(getattr(segment, "text", ""))
            if not segment_text:
                continue
            segments.append({
                "start_ms": int(getattr(segment, "start_ms", 0)),
                "end_ms": int(getattr(segment, "end_ms", 0)),
                "text": segment_text,
            })
        provider_segments[provider] = segments
    return provider_texts, provider_segments


def build_raw_provider_artifacts(source_run_id, ocr_stage_result, asr_stage_result):
    """Return provider text/timing only; never provider raw_response or paths."""
    ocr_provider_texts = _ocr_provider_texts(ocr_stage_result)
    asr_provider_texts, asr_provider_segments = _asr_provider_projection(
        asr_stage_result,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "source_run_id": str(source_run_id or ""),
        "ocr": {
            "artifact_key": "recognizing_ocr",
            "provider_texts": ocr_provider_texts,
        },
        "asr": {
            "artifact_key": "fusing_asr",
            "provider_texts": asr_provider_texts,
            "provider_segments": asr_provider_segments,
        },
    }
