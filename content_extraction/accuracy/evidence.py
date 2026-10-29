"""Read-only, redacted adapter from frozen Cross-Modal results."""

import hashlib
import json

from .schemas import (
    EvidenceProviderRecord, TranscriptEvidenceBundle, TranscriptEvidenceSegment,
)


def _safe_summary(value):
    if value is None:
        return {}
    allowed = (
        "strength", "provider_count", "agreement_count", "local_consensus",
        "provenance_complete", "evidence_completeness", "timestamp_quality",
        "providers_used", "alignment_score", "critical_conflict",
    )
    return {key: getattr(value, key) for key in allowed if hasattr(value, key)}


def _provider_records(segment):
    output = []
    for modality, provenance in (
        ("ocr", getattr(segment, "ocr_provenance", {})),
        ("asr", getattr(segment, "asr_provenance", {})),
    ):
        if not isinstance(provenance, dict):
            continue
        provider_texts = provenance.get("provider_texts", {})
        if not isinstance(provider_texts, dict):
            continue
        for provider, text in provider_texts.items():
            output.append(EvidenceProviderRecord(
                provider=str(provider), modality=modality, text=str(text or ""),
                start_ms=int(getattr(segment, "start_ms", 0)),
                end_ms=int(getattr(segment, "end_ms", 0)),
                timestamp_quality=("native" if modality == "ocr" else "aligned"),
                evidence_ref=f"{modality}:{getattr(segment, 'id', '')}:{provider}",
            ))
    return tuple(output)


def _serializable(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "token_type") and hasattr(value, "value"):
        return {"token_type": value.token_type, "value": value.value}
    return str(value)


def build_evidence_bundle(result, glossary):
    source_identity = getattr(result, "source_identity", {}) or {}
    source_video_id = str(source_identity.get("source_video_id", ""))
    segments = tuple(TranscriptEvidenceSegment(
        segment_id=str(item.id), start_ms=int(item.start_ms), end_ms=int(item.end_ms),
        clean_text=str(item.clean_text or ""), ocr_text=str(item.ocr_text or ""),
        asr_text=str(item.asr_text or ""), selected_source=str(item.selected_source or ""),
        included_in_clean_transcript=bool(item.included_in_clean_transcript),
        text_role=str(item.text_role or "unknown"),
        content_relation=str(item.content_relation or ""),
        alternatives=tuple(str(value) for value in getattr(item, "alternatives", ()) if value),
        conflicts=tuple(_serializable(value) for value in getattr(item, "conflicts", ())),
        critical_tokens=tuple(
            _serializable(value) for value in getattr(item, "critical_tokens", ())
        ),
        provider_evidence=_provider_records(item),
        ocr_evidence_summary=_safe_summary(getattr(item, "ocr_evidence", None)),
        asr_evidence_summary=_safe_summary(getattr(item, "asr_evidence", None)),
        selection_reason=str(item.selection_reason or ""),
        fusion_status=str(item.fusion_status or ""),
        confidence_level=str(item.confidence_level or "LOW"),
        provisional_selection=bool(item.provisional_selection),
    ) for item in result.segments)
    identity = json.dumps({
        "source_video_id": source_video_id,
        "duration_ms": int(getattr(result, "duration_ms", 0)),
        "segments": [(x.segment_id, x.start_ms, x.end_ms, x.clean_text) for x in segments],
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return TranscriptEvidenceBundle(
        bundle_id=f"evidence:{source_video_id}:{hashlib.sha256(identity).hexdigest()}",
        source_video_id=source_video_id,
        duration_ms=int(getattr(result, "duration_ms", 0)),
        timebase=str(source_identity.get("timebase", "milliseconds")),
        source_clean_transcript=str(getattr(result, "clean_raw_transcript", "")),
        source_fusion_status=str(getattr(result, "fusion_status", "")),
        source_confidence_level=str(getattr(result, "confidence_level", "LOW")),
        source_final_truth=False, segments=segments,
        glossary={"version": glossary.version, "fingerprint": glossary.fingerprint,
                  "domain": glossary.domain},
        source_provenance={"cross_modal_schema": "phase4-v1",
                           "provider_execution_performed": False},
    )


def build_evidence_bundle_from_payload(payload, glossary):
    """Adapt a previously frozen benchmark JSON without re-running Providers."""
    source_identity = payload.get("source_identity") or {}
    source_video_id = str(source_identity.get("source_video_id") or "")
    allowed_summary = {
        "strength", "provider_count", "agreement_count", "local_consensus",
        "provenance_complete", "evidence_completeness", "timestamp_quality",
        "providers_used", "alignment_score", "critical_conflict",
    }
    segments = []
    for item in payload.get("segments") or ():
        records = []
        for modality, key in (("ocr", "ocr_provenance"), ("asr", "asr_provenance")):
            provenance = item.get(key) or {}
            for provider, text in (provenance.get("provider_texts") or {}).items():
                records.append(EvidenceProviderRecord(
                    provider=str(provider), modality=modality, text=str(text or ""),
                    start_ms=int(item.get("start_ms") or 0), end_ms=int(item.get("end_ms") or 0),
                    timestamp_quality="native" if modality == "ocr" else "aligned",
                    evidence_ref=f"{modality}:{item.get('id', '')}:{provider}",
                ))
        summaries = {}
        for key in ("ocr_evidence", "asr_evidence"):
            value = item.get(key) or {}
            summaries[key] = {name: value[name] for name in allowed_summary if name in value}
        segments.append(TranscriptEvidenceSegment(
            segment_id=str(item.get("id") or ""), start_ms=int(item.get("start_ms") or 0),
            end_ms=int(item.get("end_ms") or 0), clean_text=str(item.get("clean_text") or ""),
            ocr_text=str(item.get("ocr_text") or ""), asr_text=str(item.get("asr_text") or ""),
            selected_source=str(item.get("selected_source") or ""),
            included_in_clean_transcript=bool(item.get("included_in_clean_transcript", True)),
            text_role=str(item.get("text_role") or "unknown"),
            content_relation=str(item.get("content_relation") or ""),
            alternatives=tuple(str(value) for value in item.get("alternatives") or () if value),
            conflicts=tuple(item.get("conflicts") or ()),
            critical_tokens=tuple(item.get("critical_tokens") or ()),
            provider_evidence=tuple(records),
            ocr_evidence_summary=summaries["ocr_evidence"],
            asr_evidence_summary=summaries["asr_evidence"],
            selection_reason=str(item.get("selection_reason") or ""),
            fusion_status=str(item.get("fusion_status") or ""),
            confidence_level=str(item.get("confidence_level") or "LOW"),
            provisional_selection=bool(item.get("provisional_selection")),
        ))
    identity = json.dumps({
        "source_video_id": source_video_id,
        "duration_ms": int(payload.get("duration_ms") or 0),
        "segments": [(x.segment_id, x.start_ms, x.end_ms, x.clean_text) for x in segments],
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return TranscriptEvidenceBundle(
        bundle_id=f"evidence:{source_video_id}:{hashlib.sha256(identity).hexdigest()}",
        source_video_id=source_video_id, duration_ms=int(payload.get("duration_ms") or 0),
        source_clean_transcript=str(payload.get("clean_raw_transcript") or ""),
        segments=tuple(segments), timebase=str(source_identity.get("timebase") or "milliseconds"),
        source_fusion_status=str(payload.get("fusion_status") or ""),
        source_confidence_level=str(payload.get("confidence_level") or "LOW"),
        source_final_truth=False,
        glossary={"version": glossary.version, "fingerprint": glossary.fingerprint,
                  "domain": glossary.domain},
        source_provenance={"cross_modal_schema": "phase4-v1-frozen-json",
                           "provider_execution_performed": False},
    )
