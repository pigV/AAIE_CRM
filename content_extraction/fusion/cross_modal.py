from dataclasses import dataclass, field, replace

from ..ocr.evidence import OCRCrossModalEvidence, OCRCrossModalEvidenceResolver
from ..shared.critical_tokens import extract_critical_tokens
from ..shared.recognition_text import normalize_for_comparison, similarity_signals
from .alignment import (
    build_temporal_components,
    classify_text_relation,
)
from .conflict import detect_cross_modal_conflicts
from .evidence import classify_asr_evidence, classify_ocr_evidence
from .transcript import finalize_clean_segments


def _serialize(value):
    if value is None:
        return None
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


@dataclass(frozen=True)
class CrossModalSegment:
    id: str
    start_ms: int
    end_ms: int
    clean_text: str
    normalized_text: str
    ocr_text: str
    asr_text: str
    selected_source: str
    included_in_clean_transcript: bool
    text_role: str
    content_relation: str
    ocr_evidence: object | None
    asr_evidence: object | None
    temporal_overlap: object | None
    text_similarity: dict
    critical_tokens: tuple
    conflicts: tuple
    alternatives: tuple[str, ...]
    selection_reason: str
    fusion_status: str
    resolution_level: int
    confidence_level: str
    ocr_provenance: dict
    asr_provenance: dict
    provisional_selection: bool
    final_truth: bool = False

    def to_dict(self):
        return {
            "id": self.id,
            "start_ms": int(self.start_ms),
            "end_ms": int(self.end_ms),
            "clean_text": self.clean_text,
            "normalized_text": self.normalized_text,
            "ocr_text": self.ocr_text,
            "asr_text": self.asr_text,
            "selected_source": self.selected_source,
            "included_in_clean_transcript": bool(self.included_in_clean_transcript),
            "text_role": self.text_role,
            "content_relation": self.content_relation,
            "ocr_evidence": _serialize(self.ocr_evidence),
            "asr_evidence": _serialize(self.asr_evidence),
            "temporal_overlap": _serialize(self.temporal_overlap),
            "text_similarity": dict(self.text_similarity),
            "critical_tokens": [_serialize(item) for item in self.critical_tokens],
            "conflicts": [_serialize(item) for item in self.conflicts],
            "alternatives": list(self.alternatives),
            "selection_reason": self.selection_reason,
            "fusion_status": self.fusion_status,
            "resolution_level": int(self.resolution_level),
            "confidence_level": self.confidence_level,
            "ocr_provenance": dict(self.ocr_provenance),
            "asr_provenance": dict(self.asr_provenance),
            "provisional_selection": bool(self.provisional_selection),
            "final_truth": False,
        }


@dataclass(frozen=True)
class CrossModalFusionResult:
    clean_raw_transcript: str
    segments: tuple[CrossModalSegment, ...]
    included_segment_ids: tuple[str, ...]
    excluded_segment_ids: tuple[str, ...]
    ocr_fusion_result: object | None
    asr_fusion_result: object | None
    duration_ms: int
    fusion_status: str
    confidence_level: str
    source_identity: dict
    final_truth: bool = False
    raw_metadata: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "clean_raw_transcript": self.clean_raw_transcript,
            "segments": [item.to_dict() for item in self.segments],
            "included_segment_ids": list(self.included_segment_ids),
            "excluded_segment_ids": list(self.excluded_segment_ids),
            "ocr_fusion_result": _serialize(self.ocr_fusion_result),
            "asr_fusion_result": _serialize(self.asr_fusion_result),
            "duration_ms": int(self.duration_ms),
            "fusion_status": self.fusion_status,
            "confidence_level": self.confidence_level,
            "source_identity": dict(self.source_identity),
            "final_truth": False,
            "raw_metadata": dict(self.raw_metadata),
        }


class CrossModalFusionEngine:
    _STRENGTH_RANK = {"NONE": 0, "WEAK": 1, "MODERATE": 2, "STRONG": 3}

    @staticmethod
    def _source_identity(ocr_result, asr_result):
        ocr_source = getattr(ocr_result, "source_video_id", "") if ocr_result else ""
        asr_source = ""
        if asr_result is not None:
            asr_source = str(getattr(asr_result, "raw_metadata", {}).get(
                "source_video_id", ""
            ) or "")
            if not asr_source:
                asr_source = next((
                    getattr(item, "source_video_id", "")
                    for item in getattr(asr_result, "provider_results", ())
                    if getattr(item, "source_video_id", "")
                ), "")
        return {
            "source_video_id": ocr_source or asr_source,
            "ocr_source_video_id": ocr_source,
            "asr_source_video_id": asr_source,
            "timebase": getattr(ocr_result, "timebase", "milliseconds") if ocr_result else "milliseconds",
        }

    @staticmethod
    def _provider_texts(asr_segments):
        providers = {}
        for segment in asr_segments:
            for provider, text in segment.provider_texts.items():
                providers.setdefault(provider, []).append(text)
        return {provider: "".join(values) for provider, values in providers.items()}

    @staticmethod
    def _join_texts(items, attribute):
        values = []
        for item in items:
            value = getattr(item, attribute, "")
            if value and (not values or normalize_for_comparison(value) != normalize_for_comparison(values[-1])):
                values.append(value)
        return "".join(values)

    @staticmethod
    def _combine_ocr_evidence(items, text, start_ms, end_ms):
        if len(items) == 1:
            return items[0]
        def unique(values):
            output = []
            for item in values:
                if item in (None, "") or any(item == existing for existing in output):
                    continue
                output.append(item)
            return tuple(output)
        providers = unique(provider for item in items for provider in item.providers_used)
        missing = unique(frame_id for item in items for frame_id in item.missing_frame_ids)
        return OCRCrossModalEvidence(
            text=text,
            start_ms=start_ms,
            end_ms=end_ms,
            frame_ids=unique(frame_id for item in items for frame_id in item.frame_ids),
            missing_frame_ids=missing,
            fusion_statuses=unique(status for item in items for status in item.fusion_statuses),
            confidence_levels=unique(level for item in items for level in item.confidence_levels),
            providers_used=providers,
            provider_count=len(providers),
            agreement_count=max((item.agreement_count for item in items), default=0),
            alternatives=unique(value for item in items for value in item.alternatives),
            conflicts=unique(value for item in items for value in item.conflicts),
            critical_tokens=unique(value for item in items for value in item.critical_tokens),
            bounding_region={},
            selection_reasons=unique(value for item in items for value in item.selection_reasons),
            provider_provenance=tuple(value for item in items for value in item.provider_provenance),
            local_consensus=all(item.local_consensus for item in items),
            cloud_arbitrated=any(item.cloud_arbitrated for item in items),
            cloud_call_count=sum(item.cloud_call_count for item in items),
            provenance_complete=all(item.provenance_complete for item in items),
            evidence_completeness="complete" if all(item.provenance_complete for item in items) else "partial",
            text_role=items[0].text_role if len({item.text_role for item in items}) == 1 else "unknown",
        )

    @staticmethod
    def _best_overlap(component):
        if not component.edges:
            return None
        return max(
            (edge.overlap for edge in component.edges),
            key=lambda item: (
                CrossModalFusionEngine._STRENGTH_RANK[item.strength],
                item.shorter_coverage_ratio,
                item.iou_ratio,
            ),
        )

    @staticmethod
    def _alternatives(*groups):
        return tuple(dict.fromkeys(
            value for group in groups for value in group if value
        ))

    def _cross_modal_segment(
        self, segment_id, ocr_segments, asr_segments, component,
        ocr_resolver, asr_result,
    ):
        start_ms = min(
            [item.start_ms for item in (*ocr_segments, *asr_segments)]
        )
        end_ms = max(
            [item.end_ms for item in (*ocr_segments, *asr_segments)]
        )
        ocr_text = self._join_texts(ocr_segments, "text")
        asr_text = self._join_texts(asr_segments, "fused_text")
        raw_ocr_evidence = self._combine_ocr_evidence(
            tuple(ocr_resolver.resolve(item) for item in ocr_segments),
            ocr_text, start_ms, end_ms,
        )
        ocr_evidence = classify_ocr_evidence(raw_ocr_evidence)
        asr_evidence = classify_asr_evidence(asr_segments, asr_result.providers_used)
        overlap = self._best_overlap(component)
        relation = classify_text_relation(ocr_text, asr_text, overlap.strength)
        provider_texts = self._provider_texts(asr_segments)
        cross_conflicts = detect_cross_modal_conflicts(
            ocr_text, asr_text, provider_texts
        )
        internal_asr_critical = any(
            item.fusion_status == "ASR_CRITICAL_TOKEN_CONFLICT"
            or any(
                conflict.conflict_type == "ASR_CRITICAL_TOKEN_CONFLICT"
                for conflict in item.conflicts
            ) for item in asr_segments
        )
        strong_visual = bool(
            overlap.strength == "STRONG"
            and raw_ocr_evidence.text_role == "spoken_subtitle"
            and ocr_evidence.strength in {"STRONG", "VERY_STRONG"}
            and raw_ocr_evidence.provenance_complete
            and not ocr_evidence.critical_conflict
        )
        provider_supports_ocr = any(
            normalize_for_comparison(text) == normalize_for_comparison(ocr_text)
            for text in provider_texts.values()
        )
        provider_near_ocr = any(
            (
                (signals := similarity_signals(text, ocr_text)).ratio >= 0.75
                and signals.levenshtein_distance <= 1
            )
            for text in provider_texts.values()
        )
        alternatives = self._alternatives(
            (ocr_text, asr_text),
            raw_ocr_evidence.alternatives,
            *(item.alternatives for item in asr_segments),
            tuple(provider_texts.values()),
        )
        conflicts = cross_conflicts
        selected = asr_text
        source = "asr"
        provisional = False
        if internal_asr_critical:
            if strong_visual and normalize_for_comparison(ocr_text) == normalize_for_comparison(asr_text):
                selected = asr_text
                source = "cross_modal"
                status = "CROSS_MODAL_CRITICAL_TOKEN_RESOLVED"
                confidence = "HIGH"
                reason = "ocr_confirms_asr_selected_candidate_and_whisper_evidence"
                level = 3
            else:
                status = "CROSS_MODAL_CRITICAL_CONFLICT"
                confidence = "LOW"
                reason = "ocr_supports_asr_alternative_but_internal_conflict_remains"
                level = 4
                provisional = True
        elif cross_conflicts:
            acronym_only = all(item.category == "ASCII_ACRONYM" for item in cross_conflicts)
            if strong_visual and (provider_supports_ocr or acronym_only):
                selected = ocr_text
                source = "cross_modal"
                status = "CROSS_MODAL_CRITICAL_TOKEN_RESOLVED"
                confidence = "HIGH"
                reason = "strong_ocr_evidence_over_asr_consensus_for_critical_token"
                level = 3
                conflicts = tuple(replace(
                    item,
                    selected_value=next(iter(item.ocr_values), ""),
                    resolved=True,
                    resolution_reason=reason,
                    requires_human_or_future_evidence=False,
                ) for item in cross_conflicts)
            else:
                status = "CROSS_MODAL_CRITICAL_CONFLICT"
                confidence = "LOW"
                reason = "critical_token_evidence_insufficient_for_override"
                level = 4
                provisional = True
        elif relation.relation in {"exact", "safely_similar"}:
            selected = asr_text if asr_text else ocr_text
            source = "cross_modal"
            status = "CROSS_MODAL_CONSENSUS"
            confidence = "VERY_HIGH" if (
                ocr_evidence.strength in {"STRONG", "VERY_STRONG"}
                and asr_evidence.strength == "STRONG"
                and overlap.strength == "STRONG"
            ) else "HIGH"
            reason = "cross_modal_text_and_critical_tokens_agree"
            level = 1
        elif relation.relation == "subtitle_compression":
            selected = asr_text
            source = "asr"
            status = "CROSS_MODAL_SUBTITLE_COMPRESSION"
            confidence = "MEDIUM"
            reason = "subtitle_compression_preserves_spoken_asr_text"
            level = 2
        elif strong_visual and (
            provider_supports_ocr
            or provider_near_ocr
            or asr_evidence.strength == "WEAK"
        ):
            selected = ocr_text
            source = "cross_modal"
            status = "CROSS_MODAL_RESOLVED"
            confidence = "HIGH" if provider_supports_ocr else "MEDIUM"
            if provider_supports_ocr:
                reason = "strong_subtitle_evidence_with_cross_modal_provider_corroboration"
            elif provider_near_ocr:
                reason = "strong_subtitle_evidence_with_localized_near_provider_support"
            else:
                reason = "strong_subtitle_evidence_over_weak_asr_conflict"
            level = 2
        else:
            selected = asr_text
            source = "asr"
            status = "CROSS_MODAL_UNRESOLVED"
            confidence = "LOW" if ocr_evidence.strength == "WEAK" else "MEDIUM"
            reason = "paraphrase_or_content_difference_keeps_spoken_asr_provisional"
            level = 4
            provisional = True
        return CrossModalSegment(
            id=segment_id,
            start_ms=start_ms,
            end_ms=end_ms,
            clean_text=selected,
            normalized_text=normalize_for_comparison(selected),
            ocr_text=ocr_text,
            asr_text=asr_text,
            selected_source=source,
            included_in_clean_transcript=bool(selected),
            text_role=raw_ocr_evidence.text_role,
            content_relation=relation.relation,
            ocr_evidence=ocr_evidence,
            asr_evidence=asr_evidence,
            temporal_overlap=overlap,
            text_similarity=relation.to_dict(),
            critical_tokens=extract_critical_tokens(selected),
            conflicts=conflicts,
            alternatives=alternatives,
            selection_reason=reason,
            fusion_status=status,
            resolution_level=level,
            confidence_level=confidence,
            ocr_provenance=raw_ocr_evidence.to_dict(),
            asr_provenance={
                "providers_used": list(asr_result.providers_used),
                "provider_texts": provider_texts,
                "asr_consensus": all(item.asr_consensus for item in asr_segments),
                "fusion_statuses": list(dict.fromkeys(item.fusion_status for item in asr_segments)),
                "underlying_conflicts": [
                    conflict.to_dict()
                    for item in asr_segments for conflict in item.conflicts
                ],
            },
            provisional_selection=provisional,
            final_truth=False,
        )

    def _asr_only_segment(self, segment_id, asr_segment, asr_result):
        evidence = classify_asr_evidence((asr_segment,), asr_result.providers_used)
        return CrossModalSegment(
            segment_id, asr_segment.start_ms, asr_segment.end_ms,
            asr_segment.fused_text, normalize_for_comparison(asr_segment.fused_text),
            "", asr_segment.fused_text, "asr", True, "spoken_speech",
            "modality_only", None, evidence, None, {},
            extract_critical_tokens(asr_segment.fused_text), (),
            self._alternatives((asr_segment.fused_text,), asr_segment.alternatives),
            "usable_asr_without_matching_ocr", "ASR_ONLY_SEGMENT", 5,
            "MEDIUM" if evidence.strength in {"STRONG", "MODERATE"} else "LOW",
            {}, {
                "providers_used": list(asr_result.providers_used),
                "provider_texts": dict(asr_segment.provider_texts),
                "asr_consensus": asr_segment.asr_consensus,
                "underlying_conflicts": [
                    item.to_dict() for item in asr_segment.conflicts
                ],
            }, False, False,
        )

    def _ocr_only_segment(self, segment_id, ocr_segment, resolver, asr_unavailable):
        raw = resolver.resolve(ocr_segment)
        evidence = classify_ocr_evidence(raw)
        include = bool(
            asr_unavailable
            and raw.text_role == "spoken_subtitle"
            and evidence.strength in {"STRONG", "VERY_STRONG"}
        )
        status = "OCR_ONLY_SPOKEN_SUBTITLE" if include else "OCR_ONLY_TEXT"
        return CrossModalSegment(
            segment_id, ocr_segment.start_ms, ocr_segment.end_ms,
            ocr_segment.text, normalize_for_comparison(ocr_segment.text),
            ocr_segment.text, "", "ocr", include, raw.text_role,
            "modality_only", evidence, None, None, {},
            extract_critical_tokens(ocr_segment.text), (),
            self._alternatives((ocr_segment.text,), raw.alternatives),
            "strong_spoken_subtitle_with_asr_unavailable" if include else "ocr_only_non_spoken_or_insufficient_evidence",
            status, 6, "MEDIUM" if include else "LOW",
            raw.to_dict(), {}, not include, False,
        )

    def _empty_result(self, ocr_result, asr_result, status, source_identity):
        return CrossModalFusionResult(
            "", (), (), (), ocr_result, asr_result, 0, status, "LOW",
            source_identity, False, {"final_truth": False},
        )

    def fuse(self, ocr_fusion_result, asr_fusion_result):
        identity = self._source_identity(ocr_fusion_result, asr_fusion_result)
        if ocr_fusion_result is None and asr_fusion_result is None:
            return self._empty_result(None, None, "CROSS_MODAL_NO_INPUT", identity)
        if (
            identity["ocr_source_video_id"]
            and identity["asr_source_video_id"]
            and identity["ocr_source_video_id"] != identity["asr_source_video_id"]
        ) or identity["timebase"] != "milliseconds":
            return self._empty_result(
                ocr_fusion_result, asr_fusion_result,
                "CROSS_MODAL_SOURCE_MISMATCH", identity,
            )
        ocr_segments = tuple(getattr(ocr_fusion_result, "segments", ()) or ())
        asr_segments = tuple(getattr(asr_fusion_result, "segments", ()) or ())
        resolver = OCRCrossModalEvidenceResolver(ocr_fusion_result) if ocr_fusion_result else None
        components = build_temporal_components(ocr_segments, asr_segments)
        asr_unavailable = bool(
            asr_fusion_result is None
            or getattr(asr_fusion_result, "fusion_status", "") == "ASR_NO_PROVIDER_RESULT"
        )
        segments = []
        for index, component in enumerate(components, 1):
            component_ocr = tuple(ocr_segments[item] for item in component.ocr_indexes)
            component_asr = tuple(asr_segments[item] for item in component.asr_indexes)
            segment_id = f"cm-{index:04d}"
            if component_ocr and component_asr:
                segments.append(self._cross_modal_segment(
                    segment_id, component_ocr, component_asr, component,
                    resolver, asr_fusion_result,
                ))
            elif component_asr:
                for offset, item in enumerate(component_asr):
                    segments.append(self._asr_only_segment(
                        segment_id if not offset else f"{segment_id}-{offset + 1}",
                        item, asr_fusion_result,
                    ))
            elif component_ocr:
                for offset, item in enumerate(component_ocr):
                    segments.append(self._ocr_only_segment(
                        segment_id if not offset else f"{segment_id}-{offset + 1}",
                        item, resolver, asr_unavailable,
                    ))
        segments, clean_text = finalize_clean_segments(segments)
        included = tuple(item.id for item in segments if item.included_in_clean_transcript)
        excluded = tuple(item.id for item in segments if not item.included_in_clean_transcript)
        statuses = {item.fusion_status for item in segments}
        if not segments:
            status = "CROSS_MODAL_EMPTY"
            confidence = "LOW"
        elif len(statuses) == 1:
            status = next(iter(statuses))
            confidence = segments[0].confidence_level
        elif any("CONFLICT" in item or "UNRESOLVED" in item for item in statuses):
            status = "CROSS_MODAL_PARTIAL"
            confidence = "LOW"
        else:
            status = "CROSS_MODAL_COMPLETE"
            confidence = "MEDIUM"
        duration = max(
            getattr(ocr_fusion_result, "duration_ms", 0) if ocr_fusion_result else 0,
            getattr(asr_fusion_result, "duration_ms", 0) if asr_fusion_result else 0,
            max((item.end_ms for item in segments), default=0),
        )
        return CrossModalFusionResult(
            clean_text, segments, included, excluded,
            ocr_fusion_result, asr_fusion_result, duration, status, confidence,
            identity, False, {
                "final_truth": False,
                "component_count": len(components),
                "weak_temporal_edges_rejected": sum(len(item.weak_edges) for item in components),
                "provider_execution_performed": False,
            },
        )
