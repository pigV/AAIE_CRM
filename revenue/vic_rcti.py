"""The additive commission and quarterly RCTI in the Victorian DE schedule."""

from datetime import datetime
import re



PROFILE = "vic_rcti"
PAYMENT = "rcti_quarterly"
LABELS = {"standard": "基础佣金", "english_bonus": "强化英语课程额外奖励"}
CONDITIONS = ("500 签证；本公司招募并收到教育局首次录取函，且当时合同有效；无教育局欠费；"
              "实际就读至少四周。课程不足 12 个月时，基础佣金按比例待复核，英语奖励不适用。"
              "首年提前退学时，教育局可能在后续款项扣回。")


def _source(page, clause, text):
    return {"page": page, "clause": clause, "text": text.strip()}


def _field(value, *sources):
    return {"value": value, "sources": list(sources)}


def extract(pages):
    """Return findings only for this schedule shape, with clause evidence."""
    normalized = [" ".join(text.split()) for text in pages]
    standard = re.compile(r"standard commission rate of\s+(\d+(?:\.\d+)?)\s*%[^.]{0,220}?first year of tuition", re.I)
    bonus = re.compile(r"additional commission of\s+(\d+(?:\.\d+)?)\s*%[^.]{0,180}?intensive English Language Program", re.I)
    quarterly = re.compile(r"automated disbursement process on a quarterly basis[^.]{0,190}?(?:four weeks or more|4 weeks or more)", re.I)
    receipt = re.compile(r"receive a RCTI with the Commission payment[^.]{0,240}", re.I)
    for page, text in enumerate(normalized, 1):
        if not re.search(r"Attachment\s+2\s+Commission Schedule", text, re.I):
            continue
        base, extra, payment = standard.search(text), bonus.search(text), quarterly.search(text)
        if not all((base, extra, payment)):
            continue
        rcti = next(((p, match) for p, value in enumerate(normalized, 1) if p >= page
                     and (match := receipt.search(value))), None)
        if rcti is None:
            continue
        conditions = re.search(r"2\s+Terms and Conditions of Standard Commission Payment and English Bonus.*?(?=3\s+Payment terms|$)", text, re.I)
        base_src = _source(page, "附件 2 §1(a)", base[0])
        extra_end = min(len(text), extra.end() + 270)
        extra_text = re.split(r"\s+2\s+Terms and Conditions", text[extra.start():extra_end], maxsplit=1)[0]
        fields = {
            "standard": _field(base[1], base_src),
            "english_bonus": _field(extra[1], _source(page, "附件 2 §1(b)", extra_text)),
            "tuition_period": _field("first_year", base_src),
            "conditions": _field(CONDITIONS, _source(page, "附件 2 §2", conditions[0]) if conditions else base_src),
            "payment_mode": _field("按季度自动付款 · 教育局出具 RCTI",
                                   _source(page, "附件 2 §3.1", payment[0]),
                                   _source(rcti[0], "附件 2 §3.2(a)", rcti[1][0])),
        }
        for p, value in enumerate(normalized, 1):
            for field, label in (("start", "Date of Agreement"), ("end", "Comp(?:le|e)tion Date")):
                if field not in fields and (match := re.search(
                    label + r"\s*[:–-]?\s*(\d{1,2}\s+[A-Za-z]+\s+\d{4})", value, re.I
                )):
                    try:
                        day = datetime.strptime(match[1], "%d %B %Y").date().isoformat()
                    except ValueError:
                        continue
                    fields[field] = _field(day, _source(p, "合同明细", match[0]))
            if "gst" not in fields and (match := re.search(r"23\.\s*GST.{0,740}?exclusive of GST", value, re.I)):
                fields["gst"] = _field("exclusive", _source(p, "§23(b)", match[0]))
        return fields
    return None
