import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher


_CHINESE_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CHINESE_NUMBER = re.compile(r"[零〇一二两三四五六七八九十]{1,3}")
_COMPARISON_PUNCTUATION = re.compile(
    r"[\s，。！？、；：,.!?;:'\"“”‘’（）()\[\]【】《》<>—–_\-]+"
)


@dataclass(frozen=True)
class NormalizedSpan:
    raw_start: int
    raw_end: int


@dataclass(frozen=True)
class TraceableText:
    raw: str
    normalized: str
    trace: tuple[NormalizedSpan, ...]


@dataclass(frozen=True)
class SimilaritySignals:
    exact: bool
    ratio: float
    levenshtein_distance: int
    prefix_overlap: str
    suffix_prefix_overlap: str


def _parse_chinese_number(value):
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        tens = _CHINESE_DIGITS.get(left, 1) if left else 1
        ones = _CHINESE_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    digits = "".join(str(_CHINESE_DIGITS[char]) for char in value)
    return int(digits) if digits else 0


def normalize_chinese_numbers(text):
    return _CHINESE_NUMBER.sub(
        lambda match: str(_parse_chinese_number(match.group(0))),
        str(text or ""),
    )


def _nfkc_with_trace(raw):
    chars = []
    spans = []
    for index, char in enumerate(raw):
        for normalized_char in unicodedata.normalize("NFKC", char):
            chars.append(normalized_char)
            spans.append(NormalizedSpan(index, index + 1))
    return chars, spans


def _numbers_with_trace(chars, spans):
    value = "".join(chars)
    output_chars = []
    output_spans = []
    cursor = 0
    for match in _CHINESE_NUMBER.finditer(value):
        output_chars.extend(chars[cursor:match.start()])
        output_spans.extend(spans[cursor:match.start()])
        replacement = str(_parse_chinese_number(match.group(0)))
        raw_span = NormalizedSpan(
            spans[match.start()].raw_start,
            spans[match.end() - 1].raw_end,
        )
        output_chars.extend(replacement)
        output_spans.extend(raw_span for _ in replacement)
        cursor = match.end()
    output_chars.extend(chars[cursor:])
    output_spans.extend(spans[cursor:])
    return output_chars, output_spans


def traceable_normalize(text):
    raw = str(text or "")
    chars, spans = _nfkc_with_trace(raw)
    chars, spans = _numbers_with_trace(chars, spans)
    output_chars = []
    output_spans = []
    for char, span in zip(chars, spans):
        if _COMPARISON_PUNCTUATION.fullmatch(char):
            continue
        folded = char.casefold()
        output_chars.extend(folded)
        output_spans.extend(span for _ in folded)
    return TraceableText(raw, "".join(output_chars), tuple(output_spans))


def normalize_for_comparison(text):
    return traceable_normalize(text).normalized


def levenshtein_distance(first, second):
    first = str(first or "")
    second = str(second or "")
    if len(first) < len(second):
        first, second = second, first
    previous = list(range(len(second) + 1))
    for row, left in enumerate(first, 1):
        current = [row]
        for column, right in enumerate(second, 1):
            current.append(min(
                current[-1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left != right),
            ))
        previous = current
    return previous[-1]


def _longest_suffix_prefix(first, second):
    limit = min(len(first), len(second))
    for size in range(limit, 0, -1):
        if first[-size:] == second[:size]:
            return second[:size]
    return ""


def similarity_signals(first, second):
    left = normalize_for_comparison(first)
    right = normalize_for_comparison(second)
    prefix_size = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char:
            break
        prefix_size += 1
    return SimilaritySignals(
        left == right,
        SequenceMatcher(None, left, right).ratio(),
        levenshtein_distance(left, right),
        left[:prefix_size],
        _longest_suffix_prefix(left, right),
    )
