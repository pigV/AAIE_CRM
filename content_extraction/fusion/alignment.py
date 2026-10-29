from dataclasses import dataclass
from difflib import SequenceMatcher

from ..shared.critical_tokens import extract_critical_tokens
from ..shared.recognition_text import normalize_for_comparison, similarity_signals


@dataclass(frozen=True)
class TemporalOverlap:
    intersection_ms: int
    shorter_coverage_ratio: float
    iou_ratio: float
    midpoint_distance_ms: int
    boundary_gap_ms: int
    strength: str

    def to_dict(self):
        return {
            "intersection_ms": int(self.intersection_ms),
            "shorter_coverage_ratio": float(self.shorter_coverage_ratio),
            "iou_ratio": float(self.iou_ratio),
            "midpoint_distance_ms": int(self.midpoint_distance_ms),
            "boundary_gap_ms": int(self.boundary_gap_ms),
            "strength": self.strength,
        }


@dataclass(frozen=True)
class TemporalEdge:
    ocr_index: int
    asr_index: int
    overlap: TemporalOverlap


@dataclass(frozen=True)
class TemporalComponent:
    ocr_indexes: tuple[int, ...]
    asr_indexes: tuple[int, ...]
    edges: tuple[TemporalEdge, ...]
    weak_edges: tuple[TemporalEdge, ...]


@dataclass(frozen=True)
class TextRelation:
    relation: str
    exact: bool
    ratio: float
    levenshtein_distance: int
    lcs_length: int
    ocr_coverage_ratio: float
    asr_coverage_ratio: float
    critical_tokens_agree: bool

    def to_dict(self):
        return {
            "relation": self.relation,
            "exact": bool(self.exact),
            "ratio": float(self.ratio),
            "levenshtein_distance": int(self.levenshtein_distance),
            "lcs_length": int(self.lcs_length),
            "ocr_coverage_ratio": float(self.ocr_coverage_ratio),
            "asr_coverage_ratio": float(self.asr_coverage_ratio),
            "critical_tokens_agree": bool(self.critical_tokens_agree),
        }


def calculate_temporal_overlap(ocr_start, ocr_end, asr_start, asr_end):
    ocr_start, ocr_end = int(ocr_start), int(ocr_end)
    asr_start, asr_end = int(asr_start), int(asr_end)
    ocr_duration = max(0, ocr_end - ocr_start)
    asr_duration = max(0, asr_end - asr_start)
    intersection = max(0, min(ocr_end, asr_end) - max(ocr_start, asr_start))
    shorter = min(ocr_duration, asr_duration)
    union = max(ocr_end, asr_end) - min(ocr_start, asr_start)
    shorter_coverage = intersection / shorter if shorter > 0 else 0.0
    iou = intersection / union if union > 0 else 0.0
    midpoint = abs((ocr_start + ocr_end) - (asr_start + asr_end)) / 2
    if intersection:
        gap = 0
    else:
        gap = max(0, max(ocr_start, asr_start) - min(ocr_end, asr_end))
    if (
        intersection > 0
        and shorter_coverage >= 0.70
        and iou >= 0.50
        and midpoint <= 1000
    ):
        strength = "STRONG"
    elif intersection > 0 and shorter_coverage >= 0.35:
        strength = "MODERATE"
    elif intersection > 0 or gap <= 250:
        strength = "WEAK"
    else:
        strength = "NONE"
    return TemporalOverlap(
        intersection,
        round(shorter_coverage, 6),
        round(iou, 6),
        int(round(midpoint)),
        int(gap),
        strength,
    )


def build_temporal_components(ocr_segments, asr_segments):
    ocr_segments = tuple(ocr_segments or ())
    asr_segments = tuple(asr_segments or ())
    edges = []
    weak_edges = []
    adjacency = {}
    for ocr_index, ocr in enumerate(ocr_segments):
        for asr_index, asr in enumerate(asr_segments):
            overlap = calculate_temporal_overlap(
                ocr.start_ms, ocr.end_ms, asr.start_ms, asr.end_ms
            )
            edge = TemporalEdge(ocr_index, asr_index, overlap)
            if overlap.strength in {"STRONG", "MODERATE"}:
                edges.append(edge)
                left = ("ocr", ocr_index)
                right = ("asr", asr_index)
                adjacency.setdefault(left, set()).add(right)
                adjacency.setdefault(right, set()).add(left)
            elif overlap.strength == "WEAK":
                weak_edges.append(edge)

    nodes = {
        *(("ocr", index) for index in range(len(ocr_segments))),
        *(("asr", index) for index in range(len(asr_segments))),
    }
    visited = set()
    components = []
    for node in nodes:
        if node in visited:
            continue
        pending = [node]
        members = set()
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            members.add(current)
            pending.extend(adjacency.get(current, ()))
        ocr_indexes = tuple(sorted(index for kind, index in members if kind == "ocr"))
        asr_indexes = tuple(sorted(index for kind, index in members if kind == "asr"))
        component_edges = tuple(
            edge for edge in edges
            if edge.ocr_index in ocr_indexes and edge.asr_index in asr_indexes
        )
        component_weak = tuple(
            edge for edge in weak_edges
            if edge.ocr_index in ocr_indexes or edge.asr_index in asr_indexes
        )
        components.append(TemporalComponent(
            ocr_indexes, asr_indexes, component_edges, component_weak
        ))

    def component_key(component):
        times = [
            (ocr_segments[index].start_ms, ocr_segments[index].end_ms, "ocr", index)
            for index in component.ocr_indexes
        ] + [
            (asr_segments[index].start_ms, asr_segments[index].end_ms, "asr", index)
            for index in component.asr_indexes
        ]
        return min(times) if times else (0, 0, "", 0)

    return tuple(sorted(components, key=component_key))


def _token_groups(text):
    groups = {}
    for token in extract_critical_tokens(text):
        groups.setdefault(token.token_type, set()).add(token.value)
    return {key: tuple(sorted(values)) for key, values in groups.items()}


def _lcs_length(left, right):
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    return sum(item.size for item in matcher.get_matching_blocks())


def classify_text_relation(ocr_text, asr_text, temporal_strength):
    ocr_normalized = normalize_for_comparison(ocr_text)
    asr_normalized = normalize_for_comparison(asr_text)
    signals = similarity_signals(ocr_text, asr_text)
    critical_agree = _token_groups(ocr_text) == _token_groups(asr_text)
    lcs = _lcs_length(ocr_normalized, asr_normalized)
    ocr_coverage = lcs / max(len(ocr_normalized), 1)
    asr_coverage = lcs / max(len(asr_normalized), 1)
    if signals.exact:
        relation = "exact"
    elif signals.ratio >= 0.90 and critical_agree:
        relation = "safely_similar"
    elif (
        temporal_strength == "STRONG"
        and critical_agree
        and len(ocr_normalized) < len(asr_normalized)
        and len(ocr_normalized) <= 0.90 * max(len(asr_normalized), 1)
        and ocr_coverage >= 0.85
        and asr_coverage >= 0.55
    ):
        relation = "subtitle_compression"
    else:
        relation = "paraphrase_or_content_difference"
    return TextRelation(
        relation,
        signals.exact,
        round(signals.ratio, 6),
        signals.levenshtein_distance,
        lcs,
        round(ocr_coverage, 6),
        round(asr_coverage, 6),
        critical_agree,
    )
