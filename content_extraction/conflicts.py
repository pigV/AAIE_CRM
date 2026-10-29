import re

from .models import OCRConflict


_ENTITY_PATTERNS = (
    ("日期", re.compile(r"(?:19|20)\d{2}[年./-]\d{1,2}(?:[月./-]\d{1,2}日?)?")),
    ("年份", re.compile(r"(?:19|20)\d{2}年?")),
    ("金额", re.compile(r"(?i)(?:AUD|RMB|CNY|USD|A\$|\$|￥)\s*[\d,]+(?:\.\d+)?|[\d,]+(?:\.\d+)?\s*(?:澳元|元|万元)")),
    ("百分比", re.compile(r"\d+(?:\.\d+)?%")),
    ("年级", re.compile(r"(?i)(?:grade\s*\d{1,2}|\d{1,2}\s*年级)")),
    ("年龄", re.compile(r"\d{1,3}\s*岁")),
    ("签证类别", re.compile(r"(?i)(?:subclass\s*\d{3}|\d{3}\s*(?:类)?签证|学生签证|陪读签证)")),
    ("电话号码", re.compile(r"(?<!\d)(?:\+?\d[\s-]?){8,14}(?!\d)")),
    ("网址", re.compile(r"(?i)(?:https?://)?(?:[a-z0-9-]+\.)+(?:com|net|org|edu|gov)(?:\.[a-z]{2})?(?:/[^\s]*)?")),
    ("学校名称", re.compile(r"(?i)[A-Za-z][A-Za-z &'’-]{1,50}(?:University|College|School)|[\u4e00-\u9fff]{2,20}(?:大学|学校|学院)")),
)

_PLACES = (
    "悉尼", "墨尔本", "布里斯班", "珀斯", "阿德莱德", "堪培拉", "霍巴特",
    "Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide", "Canberra", "Hobart",
    "NSW", "VIC", "QLD", "SA", "WA", "TAS", "ACT", "NT",
)


def _unique(values):
    return tuple(dict.fromkeys(str(value) for value in values))


def _entity_values(category, pattern, texts):
    from .ocr.consensus import normalize_chinese_numbers

    values = set()
    for text in texts:
        normalized = normalize_chinese_numbers(str(text))
        values.update(match.group(0).lower().replace(" ", "") for match in pattern.finditer(normalized))
    return values


def classify_conflicts(segment_id, recommended_text, variants):
    variants = _unique(variants)
    if len(variants) <= 1:
        return ()
    conflicts = []
    for category, pattern in _ENTITY_PATTERNS:
        if len(_entity_values(category, pattern, variants)) > 1:
            conflicts.append(
                OCRConflict(segment_id, category, "high", recommended_text, variants)
            )

    place_sets = []
    for text in variants:
        lower = text.lower()
        place_sets.append({place.lower() for place in _PLACES if place.lower() in lower})
    if len({tuple(sorted(values)) for values in place_sets}) > 1 and any(place_sets):
        conflicts.append(
            OCRConflict(segment_id, "城市和州名", "high", recommended_text, variants)
        )

    if conflicts:
        return tuple(conflicts)

    from .ocr.consensus import normalize_recognition_text

    normalized = {normalize_recognition_text(text) for text in variants}
    category = "标点或断句" if len(normalized) == 1 else "文字差异"
    return (OCRConflict(segment_id, category, "yellow", recommended_text, variants),)
