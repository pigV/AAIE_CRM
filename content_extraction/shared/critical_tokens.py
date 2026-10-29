import re
import unicodedata
from dataclasses import dataclass

from .recognition_text import normalize_chinese_numbers


_KNOWN_ACRONYMS = {
    "VISA", "VCE", "IB", "ATAR", "IELTS", "EAL", "ESL", "GPA",
    "COE", "OSHC",
}
_PATTERNS = (
    ("DATE", re.compile(r"(?:19|20)\d{2}(?:[年./-]\d{1,2}(?:[月./-]\d{1,2}日?)?)?")),
    ("AMOUNT", re.compile(r"(?i)(?:AUD|RMB|CNY|USD|A\$|\$|￥)\s*[\d,]+(?:\.\d+)?|[\d,]+(?:\.\d+)?\s*(?:澳元|元|万元)")),
    ("PERCENTAGE", re.compile(r"\d+(?:\.\d+)?%")),
    ("AGE", re.compile(r"\d{1,3}\s*岁")),
    ("VISA_SUBCLASS", re.compile(r"(?i)(?:subclass\s*)?(?:500|590|600|485|482)(?=\s*(?:类)?签证|\b)")),
    ("PROPER_NAME", re.compile(r"(?i)(?:[A-Z][A-Za-z&'’.-]*\s+){1,5}(?:University|College|School)|[\u4e00-\u9fff]{2,20}(?:大学|学校|学院)")),
    ("NUMBER", re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?![A-Za-z0-9])")),
)
_ASCII_WORD = re.compile(r"[A-Za-z]{2,10}")
_PROVIDER_CONTROL_TAG = re.compile(r"<\|[^|]*\|>")


@dataclass(frozen=True)
class CriticalToken:
    token_type: str
    value: str

    def to_dict(self):
        return {"token_type": self.token_type, "value": self.value}


@dataclass(frozen=True)
class CriticalTokenConflict:
    token_type: str
    values: tuple


def _normalized_text(text):
    sanitized = _PROVIDER_CONTROL_TAG.sub("", str(text or ""))
    return normalize_chinese_numbers(unicodedata.normalize("NFKC", sanitized))


def extract_critical_tokens(text):
    value = _normalized_text(text)
    found = []
    for token_type, pattern in _PATTERNS:
        for match in pattern.finditer(value):
            normalized = re.sub(r"\s+", "", match.group(0)).upper()
            found.append(CriticalToken(token_type, normalized))
    for match in _ASCII_WORD.finditer(value):
        raw = match.group(0)
        upper = raw.upper()
        if upper in _KNOWN_ACRONYMS or raw.isupper():
            found.append(CriticalToken("ASCII_ACRONYM", upper))
    return tuple(dict.fromkeys(found))


def critical_token_conflicts(results):
    successful = tuple(
        result for result in results if result.status == "completed" and result.text
    )
    if len(successful) < 2:
        return ()
    by_provider = {}
    token_types = set()
    for result in successful:
        grouped = {}
        for token in extract_critical_tokens(result.text):
            grouped.setdefault(token.token_type, set()).add(token.value)
            token_types.add(token.token_type)
        by_provider[result.provider] = grouped
    conflicts = []
    for token_type in sorted(token_types):
        values = tuple(
            (result.provider, tuple(sorted(by_provider[result.provider].get(token_type, ()))))
            for result in successful
        )
        if len({item[1] for item in values}) > 1:
            conflicts.append(CriticalTokenConflict(token_type, values))
    return tuple(conflicts)
