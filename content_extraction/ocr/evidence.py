from dataclasses import dataclass


@dataclass(frozen=True)
class OCRCrossModalEvidence:
    text: str
    start_ms: int
    end_ms: int
    frame_ids: tuple[str, ...]
    missing_frame_ids: tuple[str, ...]
    fusion_statuses: tuple[str, ...]
    confidence_levels: tuple[str, ...]
    providers_used: tuple[str, ...]
    provider_count: int
    agreement_count: int
    alternatives: tuple[str, ...]
    conflicts: tuple
    critical_tokens: tuple
    bounding_region: dict
    selection_reasons: tuple[str, ...]
    provider_provenance: tuple[dict, ...]
    local_consensus: bool
    cloud_arbitrated: bool
    cloud_call_count: int
    provenance_complete: bool
    evidence_completeness: str
    text_role: str

    def to_dict(self):
        return {
            "text": self.text,
            "start_ms": int(self.start_ms),
            "end_ms": int(self.end_ms),
            "frame_ids": list(self.frame_ids),
            "missing_frame_ids": list(self.missing_frame_ids),
            "fusion_statuses": list(self.fusion_statuses),
            "confidence_levels": list(self.confidence_levels),
            "providers_used": list(self.providers_used),
            "provider_count": int(self.provider_count),
            "agreement_count": int(self.agreement_count),
            "alternatives": list(self.alternatives),
            "conflicts": [_serialize(item) for item in self.conflicts],
            "critical_tokens": [_serialize(item) for item in self.critical_tokens],
            "bounding_region": dict(self.bounding_region),
            "selection_reasons": list(self.selection_reasons),
            "provider_provenance": [dict(item) for item in self.provider_provenance],
            "local_consensus": bool(self.local_consensus),
            "cloud_arbitrated": bool(self.cloud_arbitrated),
            "cloud_call_count": int(self.cloud_call_count),
            "provenance_complete": bool(self.provenance_complete),
            "evidence_completeness": self.evidence_completeness,
            "text_role": self.text_role,
        }


def _serialize(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return value


def _unique(values):
    output = []
    for item in values:
        if item in (None, "") or any(item == existing for existing in output):
            continue
        output.append(item)
    return tuple(output)


def _bounding_union(regions):
    valid = []
    for item in regions:
        try:
            x = int(item["x"])
            y = int(item["y"])
            width = int(item["width"])
            height = int(item["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if width > 0 and height > 0:
            valid.append((x, y, x + width, y + height))
    if not valid:
        return {}
    left = min(item[0] for item in valid)
    top = min(item[1] for item in valid)
    right = max(item[2] for item in valid)
    bottom = max(item[3] for item in valid)
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


class OCRCrossModalEvidenceResolver:
    """Build a redacted evidence envelope from OCR frame-fusion provenance."""

    def __init__(self, transcript):
        self.transcript = transcript
        self._frames = {
            item.frame_id: item
            for item in getattr(transcript, "frame_fusions", ())
            if getattr(item, "frame_id", "")
        }

    def resolve(self, segment):
        frames = tuple(
            self._frames[frame_id]
            for frame_id in segment.frame_ids
            if frame_id in self._frames
        )
        missing = tuple(
            frame_id for frame_id in segment.frame_ids
            if frame_id not in self._frames
        )
        if not frames:
            completeness = "missing"
        elif missing:
            completeness = "partial"
        else:
            completeness = "complete"
        providers = _unique(
            provider for item in frames
            for provider in getattr(item, "providers_used", ())
        )
        statuses = _unique((
            *getattr(segment, "fusion_statuses", ()),
            *(getattr(item, "fusion_status", "") for item in frames),
        ))
        confidence_levels = _unique(
            getattr(item, "confidence_level", "") for item in frames
        )
        alternatives = _unique(
            value for item in frames
            for value in getattr(item, "alternatives", ())
        )
        conflicts = _unique(
            value for item in frames
            for value in getattr(item, "conflicts", ())
        )
        critical_tokens = _unique(
            value for item in frames
            for value in getattr(item, "critical_tokens", ())
        )
        reasons = _unique(
            getattr(item, "selection_reason", "") for item in frames
        )
        frame_key = "|".join(segment.frame_ids)
        role = getattr(self.transcript, "segment_roles", {}).get(frame_key)
        if not role:
            role = next((
                getattr(self.transcript, "segment_roles", {}).get(frame_id)
                for frame_id in segment.frame_ids
                if getattr(self.transcript, "segment_roles", {}).get(frame_id)
            ), "unknown")
        provenance = tuple({
            "frame_id": item.frame_id,
            "fusion_status": getattr(item, "fusion_status", ""),
            "providers_used": list(getattr(item, "providers_used", ())),
            "agreement_count": int(getattr(item, "agreement_count", 0) or 0),
            "selected_source": getattr(item, "selected_source", ""),
            "cloud_called": bool(getattr(item, "cloud_called", False)),
            "cloud_call_count": int(getattr(item, "cloud_call_count", 0) or 0),
        } for item in frames)
        return OCRCrossModalEvidence(
            text=segment.text,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            frame_ids=tuple(segment.frame_ids),
            missing_frame_ids=missing,
            fusion_statuses=statuses,
            confidence_levels=confidence_levels,
            providers_used=providers,
            provider_count=len(providers),
            agreement_count=max(
                (int(getattr(item, "agreement_count", 0) or 0) for item in frames),
                default=0,
            ),
            alternatives=alternatives,
            conflicts=conflicts,
            critical_tokens=critical_tokens,
            bounding_region=_bounding_union(
                getattr(item, "bounding_region", {}) for item in frames
            ),
            selection_reasons=reasons,
            provider_provenance=provenance,
            local_consensus=any(
                status in {"LOCAL_CONSENSUS_EXACT", "LOCAL_CONSENSUS_SIMILAR"}
                for status in statuses
            ),
            cloud_arbitrated=any(
                status in {"CLOUD_CONSENSUS", "CLOUD_ARBITRATED_MAJORITY"}
                for status in statuses
            ),
            cloud_call_count=sum(
                int(getattr(item, "cloud_call_count", 0) or 0) for item in frames
            ),
            provenance_complete=completeness == "complete",
            evidence_completeness=completeness,
            text_role=str(role or "unknown"),
        )
