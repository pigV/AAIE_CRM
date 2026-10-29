from dataclasses import dataclass
from difflib import SequenceMatcher

from ..models import ASRAlignmentMetrics
from ..shared.recognition_text import traceable_normalize


@dataclass(frozen=True)
class AlignmentOperation:
    operation: str
    funasr_index: int | None
    whisper_index: int | None


@dataclass(frozen=True)
class SegmentTextAlignment:
    segment_index: int
    whisper_text: str
    funasr_text: str
    funasr_raw_start: int | None
    funasr_raw_end: int | None
    metrics: ASRAlignmentMetrics


@dataclass(frozen=True)
class ASRTextAlignment:
    funasr_normalized: str
    whisper_normalized: str
    funasr_to_whisper_segment: tuple[int | None, ...]
    operations: tuple[AlignmentOperation, ...]
    segments: tuple[SegmentTextAlignment, ...]
    metrics: ASRAlignmentMetrics


def _needleman_wunsch(left, right, left_offset, right_offset):
    rows = len(left) + 1
    columns = len(right) + 1
    costs = [[0] * columns for _ in range(rows)]
    paths = [[None] * columns for _ in range(rows)]
    for row in range(1, rows):
        costs[row][0] = row
        paths[row][0] = "insert"
    for column in range(1, columns):
        costs[0][column] = column
        paths[0][column] = "delete"
    for row in range(1, rows):
        for column in range(1, columns):
            same = left[row - 1] == right[column - 1]
            candidates = (
                (costs[row - 1][column - 1] + (not same), 0,
                 "match" if same else "substitute"),
                (costs[row - 1][column] + 1, 1, "insert"),
                (costs[row][column - 1] + 1, 2, "delete"),
            )
            cost, _, operation = min(candidates)
            costs[row][column] = cost
            paths[row][column] = operation
    output = []
    row = len(left)
    column = len(right)
    while row or column:
        operation = paths[row][column]
        if operation in {"match", "substitute"}:
            row -= 1
            column -= 1
            output.append(AlignmentOperation(
                operation, left_offset + row, right_offset + column
            ))
        elif operation == "insert":
            row -= 1
            output.append(AlignmentOperation(
                operation, left_offset + row, None
            ))
        else:
            column -= 1
            output.append(AlignmentOperation(
                "delete", None, right_offset + column
            ))
    output.reverse()
    return output


def _ordered_alignment(left, right):
    operations = []
    left_cursor = 0
    right_cursor = 0
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    for block in matcher.get_matching_blocks():
        operations.extend(_needleman_wunsch(
            left[left_cursor:block.a],
            right[right_cursor:block.b],
            left_cursor,
            right_cursor,
        ))
        for offset in range(block.size):
            operations.append(AlignmentOperation(
                "match", block.a + offset, block.b + offset
            ))
        left_cursor = block.a + block.size
        right_cursor = block.b + block.size
    return tuple(operations)


def _has_repeated_ambiguity(left, right, minimum=4):
    if len(left) < minimum or len(right) < minimum:
        return False
    seen = set()
    for index in range(len(left) - minimum + 1):
        value = left[index:index + minimum]
        if value in seen:
            continue
        seen.add(value)
        if left.count(value) > 1 and right.count(value) > 1:
            return True
    return False


def _quality_metrics(
    operations,
    source_length,
    target_length,
    mapped_count,
    warnings=(),
    boundary_anchor_score=1.0,
):
    matched = sum(item.operation == "match" for item in operations)
    substituted = sum(item.operation == "substitute" for item in operations)
    inserted = sum(item.operation == "insert" for item in operations)
    deleted = sum(item.operation == "delete" for item in operations)
    unmatched = substituted + inserted + deleted
    denominator = max(source_length, target_length, 1)
    char_match_ratio = matched / denominator
    coverage_ratio = mapped_count / max(source_length, 1)
    score = (
        0.60 * char_match_ratio
        + 0.30 * coverage_ratio
        + 0.10 * boundary_anchor_score
    )
    warning_set = tuple(dict.fromkeys(warnings))
    ambiguous = bool({
        "AMBIGUOUS_REPEATED_TEXT", "CROSS_SEGMENT_INSERTION"
    }.intersection(warning_set))
    if (
        not ambiguous
        and score >= 0.90
        and coverage_ratio >= 0.90
        and boundary_anchor_score >= 0.5
    ):
        quality = "HIGH"
    elif (
        not ambiguous
        and score >= 0.75
        and coverage_ratio >= 0.75
    ):
        quality = "MEDIUM"
    else:
        quality = "LOW"
    return ASRAlignmentMetrics(
        alignment_score=round(score, 6),
        matched_chars=matched,
        substituted_chars=substituted,
        inserted_chars=inserted,
        deleted_chars=deleted,
        unmatched_chars=unmatched,
        coverage_ratio=round(coverage_ratio, 6),
        char_match_ratio=round(char_match_ratio, 6),
        boundary_anchor_score=float(boundary_anchor_score),
        quality_level=quality,
        warnings=warning_set,
    )


def align_text_to_segments(funasr_text, whisper_segments):
    whisper_segments = tuple(whisper_segments or ())
    funasr = traceable_normalize(funasr_text)
    whisper_parts = tuple(traceable_normalize(item.text) for item in whisper_segments)
    whisper_text = "".join(item.normalized for item in whisper_parts)
    whisper_segment_indexes = []
    for segment_index, item in enumerate(whisper_parts):
        whisper_segment_indexes.extend(segment_index for _ in item.normalized)

    operations = _ordered_alignment(funasr.normalized, whisper_text)
    mapping = [None] * len(funasr.normalized)
    for item in operations:
        if item.funasr_index is None or item.whisper_index is None:
            continue
        mapping[item.funasr_index] = whisper_segment_indexes[item.whisper_index]

    warnings = []
    for position, item in enumerate(operations):
        if item.operation != "insert" or item.funasr_index is None:
            continue
        previous = next((
            mapping[other.funasr_index]
            for other in reversed(operations[:position])
            if other.funasr_index is not None
            and mapping[other.funasr_index] is not None
        ), None)
        following = next((
            mapping[other.funasr_index]
            for other in operations[position + 1:]
            if other.funasr_index is not None
            and mapping[other.funasr_index] is not None
        ), None)
        if previous is not None and previous == following:
            mapping[item.funasr_index] = previous
        elif previous is not None and following is not None:
            warnings.append("CROSS_SEGMENT_INSERTION")

    repeated_ambiguity = _has_repeated_ambiguity(
        funasr.normalized, whisper_text
    )
    if repeated_ambiguity:
        warnings.append("AMBIGUOUS_REPEATED_TEXT")
    boundary_score = 0.0 if repeated_ambiguity else (
        1.0 if mapping and mapping[0] is not None and mapping[-1] is not None
        else 0.5 if any(item is not None for item in mapping)
        else 0.0
    )
    metrics = _quality_metrics(
        operations,
        len(funasr.normalized),
        len(whisper_text),
        sum(item is not None for item in mapping),
        warnings,
        boundary_score,
    )

    segment_alignments = []
    for segment_index, segment in enumerate(whisper_segments):
        source_positions = [
            index for index, mapped in enumerate(mapping)
            if mapped == segment_index
        ]
        target_positions = [
            index for index, mapped in enumerate(whisper_segment_indexes)
            if mapped == segment_index
        ]
        segment_operations = tuple(
            item for item in operations
            if (
                item.whisper_index in target_positions
                if item.whisper_index is not None else
                item.funasr_index in source_positions
            )
        )
        if source_positions:
            normalized_start = min(source_positions)
            normalized_end = max(source_positions)
            raw_start = funasr.trace[normalized_start].raw_start
            raw_end = funasr.trace[normalized_end].raw_end
            candidate = funasr.raw[raw_start:raw_end]
            source_span_length = normalized_end - normalized_start + 1
            mapped_count = len(source_positions)
        else:
            raw_start = None
            raw_end = None
            candidate = ""
            source_span_length = 0
            mapped_count = 0
        segment_warnings = tuple(
            warning for warning in metrics.warnings
            if warning == "AMBIGUOUS_REPEATED_TEXT"
        )
        segment_boundary = 0.0 if repeated_ambiguity else (
            1.0 if source_positions else 0.0
        )
        segment_metrics = _quality_metrics(
            segment_operations,
            source_span_length,
            len(target_positions),
            mapped_count,
            segment_warnings,
            segment_boundary,
        )
        segment_alignments.append(SegmentTextAlignment(
            segment_index=segment_index,
            whisper_text=segment.text,
            funasr_text=candidate,
            funasr_raw_start=raw_start,
            funasr_raw_end=raw_end,
            metrics=segment_metrics,
        ))

    return ASRTextAlignment(
        funasr_normalized=funasr.normalized,
        whisper_normalized=whisper_text,
        funasr_to_whisper_segment=tuple(mapping),
        operations=operations,
        segments=tuple(segment_alignments),
        metrics=metrics,
    )
