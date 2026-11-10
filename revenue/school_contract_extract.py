"""Local, page-attributed extraction for signed school commission drafts."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
import re

from pypdf import PdfReader

FIELDS = {
    "local_rate": "本地首年学费佣金（%）",
    "local_scope": "本地档学费及签证",
    "international_rate": "国际首年学费佣金（%）",
    "international_scope": "国际档学费及签证",
    "frequency": "佣金次数及学费基数",
    "conditions": "学生须满足的条件",
    "agency_application": "代理招募的认定",
    "direct_exclusion": "直接申请",
    "invoice_condition": "付款前的发票要求",
    "start": "协议生效日",
    "term_years": "协议期限（年）",
    "review_months": "协议复核（月）",
    "end": "到期日（含当天）",
    "payment_days": "付款期限（天）",
    "payment_trigger": "付款期限起算事件",
    "gst": "GST",
}
CONFIGURABLE_FIELDS = {
    "rate": "首年学费佣金（%）",
    "frequency": FIELDS["frequency"],
    "start": FIELDS["start"],
    "end": FIELDS["end"],
    "conditions": FIELDS["conditions"],
    "commencement_days": "开学后满多少天",
    "registration_required": "注册凭证（1 需要，0 无需）",
    "payment_days": FIELDS["payment_days"],
    "payment_trigger": FIELDS["payment_trigger"],
    "gst": "GST",
}
ONBOARDING_FIELDS = {
    "school_name": "合同上的学校名称／付款主体",
    "invoice_email": "学校收发票的邮箱",
    "tuition_period": "计佣学费周期",
}
TUITION_PERIODS = {
    "first_year": "首年学费（一次性）",
    "first_half_year": "首半年学费（一次性）",
    "each_year": "每年学费（按年）",
}
FIELD_LABELS = {**ONBOARDING_FIELDS, **FIELDS, **CONFIGURABLE_FIELDS}
RATES = {"local_rate", "international_rate", "rate"}
MAX_PAGES = 40
TERM = re.compile(r"\b(?:the\s+)?term\s+of\s+this\s+agreement\s+is\s+(\d+)\s+years?\s+from\s+the\s+date\s+of\s+the\s+agreement\b", re.I)
REVIEW = re.compile(r"\b(?:this\s+)?agreement\s+will\s+be\s+reviewed\s+after\s+(\d+)\s+months?\b", re.I)
RATE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s+of\s+the\s+first\s+year[’']?s?\s+(local|international)\s+tuition\s+fees", re.I)
FEE_HEADING = re.compile(r"(?m)^\s*(\d+)\s*\.\s*(?:the\s+)?agent[’']s\s+fees\b", re.I)
CLAUSE = re.compile(r"(?m)^[ \t]*([a-f])\.\s*")
SUBCLAUSE = re.compile(r"(?m)^[ \t]*(i{1,3})\.\s*")
DATE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})(?!\d)")
NUMBERED_HEADING = re.compile(r"(?m)^[ \t]*(\d+)\s*\.\s+[A-Z]")
LETTERED_PARAGRAPH = re.compile(r"(?m)^[ \t]*([a-z])\.[ \t]+")
PDF_FOOTER = re.compile(r"(?mi)^\s*Docusign Envelope ID:[^\r\n]*")
PAYMENT = re.compile(r"\bwithin\s+(\d+)\s+days?\s+of\s+receipt\s+of\s+a\s+valid\s+invoice\b", re.I)


def _clean(text):
    return " ".join(text.split())


def _evidence(page, clause, text):
    return {"page": page, "clause": clause, "text": _clean(text)}


def read_pages(pdf, ocr):
    """Use the text layer first and OCR only pages whose text is unusable."""
    try:
        reader = PdfReader(BytesIO(pdf))
        if reader.is_encrypted or not 0 < len(reader.pages) <= MAX_PAGES:
            return [], "unreadable"
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:  # A malformed page must not discard the readable pages.
                pages.append("")
    except Exception:  # PDF parser errors must preserve the uploaded draft.
        return [], "unreadable"
    missing = [i for i, text in enumerate(pages)
               if sum(c.isascii() and c.isalpha() for c in text) < 40 or "\ufffd" in text or "(cid:" in text]
    if missing:
        try:
            recovered = ocr(pdf, missing)
        except Exception:
            recovered = {}
        for i in missing:
            pages[i] = recovered.get(i, "")
    return pages, "partial" if any(not pages[i].strip() for i in missing) else "read"


def _clauses(text, pattern):
    matches = list(pattern.finditer(text))
    return [(m.group(1), text[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(text)].strip())
            for i, m in enumerate(matches)]


def _empty_fields(fields):
    return {name: {"value": "", "sources": [], "derived": False} for name in fields}


def _record(findings, key, value, evidence, *, derived=False):
    row = findings[key]
    try:
        value = normalize_value(key, str(value))
    except ValueError:
        value = ""
    if row["sources"] and row["value"] != value:
        row.update(value="", conflict=True)
    elif not row.get("conflict"):
        row["value"] = value
    row["derived"] = derived
    if evidence not in row["sources"]:
        row["sources"].append(evidence)


def extract_terms(pages):
    findings = _empty_fields(FIELDS)

    def add(*args, **kwargs):
        _record(findings, *args, **kwargs)

    section_number = ""
    pages = [PDF_FOOTER.sub("", text) for text in pages]
    for i, text in enumerate(pages, 1):
        flat = _clean(text)
        markers = sorted([(m.start(), "section", m[1]) for m in NUMBERED_HEADING.finditer(text)]
                         + [(m.start(), "letter", m[1]) for m in LETTERED_PARAGRAPH.finditer(text)])
        for index, (offset, kind, label) in enumerate(markers):
            if kind == "section":
                section_number = label
                continue
            stop = markers[index + 1][0] if index + 1 < len(markers) else len(text)
            paragraph = _clean(text[offset:stop])
            clause = f"§{section_number}({label})" if section_number else f"({label})"
            for match in TERM.finditer(paragraph):
                add("term_years", match[1], _evidence(i, clause, match[0]))
            for match in REVIEW.finditer(paragraph):
                add("review_months", match[1], _evidence(i, clause, match[0]))
        if re.search(r"\bDate\s*:", flat, re.I) and re.search(r"\bSigned\s+for\b", flat, re.I):
            for match in DATE.finditer(flat):
                try:
                    start = date(int(match[3]), int(match[2]), int(match[1]))
                except ValueError:
                    continue
                add("start", start.isoformat(), _evidence(i, "签署页 · Date（无条款编号）", match[0]))

    for i, text in enumerate(pages, 1):
        heading = FEE_HEADING.search(text)
        if not heading:
            continue
        section = re.split(r"(?m)^\s*\d+\s*\.\s+[A-Z]", text[heading.end():], maxsplit=1)[0]
        number = heading[1]
        for part, body in _clauses(section, CLAUSE):
            clause = f"§{number}({part})"
            evidence = _evidence(i, clause, body)
            flat = _clean(body)
            if part == "a":
                tiers = _clauses(body, SUBCLAUSE)
                intro = body.splitlines()[0] if not tiers else body[:SUBCLAUSE.search(body).start()]
                if re.search(r"one[ -]off\s+payment", intro, re.I) and re.search(r"tuition\s+fees\s+only", intro, re.I):
                    add("frequency", "一次性，仅按学费", _evidence(i, clause, intro))
                for sub, tier in tiers:
                    wording = re.sub(r"\s+OR\s*$", "", _clean(tier), flags=re.I)
                    for match in RATE.finditer(wording):
                        side = match[2].lower()
                        proof = _evidence(i, f"{clause}({sub})", tier)
                        add(f"{side}_rate", match[1], proof)
                        add(f"{side}_scope", wording, proof)
            elif part in {"b", "c", "d", "e"}:
                key = {"b": "conditions", "c": "agency_application", "d": "direct_exclusion",
                       "e": "invoice_condition"}[part]
                if flat:
                    add(key, flat, evidence)
            elif part == "f":
                for match in PAYMENT.finditer(flat):
                    add("payment_days", match[1], evidence)
                    add("payment_trigger", "收到有效发票", evidence)
    start, term = findings["start"], findings["term_years"]
    if start["value"] and term["value"]:
        try:
            start_date = date.fromisoformat(start["value"])
            end = start_date.replace(year=start_date.year + int(term["value"])) - timedelta(days=1)
        except (ValueError, OverflowError):
            pass
        else:
            for proof in (*term["sources"], *start["sources"]):
                add("end", end.isoformat(), proof, derived=True)
    return findings


CONFIG_CLAUSE = re.compile(r"(?m)^[ \t]*(\d+(?:\.\d+)*)\.?[ \t]+")
CONFIG_DATES = re.compile(r"This agreement is effective from (\d{4}-\d{2}-\d{2}) (?:through|to) (\d{4}-\d{2}-\d{2})\.", re.I)
CONFIG_RATE = re.compile(r"Commission is a one[ -]off payment of (\d+(?:\.\d+)?)% of (?:the )?first[ -]year(?:[’']s)? tuition fees\.", re.I)
CONFIG_CONDITIONS = re.compile(
    r"A claim requires the student to have commenced at least (\d+) days? ago and a school registration certificate\.", re.I
)
CONFIG_PAYMENT = re.compile(r"Payment is due within (\d+) days? of receipt of a valid invoice\.", re.I)


def extract_configurable_terms(pages):
    findings = _empty_fields(CONFIGURABLE_FIELDS)
    for i, page in enumerate(pages, 1):
        for clause, body in _clauses(PDF_FOOTER.sub("", page), CONFIG_CLAUSE):
            text = _clean(body)
            proof = _evidence(i, f"§{clause}", body)
            fields = {}
            if dates := CONFIG_DATES.fullmatch(text):
                fields = {"start": dates[1], "end": dates[2]}
            elif rate := CONFIG_RATE.fullmatch(text):
                fields = {"rate": rate[1], "frequency": "一次性，仅按学费"}
            elif terms := CONFIG_CONDITIONS.fullmatch(text):
                fields = {"conditions": text, "commencement_days": terms[1], "registration_required": "1"}
            elif re.search(r"\bclaim\b", text, re.I):
                fields = {"conditions": text}
            elif payment := CONFIG_PAYMENT.fullmatch(text):
                fields = {"payment_days": payment[1], "payment_trigger": "收到有效发票"}
            elif re.fullmatch(r"No GST applies to the commission\.", text, re.I):
                fields = {"gst": "none"}
            for key, value in fields.items():
                _record(findings, key, value, proof)
    return findings


EMAIL = re.compile(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+", re.I)
INVOICE_ADDRESS = re.compile(
    r"\binvoic(?:es?|ing)\s+(?:(?:should|must|can|are to)\s+be\s+)?"
    r"(?:sent|submitted|emailed|addressed)\s+to\b|\binvoice\s+(?:recipient\s+)?e-?mail\s*:",
    re.I,
)
SCHOOL_PARTY = re.compile(r"(?im)^[ \t]*(?:\d+(?:\.\d+)*\.?[ \t]+)?(?:school(?: legal)? name|school|college legal name)[ \t]*:[ \t]*([^\r\n]{1,200})")
PERIOD_PATTERNS = {
    "first_year": re.compile(r"\bfirst[ -]year(?:[’']s)?\s+(?:(?:local|international)\s+)?tuition", re.I),
    "first_half_year": re.compile(r"\bfirst (?:six|6) months\b|\bfirst half[ -]year\b", re.I),
    "each_year": re.compile(r"\beach (?:academic |school )?year\b|\bevery (?:academic |school )?year\b|\b(?:paid|payable|calculated) annually\b", re.I),
}


def add_onboarding_findings(findings, pages, school_name):
    findings.update(_empty_fields(ONBOARDING_FIELDS))
    for number, page in enumerate(pages, 1):
        for match in SCHOOL_PARTY.finditer(page):
            _record(findings, "school_name", match[1], _evidence(number, "学校名称", match[0]))
        sections = _clauses(PDF_FOOTER.sub("", page), CONFIG_CLAUSE)
        sections += [("原文", line) for line in page.splitlines() if INVOICE_ADDRESS.search(line)]
        for clause, body in sections:
            text = _clean(body)
            proof = _evidence(number, clause if clause == "原文" else f"§{clause}", body)
            if INVOICE_ADDRESS.search(text):
                for address in EMAIL.findall(text):
                    _record(findings, "invoice_email", address, proof)
            if not re.search(r"\b(?:commission|agent[’']s fees?)\b", text, re.I) or "tuition" not in text.lower():
                continue
            periods = [name for name, pattern in PERIOD_PATTERNS.items() if pattern.search(text)]
            for period in periods:
                _record(findings, "tuition_period", period, proof)
            if periods and "rate" in findings:
                rates = set(re.findall(r"(\d+(?:\.\d+)?)\s*%", text))
                if len(rates) == 1:
                    _record(findings, "rate", rates.pop(), proof)
                    if periods == ["first_year"] and re.search(r"\bone[ -]off\b|\bonce\b", text, re.I):
                        _record(findings, "frequency", "一次性，仅按学费", proof)
    if not findings["school_name"]["sources"]:
        findings["school_name"]["value"] = school_name
    if not findings["tuition_period"]["sources"]:
        rates = [findings.get(key) or {} for key in ("local_rate", "international_rate", "rate")]
        for rate in rates:
            for proof in rate.get("sources", []):
                if PERIOD_PATTERNS["first_year"].search(proof["text"]):
                    _record(findings, "tuition_period", "first_year", proof)
    return findings


def normalize_value(key, value):
    value = value.strip()
    if not value or len(value) > 3000:
        raise ValueError("请填写有效的确认值。")
    if key == "school_name" and len(value) > 200:
        raise ValueError("合同上的学校名称不能超过 200 个字符。")
    if key == "invoice_email":
        if len(value) > 254 or not EMAIL.fullmatch(value):
            raise ValueError("请输入一个有效的学校收票邮箱。")
    if key == "tuition_period" and value not in TUITION_PERIODS:
        raise ValueError("请选择首年、首半年或每年学费。")
    if key in RATES:
        try:
            number = Decimal(value)
            if not number.is_finite() or not 0 < number <= 100 or number * 100 != (number * 100).to_integral_value():
                raise InvalidOperation
            return format(number.normalize(), "f")
        except InvalidOperation:
            raise ValueError("佣金比例须在 0 至 100 之间，最多两位小数。") from None
    if key in {"start", "end"}:
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            raise ValueError("请按 YYYY-MM-DD 填写日期。") from None
    if key in {"term_years", "review_months", "payment_days", "commencement_days"}:
        if not re.fullmatch(r"\d{1,3}", value) or int(value) > 365:
            raise ValueError("期限须为 0 至 365 的整数。")
        return str(int(value))
    if key == "registration_required" and value not in {"0", "1"}:
        raise ValueError("注册凭证请填写 1（需要）或 0（无需）。")
    if key == "gst" and value not in {"none", "inclusive", "exclusive"}:
        raise ValueError("GST 请选择不适用、已含或另加。")
    return value
