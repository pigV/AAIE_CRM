from dataclasses import dataclass

from ..shared.critical_tokens import extract_critical_tokens


@dataclass(frozen=True)
class CrossModalConflict:
    conflict_type: str
    category: str
    ocr_values: tuple[str, ...]
    asr_values: tuple[str, ...]
    underlying_provider_values: dict
    alternatives: tuple[str, ...]
    selected_value: str
    resolved: bool
    resolution_reason: str
    requires_human_or_future_evidence: bool

    def to_dict(self):
        return {
            "conflict_type": self.conflict_type,
            "category": self.category,
            "ocr_values": list(self.ocr_values),
            "asr_values": list(self.asr_values),
            "underlying_provider_values": dict(self.underlying_provider_values),
            "alternatives": list(self.alternatives),
            "selected_value": self.selected_value,
            "resolved": bool(self.resolved),
            "resolution_reason": self.resolution_reason,
            "requires_human_or_future_evidence": bool(
                self.requires_human_or_future_evidence
            ),
        }


def _token_groups(text):
    output = {}
    for token in extract_critical_tokens(text):
        output.setdefault(token.token_type, set()).add(token.value)
    return {key: tuple(sorted(values)) for key, values in output.items()}


def detect_cross_modal_conflicts(ocr_text, asr_text, provider_texts=None):
    ocr_groups = _token_groups(ocr_text)
    asr_groups = _token_groups(asr_text)
    provider_texts = dict(provider_texts or {})
    provider_groups = {
        provider: _token_groups(text) for provider, text in provider_texts.items()
    }
    output = []
    for category in sorted(set(ocr_groups) | set(asr_groups)):
        ocr_values = ocr_groups.get(category, ())
        asr_values = asr_groups.get(category, ())
        if ocr_values == asr_values:
            continue
        alternatives = tuple(dict.fromkeys((*ocr_values, *asr_values)))
        output.append(CrossModalConflict(
            conflict_type="CROSS_MODAL_CRITICAL_CONFLICT",
            category=category,
            ocr_values=ocr_values,
            asr_values=asr_values,
            underlying_provider_values={
                provider: list(groups.get(category, ()))
                for provider, groups in provider_groups.items()
            },
            alternatives=alternatives,
            selected_value="",
            resolved=False,
            resolution_reason="cross_modal_critical_token_disagreement",
            requires_human_or_future_evidence=True,
        ))
    return tuple(output)
