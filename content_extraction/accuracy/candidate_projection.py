"""Deterministic orthographic precheck and monotonic local-span projection."""

from collections import Counter
from dataclasses import asdict, dataclass
import unicodedata

from .minimal_edit import maximum_lexical_edit_distance
from ..shared.recognition_text import levenshtein_distance


_MAX_ALIGNMENT_CHARACTERS = 512
_MAX_ALIGNMENT_CELLS = 160_000
_MAX_SUBSTANTIVE_SPAN = 12

# Conservative one-to-one writing-form equivalence. This is comparison-only;
# it is never used to generate or replace transcript text.
_TRADITIONAL_TO_SIMPLIFIED = {
    "沒": "没", "觀": "观", "來": "来", "學": "学", "個": "个",
    "這": "这", "國": "国", "種": "种", "們": "们", "會": "会",
    "還": "还", "觸": "触", "為": "为", "說": "说", "應": "应",
    "樂": "乐", "認": "认", "對": "对", "該": "该", "當": "当",
    "離": "离", "開": "开", "語": "语", "習": "习", "給": "给",
    "機": "机", "萬": "万", "興": "兴", "讓": "让", "發": "发",
    "現": "现", "優": "优", "樣": "样", "話": "话", "講": "讲",
    "點": "点", "鄉": "乡", "親": "亲", "環": "环", "飯": "饭",
    "別": "别", "規": "规", "簡": "简", "雖": "虽", "師": "师",
    "帶": "带", "歐": "欧", "於": "于", "幾": "几", "長": "长",
    "園": "园", "藍": "蓝", "費": "费", "緊": "紧", "張": "张",
    "況": "况", "戶": "户", "鮮": "鲜", "氣": "气", "覺": "觉",
    "壓": "压", "禮": "礼", "饋": "馈", "贈": "赠", "參": "参",
    "靈": "灵", "愛": "爱", "歡": "欢", "飛": "飞", "盤": "盘",
    "裡": "里", "結": "结", "識": "识", "養": "养", "級": "级",
    "變": "变", "際": "际", "絕": "绝", "華": "华", "後": "后",
    "謝": "谢", "體": "体", "過": "过", "資": "资", "訊": "讯",
    "處": "处", "動": "动", "間": "间", "務": "务", "較": "较",
    "據": "据", "號": "号", "庫": "库", "達": "达", "錄": "录",
}


def orthographic_key(value):
    output = []
    for char in unicodedata.normalize("NFKC", str(value or "")):
        if unicodedata.category(char).startswith(("P", "Z")):
            continue
        for folded in char.casefold():
            output.append(_TRADITIONAL_TO_SIMPLIFIED.get(folded, folded))
    return "".join(output)


@dataclass(frozen=True)
class CandidateVariantComparison:
    status: str
    carrier_key: str
    candidate_key: str
    difference_types: tuple[str, ...] = ()

    def to_dict(self):
        payload = asdict(self)
        payload["difference_types"] = list(self.difference_types)
        return payload


@dataclass(frozen=True)
class CandidateEditSpan:
    before_span: str
    after_span: str
    start_offset: int
    end_offset: int
    candidate_start_offset: int
    candidate_end_offset: int
    substantive: bool
    difference_type: str

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class CandidateSpanProjection:
    status: str
    carrier_text: str
    candidate_text: str
    spans: tuple[CandidateEditSpan, ...] = ()
    reasons: tuple[str, ...] = ()
    substantive_edit_distance: int = 0
    edit_budget: int = 0

    @property
    def substantive_spans(self):
        return tuple(value for value in self.spans if value.substantive)

    def to_dict(self):
        return {
            "status": self.status,
            "carrier_text": self.carrier_text,
            "candidate_text": self.candidate_text,
            "spans": [value.to_dict() for value in self.spans],
            "reasons": list(self.reasons),
            "substantive_edit_distance": self.substantive_edit_distance,
            "edit_budget": self.edit_budget,
        }


def compare_candidate_variant(carrier_text, candidate_text):
    carrier = str(carrier_text or "")
    candidate = str(candidate_text or "")
    left = orthographic_key(carrier)
    right = orthographic_key(candidate)
    if carrier == candidate:
        return CandidateVariantComparison("EXACT", left, right)
    if left == right:
        types = []
        if unicodedata.normalize("NFKC", carrier).casefold() != unicodedata.normalize("NFKC", candidate).casefold():
            types.append("ORTHOGRAPHIC")
        if any(unicodedata.category(char).startswith("P") for char in carrier + candidate):
            types.append("PUNCTUATION")
        if any(unicodedata.category(char).startswith("Z") for char in carrier + candidate):
            types.append("SPACING")
        if not types:
            types.append("CASE_OR_WIDTH")
        return CandidateVariantComparison(
            "NON_SUBSTANTIVE_VARIANT", left, right, tuple(types),
        )
    return CandidateVariantComparison("SUBSTANTIVE_CONFLICT", left, right, ("LEXICAL",))


def _alignment_operations(carrier, candidate):
    rows = len(carrier) + 1
    columns = len(candidate) + 1
    distances = [[0] * columns for _ in range(rows)]
    for row in range(rows):
        distances[row][0] = row
    for column in range(columns):
        distances[0][column] = column
    for row in range(1, rows):
        for column in range(1, columns):
            if carrier[row - 1] == candidate[column - 1]:
                distances[row][column] = distances[row - 1][column - 1]
            else:
                distances[row][column] = min(
                    distances[row - 1][column - 1] + 1,
                    distances[row - 1][column] + 1,
                    distances[row][column - 1] + 1,
                )

    operations = []
    row, column = len(carrier), len(candidate)
    while row or column:
        if (
            row and column
            and carrier[row - 1] == candidate[column - 1]
            and distances[row][column] == distances[row - 1][column - 1]
        ):
            operations.append(("equal", row - 1, row, column - 1, column))
            row -= 1
            column -= 1
            continue
        if (
            row and column
            and distances[row][column] == distances[row - 1][column - 1] + 1
        ):
            operations.append(("replace", row - 1, row, column - 1, column))
            row -= 1
            column -= 1
            continue
        if row and distances[row][column] == distances[row - 1][column] + 1:
            operations.append(("delete", row - 1, row, column, column))
            row -= 1
            continue
        operations.append(("insert", row, row, column - 1, column))
        column -= 1
    operations.reverse()
    return tuple(operations)


def _group_edit_spans(carrier, candidate, operations):
    groups = []
    current = None
    for kind, start, end, candidate_start, candidate_end in operations:
        if kind == "equal":
            if current is not None:
                groups.append(current)
                current = None
            continue
        if current is None:
            current = [start, end, candidate_start, candidate_end]
            continue
        if start == current[1] and candidate_start == current[3]:
            current[1] = end
            current[3] = candidate_end
        else:
            groups.append(current)
            current = [start, end, candidate_start, candidate_end]
    if current is not None:
        groups.append(current)

    spans = []
    for start, end, candidate_start, candidate_end in groups:
        before = carrier[start:end]
        after = candidate[candidate_start:candidate_end]
        comparison = compare_candidate_variant(before, after)
        substantive = comparison.status == "SUBSTANTIVE_CONFLICT"
        if substantive:
            difference_type = "LEXICAL"
        elif not orthographic_key(before) and not orthographic_key(after):
            difference_type = "PUNCTUATION_OR_SPACING"
        else:
            difference_type = "ORTHOGRAPHIC"
        spans.append(CandidateEditSpan(
            before, after, start, end, candidate_start, candidate_end,
            substantive, difference_type,
        ))
    return tuple(spans)


def apply_edit_spans(carrier_text, spans):
    carrier = str(carrier_text or "")
    ordered = tuple(sorted(spans, key=lambda value: (value.start_offset, value.end_offset)))
    previous_end = 0
    for span in ordered:
        if span.start_offset < previous_end:
            raise ValueError("overlapping_projection_spans")
        if not 0 <= span.start_offset <= span.end_offset <= len(carrier):
            raise ValueError("projection_offset_out_of_bounds")
        if carrier[span.start_offset:span.end_offset] != span.before_span:
            raise ValueError("projection_before_span_mismatch")
        previous_end = span.end_offset
    output = carrier
    for span in reversed(ordered):
        output = output[:span.start_offset] + span.after_span + output[span.end_offset:]
    return output


def project_candidate_spans(carrier_text, candidate_text):
    carrier = str(carrier_text or "")
    candidate = str(candidate_text or "")
    comparison = compare_candidate_variant(carrier, candidate)
    budget = maximum_lexical_edit_distance(carrier)
    if comparison.status == "EXACT":
        return CandidateSpanProjection("EXACT", carrier, candidate, edit_budget=budget)
    if (
        len(carrier) > _MAX_ALIGNMENT_CHARACTERS
        or len(candidate) > _MAX_ALIGNMENT_CHARACTERS
        or (len(carrier) + 1) * (len(candidate) + 1) > _MAX_ALIGNMENT_CELLS
    ):
        return CandidateSpanProjection(
            "PROJECTION_UNSAFE", carrier, candidate,
            reasons=("alignment_input_too_large",), edit_budget=budget,
        )
    if (
        comparison.carrier_key != comparison.candidate_key
        and len(comparison.carrier_key) == len(comparison.candidate_key)
        and Counter(comparison.carrier_key) == Counter(comparison.candidate_key)
    ):
        return CandidateSpanProjection(
            "PROJECTION_UNSAFE", carrier, candidate,
            reasons=("reordered_text",), edit_budget=budget,
        )

    spans = _group_edit_spans(
        carrier, candidate, _alignment_operations(carrier, candidate),
    )
    try:
        reconstructed = apply_edit_spans(carrier, spans)
    except ValueError as exc:
        return CandidateSpanProjection(
            "PROJECTION_UNSAFE", carrier, candidate, spans,
            reasons=(str(exc),), edit_budget=budget,
        )
    if reconstructed != candidate:
        return CandidateSpanProjection(
            "PROJECTION_UNSAFE", carrier, candidate, spans,
            reasons=("projection_reconstruction_mismatch",), edit_budget=budget,
        )

    substantive = tuple(value for value in spans if value.substantive)
    if not substantive:
        return CandidateSpanProjection(
            "NON_SUBSTANTIVE_VARIANT", carrier, candidate, spans,
            edit_budget=budget,
        )
    distance = sum(
        levenshtein_distance(
            orthographic_key(value.before_span),
            orthographic_key(value.after_span),
        )
        for value in substantive
    )
    reasons = []
    if any(
        max(len(value.before_span), len(value.after_span)) > _MAX_SUBSTANTIVE_SPAN
        for value in substantive
    ):
        reasons.append("substantive_span_over_12_characters")
    if distance > budget:
        reasons.append("edit_budget_exceeded")
    if (
        len(substantive) == 1
        and substantive[0].start_offset == 0
        and substantive[0].end_offset == len(carrier)
        and max(len(substantive[0].before_span), len(substantive[0].after_span)) > 2
    ):
        reasons.append("unanchored_whole_text_rewrite")
    return CandidateSpanProjection(
        "PROJECTION_UNSAFE" if reasons else "PROJECTED",
        carrier, candidate, spans, tuple(reasons), distance, budget,
    )
