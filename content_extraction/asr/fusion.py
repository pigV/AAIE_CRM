from .alignment import align_text_to_segments
from .glossary import DomainGlossary
from ..models import (
    ASRFusedSegment,
    ASRFusionConflict,
    ASRFusionResult,
)
from ..shared.critical_tokens import extract_critical_tokens
from ..shared.recognition_text import normalize_for_comparison


class ASRFusionEngine:
    def __init__(self, glossary=None):
        self.glossary = glossary or DomainGlossary.default()

    @staticmethod
    def _available(result):
        return bool(
            result is not None
            and result.status == "completed"
            and result.full_text
        )

    @staticmethod
    def _provider_errors(*results):
        return {
            result.provider: result.error_code
            for result in results
            if result is not None and result.error_code
        }

    @staticmethod
    def _normalize_bounds(raw_start_ms, raw_end_ms, duration_ms):
        duration_ms = max(0, int(duration_ms or 0))
        raw_start_ms = int(raw_start_ms)
        raw_end_ms = int(raw_end_ms)
        start_ms = max(0, min(raw_start_ms, duration_ms))
        end_ms = max(start_ms, min(raw_end_ms, duration_ms))
        if raw_end_ms > duration_ms:
            adjustment = "duration_clamp"
        elif raw_start_ms < 0:
            adjustment = "negative_start_clamp"
        else:
            adjustment = None
        return start_ms, end_ms, adjustment

    @staticmethod
    def _safe_punctuated_text(full_text, segment_alignment, segment_index, total):
        candidate = segment_alignment.funasr_text
        if (
            not candidate
            or segment_alignment.metrics.quality_level != "HIGH"
            or segment_alignment.funasr_raw_start is None
            or segment_alignment.funasr_raw_end is None
        ):
            return candidate
        if segment_index == 0:
            prefix = full_text[:segment_alignment.funasr_raw_start]
            if prefix and not normalize_for_comparison(prefix):
                candidate = prefix + candidate
        if segment_index == total - 1:
            suffix = full_text[segment_alignment.funasr_raw_end:]
            if suffix and not normalize_for_comparison(suffix):
                candidate += suffix
        return candidate

    @staticmethod
    def _token_groups(text):
        grouped = {}
        for token in extract_critical_tokens(text):
            grouped.setdefault(token.token_type, []).append(token.value)
        return {
            token_type: tuple(dict.fromkeys(values))
            for token_type, values in grouped.items()
        }

    def _domain_candidates(self, *texts):
        output = []
        for text in texts:
            for token in extract_critical_tokens(text):
                if token.token_type != "ASCII_ACRONYM":
                    continue
                output.extend(
                    item.value
                    for item in self.glossary.generate_candidates(token.value, text)
                )
        return tuple(dict.fromkeys(output))

    def _critical_conflicts(self, funasr_text, whisper_text):
        funasr_groups = self._token_groups(funasr_text)
        whisper_groups = self._token_groups(whisper_text)
        conflicts = []
        for category in sorted(set(funasr_groups) | set(whisper_groups)):
            funasr_values = funasr_groups.get(category, ())
            whisper_values = whisper_groups.get(category, ())
            if funasr_values == whisper_values:
                continue
            provider_values = {
                "funasr": funasr_values,
                "whispercpp": whisper_values,
            }
            alternatives = tuple(dict.fromkeys(
                (*funasr_values, *whisper_values)
            ))
            scored = self.glossary.score_candidates(
                provider_values,
                context=f"{funasr_text} {whisper_text}",
            )
            if scored and (
                len(scored) == 1 or scored[0].score > scored[1].score
            ):
                selected = scored[0].value
                selection_reason = (
                    f"selected {selected} with domain glossary support; "
                    "provider conflict remains; final_truth=false"
                )
            else:
                selected = whisper_values[0] if whisper_values else (
                    funasr_values[0] if funasr_values else ""
                )
                selection_reason = (
                    "retained Whisper timeline candidate; provider conflict "
                    "remains unresolved; final_truth=false"
                )
            conflicts.append(ASRFusionConflict(
                conflict_type="ASR_CRITICAL_TOKEN_CONFLICT",
                severity="HIGH",
                category=category,
                provider_values=provider_values,
                alternatives=alternatives,
                selected_value=selected,
                selection_reason=selection_reason,
                requires_cross_modal_evidence=True,
            ))
        return tuple(conflicts)

    def _single_whisper(self, funasr_result, whisper_result):
        segments = []
        for item in whisper_result.segments:
            start_ms, end_ms, adjustment = self._normalize_bounds(
                item.start_ms, item.end_ms, whisper_result.duration_ms
            )
            segments.append(ASRFusedSegment(
                start_ms=start_ms,
                end_ms=end_ms,
                raw_start_ms=item.start_ms,
                raw_end_ms=item.end_ms,
                fused_text=item.text,
                text_source="whispercpp",
                timestamp_source="whispercpp",
                timestamp_quality="native",
                timestamp_adjustment=adjustment,
                provider_texts={"whispercpp": item.text},
                alternatives=(),
                conflicts=(),
                critical_tokens=extract_critical_tokens(item.text),
                selection_reason="single Whisper provider result",
                alignment_score=None,
                alignment_metrics=None,
                fusion_status="ASR_SINGLE_PROVIDER",
                confidence_level="LOW",
                asr_consensus=False,
                final_truth=False,
                domain_candidates=self._domain_candidates(item.text),
                raw_metadata={},
            ))
        segments = tuple(segments)
        return ASRFusionResult(
            full_text=whisper_result.full_text,
            segments=segments,
            words=(),
            providers_used=(whisper_result.provider,),
            provider_results=tuple(
                item for item in (funasr_result, whisper_result) if item is not None
            ),
            language=whisper_result.language,
            duration_ms=whisper_result.duration_ms,
            timestamp_granularity="segment" if segments else "none",
            fusion_status="ASR_SINGLE_PROVIDER",
            confidence_level="LOW",
            asr_consensus=False,
            final_truth=False,
            raw_metadata={
                "provider_errors": self._provider_errors(
                    funasr_result, whisper_result
                ),
                "final_truth": False,
            },
        )

    def _single_funasr(self, funasr_result, whisper_result):
        return ASRFusionResult(
            full_text=funasr_result.full_text,
            segments=(),
            words=(),
            providers_used=(funasr_result.provider,),
            provider_results=tuple(
                item for item in (funasr_result, whisper_result) if item is not None
            ),
            language=funasr_result.language,
            duration_ms=funasr_result.duration_ms,
            timestamp_granularity="none",
            fusion_status="ASR_SINGLE_PROVIDER",
            confidence_level="LOW",
            asr_consensus=False,
            final_truth=False,
            raw_metadata={
                "timestamp_source": "unavailable",
                "timestamp_quality": "none",
                "provider_errors": self._provider_errors(
                    funasr_result, whisper_result
                ),
                "final_truth": False,
            },
        )

    def _no_provider(self, funasr_result, whisper_result):
        results = tuple(
            item for item in (funasr_result, whisper_result) if item is not None
        )
        return ASRFusionResult(
            full_text="",
            segments=(),
            words=(),
            providers_used=(),
            provider_results=results,
            language="",
            duration_ms=max((item.duration_ms for item in results), default=0),
            timestamp_granularity="none",
            fusion_status="ASR_NO_PROVIDER_RESULT",
            confidence_level="LOW",
            asr_consensus=False,
            final_truth=False,
            raw_metadata={
                "timestamp_source": "unavailable",
                "timestamp_quality": "none",
                "provider_errors": self._provider_errors(*results),
                "final_truth": False,
            },
        )

    def fuse(self, funasr_result, whisper_result):
        funasr_available = self._available(funasr_result)
        whisper_available = self._available(whisper_result)
        if whisper_available and not funasr_available:
            return self._single_whisper(funasr_result, whisper_result)
        if funasr_available and not whisper_available:
            return self._single_funasr(funasr_result, whisper_result)
        if not funasr_available and not whisper_available:
            return self._no_provider(funasr_result, whisper_result)

        alignment = align_text_to_segments(
            funasr_result.full_text, whisper_result.segments
        )
        fused_segments = []
        for segment_index, (whisper_segment, segment_alignment) in enumerate(zip(
            whisper_result.segments, alignment.segments
        )):
            start_ms, end_ms, timestamp_adjustment = self._normalize_bounds(
                whisper_segment.start_ms,
                whisper_segment.end_ms,
                whisper_result.duration_ms,
            )
            raw_funasr_text = segment_alignment.funasr_text
            whisper_text = whisper_segment.text
            conflicts = self._critical_conflicts(raw_funasr_text, whisper_text)
            domain_candidates = self._domain_candidates(
                raw_funasr_text, whisper_text
            )
            alternatives = tuple(dict.fromkeys(
                value for value in (raw_funasr_text, whisper_text)
                if value and value != whisper_text
            ))
            if conflicts:
                selected_token = next((
                    conflict.selected_value for conflict in conflicts
                    if conflict.selected_value
                ), "")
                funasr_tokens = {
                    token.value for token in extract_critical_tokens(raw_funasr_text)
                }
                whisper_tokens = {
                    token.value for token in extract_critical_tokens(whisper_text)
                }
                if (
                    selected_token
                    and selected_token in funasr_tokens
                    and selected_token not in whisper_tokens
                ):
                    selected_text = raw_funasr_text
                    text_source = "funasr"
                    timestamp_quality = "aligned"
                    candidate_label = "FunASR candidate"
                else:
                    selected_text = whisper_text
                    text_source = "whispercpp"
                    timestamp_quality = "native"
                    candidate_label = "Whisper candidate"
                selected_with_glossary = any(
                    "domain glossary support" in item.selection_reason
                    for item in conflicts
                )
                reason = (
                    f"{candidate_label}; "
                    + ("domain glossary support; " if selected_with_glossary else "")
                    + "provider conflict remains; final_truth=false"
                )
                status = "ASR_CRITICAL_TOKEN_CONFLICT"
                confidence = "LOW"
                consensus = False
            elif segment_alignment.metrics.quality_level == "LOW":
                selected_text = whisper_text
                text_source = "whispercpp"
                timestamp_quality = "native"
                status = "ASR_UNRESOLVED_CONFLICT"
                confidence = "LOW"
                consensus = False
                reason = "low_alignment_quality_fallback_to_whisper"
            else:
                funasr_text = self._safe_punctuated_text(
                    funasr_result.full_text,
                    segment_alignment,
                    segment_index,
                    len(whisper_result.segments),
                )
                if funasr_text == whisper_text:
                    selected_text = whisper_text
                    text_source = "whispercpp"
                    timestamp_quality = "native"
                    status = "ASR_CONSENSUS_EXACT"
                    confidence = "HIGH"
                    consensus = True
                    reason = "providers returned identical aligned text"
                elif (
                    normalize_for_comparison(funasr_text)
                    == normalize_for_comparison(whisper_text)
                ):
                    selected_text = funasr_text
                    text_source = "fused"
                    timestamp_quality = "aligned"
                    status = "ASR_CONSENSUS_SIMILAR"
                    confidence = "HIGH"
                    consensus = True
                    reason = (
                        "projected FunASR native punctuation through high alignment"
                    )
                elif (
                    len(normalize_for_comparison(funasr_text))
                    > len(normalize_for_comparison(whisper_text))
                ):
                    selected_text = funasr_text
                    text_source = "funasr"
                    timestamp_quality = "aligned"
                    status = "ASR_TEXT_CONFLICT"
                    confidence = "MEDIUM"
                    consensus = False
                    reason = "selected FunASR aligned lexical completeness"
                else:
                    selected_text = whisper_text
                    text_source = "whispercpp"
                    timestamp_quality = "native"
                    status = "ASR_TEXT_CONFLICT"
                    confidence = "MEDIUM"
                    consensus = False
                    reason = "retained Whisper candidate on equal aligned evidence"
            fused_segments.append(ASRFusedSegment(
                start_ms=start_ms,
                end_ms=end_ms,
                raw_start_ms=whisper_segment.start_ms,
                raw_end_ms=whisper_segment.end_ms,
                fused_text=selected_text,
                text_source=text_source,
                timestamp_source="whispercpp",
                timestamp_quality=timestamp_quality,
                timestamp_adjustment=timestamp_adjustment,
                provider_texts={
                    "funasr": raw_funasr_text,
                    "whispercpp": whisper_text,
                },
                alternatives=alternatives,
                conflicts=conflicts,
                critical_tokens=extract_critical_tokens(selected_text),
                selection_reason=reason,
                alignment_score=segment_alignment.metrics.alignment_score,
                alignment_metrics=segment_alignment.metrics,
                fusion_status=status,
                confidence_level=confidence,
                asr_consensus=consensus,
                final_truth=False,
                domain_candidates=domain_candidates,
                raw_metadata={
                    "funasr_raw_start": segment_alignment.funasr_raw_start,
                    "funasr_raw_end": segment_alignment.funasr_raw_end,
                },
            ))

        rank = {
            "ASR_CRITICAL_TOKEN_CONFLICT": 5,
            "ASR_UNRESOLVED_CONFLICT": 4,
            "ASR_TEXT_CONFLICT": 3,
            "ASR_CONSENSUS_SIMILAR": 2,
            "ASR_CONSENSUS_EXACT": 1,
        }
        all_fused_segments = tuple(fused_segments)
        omitted_segments = tuple(
            item for item in all_fused_segments if item.end_ms <= item.start_ms
        )
        rendered_segments = tuple(
            item for item in all_fused_segments if item.end_ms > item.start_ms
        )
        result_status = max(
            (item.fusion_status for item in all_fused_segments),
            key=lambda value: rank[value],
            default="ASR_NO_PROVIDER_RESULT",
        )
        confidence = (
            "LOW" if any(item.confidence_level == "LOW" for item in all_fused_segments)
            else "MEDIUM" if any(item.confidence_level == "MEDIUM" for item in all_fused_segments)
            else "HIGH"
        )
        return ASRFusionResult(
            full_text="".join(item.fused_text for item in all_fused_segments),
            segments=rendered_segments,
            words=(),
            providers_used=(funasr_result.provider, whisper_result.provider),
            provider_results=(funasr_result, whisper_result),
            language=whisper_result.language or funasr_result.language,
            duration_ms=whisper_result.duration_ms,
            timestamp_granularity="segment",
            fusion_status=result_status,
            confidence_level=confidence,
            asr_consensus=bool(all_fused_segments) and all(
                item.asr_consensus for item in all_fused_segments
            ),
            final_truth=False,
            raw_metadata={
                "alignment": alignment.metrics.to_dict(),
                "glossary_version": self.glossary.version,
                "glossary_fingerprint": self.glossary.fingerprint,
                "provider_errors": self._provider_errors(
                    funasr_result, whisper_result
                ),
                "omitted_zero_duration_segments": [
                    {
                        "raw_start_ms": item.raw_start_ms,
                        "raw_end_ms": item.raw_end_ms,
                        "fused_text": item.fused_text,
                        "timestamp_adjustment": item.timestamp_adjustment,
                    }
                    for item in omitted_segments
                ],
                "warnings": (
                    ["ZERO_DURATION_SEGMENT_OMITTED"]
                    if omitted_segments else []
                ),
                "final_truth": False,
            },
        )
