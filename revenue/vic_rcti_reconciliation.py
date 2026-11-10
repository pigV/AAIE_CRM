"""VIC recipient-created tax invoice reconciliation, separate from COM invoices."""
import csv
from datetime import date, datetime
from io import StringIO
import json
import re
import sqlite3
import unicodedata
from zoneinfo import ZoneInfo

from revenue.money import aud_cents
from revenue.institution_commission_draft import _digest
from revenue.repository import canonical_json, new_uid
from revenue.services import RevenueService
from revenue.vic_rcti import PAYMENT

URL = "/revenue/vic-rcti"
RECEIPT_KIND = "vic_rcti_bank_receipt"
RECEIPT_ACCOUNT = "~VIC-RCTI-RECEIPT~"
DOCUMENTS = {"vic_rcti_original", "vic_rcti_students", RECEIPT_KIND}
HEADER = ("issuer", "abn", "number", "date", "net", "gst", "gross")
CSV_COLUMNS = ("student_id", "component_uid", "period", "net", "gst", "gross")
MONEY = ("net_cents", "gst_cents", "gross_cents")
VIC_ISSUER_ABN = "52705101522"
STUDENT_INVOICE_NUMBER = re.compile(r"(20[0-9]{2})T([1-4])_([0-9]{1,30})")
PDF_AMOUNT = r"([0-9][0-9,]*\.[0-9]{2})[ \t]*$"


def ready(db):
    return db.execute("""SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN
      ('revenue_vic_rcti','revenue_vic_rcti_student','revenue_vic_rcti_bank')""").fetchone()[0] == 3


def norm(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).upper().split())


def _abn(value):
    return re.sub(r"[ -]", "", str(value or ""))


def _identity(header):
    abn, number = _abn(header.get("abn")), norm(header.get("number"))
    return (abn, number) if re.fullmatch(r"[0-9]{11}", abn) and number else (None, None)


def _amounts(fields):
    amounts = {key + "_cents": aud_cents(fields.get(key)) for key in ("net", "gst", "gross")}
    if amounts["gross_cents"] <= 0 or amounts["net_cents"] + amounts["gst_cents"] != amounts["gross_cents"]:
        raise ValueError("未税金额加 GST 必须等于含税总额，且总额须大于零。")
    return amounts


def _date(raw):
    try:
        return date.fromisoformat(raw).isoformat()
    except (ValueError, TypeError):
        raise ValueError("请填写有效日期。") from None


def _today():
    return datetime.now(ZoneInfo("Australia/Melbourne")).date().isoformat()


def _official_vic_fields(text, issuer_name):
    heading, _, body = re.split(r"(?im)(^[ \t]*Supply From[ \t]*:)", text, maxsplit=1)
    title = re.search(r"(?im)^[ \t]*Recipient Created Tax Invoice[ \t]*$", heading)
    abns = re.findall(r"(?im)^[ \t]*ABN[ \t:]+([0-9][0-9 \t-]*)$", heading[title.end():]) if title else []
    abn = _abn(abns[0].strip()) if len(abns) == 1 else ""
    fields = dict.fromkeys(HEADER, "")
    if abn != VIC_ISSUER_ABN:
        return fields

    fields.update(abn=abn, issuer=issuer_name)
    labels = {
        "number": r"\bInvoice Number[ \t]*:[ \t]*([A-Za-z0-9_-]+)[ \t]*$",
        "date": r"\bInvoice Date[ \t]*:[ \t]*([0-9]{2}-[0-9]{2}-[0-9]{4})[ \t]*$",
        "net": r"^[ \t]*Total not inc\. GST[ \t]+" + PDF_AMOUNT,
        "gst": r"\bPlus GST[ \t]+" + PDF_AMOUNT,
        "gross": r"^[ \t]*Total inc\. GST[ \t]*\(AUD\)[ \t]+" + PDF_AMOUNT,
    }
    for key, pattern in labels.items():
        hits = set(re.findall(pattern, body, re.I | re.M))
        if len(hits) == 1:
            fields[key] = hits.pop().replace(",", "")
    if fields["date"]:
        try:
            fields["date"] = datetime.strptime(fields["date"], "%d-%m-%Y").date().isoformat()
        except ValueError:
            fields["date"] = ""
    return fields


def read_pdf(data, *, issuer_name="Department of Education (Victoria)"):
    from io import BytesIO
    from pypdf import PdfReader
    try:
        reader = PdfReader(BytesIO(data), strict=False)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 30:
            raise ValueError()
        text = "\n".join((page.extract_text() or "")[:24000] for page in reader.pages)
    except Exception:
        raise ValueError("无法读取 RCTI PDF，请核对文件是否损坏、加密或超过 30 页。") from None
    if re.search(r"(?im)^[ \t]*Supply From[ \t]*:", text):
        return _official_vic_fields(text, issuer_name)

    labels = {
        "issuer": r"(?:Issuer|Issued by)", "abn": r"(?:Issuer ABN|ABN)",
        "number": r"(?:RCTI (?:Number|No\.?|#))", "date": r"(?:Issue Date|RCTI Date)",
        "net": r"(?:Net(?: Amount)?|Subtotal)",
        "gst": r"(?:GST(?: Amount)?)", "gross": r"(?:Gross(?: Amount)?|Total(?: Amount)?)",
    }
    fields = {}
    for key, pattern in labels.items():
        hits = re.findall(r"(?im)^\s*" + pattern + r"[ \t]*:[ \t]*([^\r\n]+)", text)
        fields[key] = hits[0].strip()[:200] if len(set(hits)) == 1 else ""
        if key in {"net", "gst", "gross"}:
            fields[key] = re.sub(r"(?i)^AUD\s*|[$,]", "", fields[key]).strip()
    return fields


def read_csv(data):
    return read_statement_csv(data)[1]


def read_statement_csv(data):
    if len(data) > 1024 * 1024:
        raise ValueError("学生明细 CSV 不能超过 1 MB。")
    try:
        source = csv.DictReader(StringIO(data.decode("utf-8-sig")), strict=True)
        has_number = source.fieldnames == ["rcti_number", *CSV_COLUMNS]
        if not has_number and source.fieldnames != list(CSV_COLUMNS):
            raise ValueError("请按 CSV 模板填写表头。")
        rows = []
        seen = set()
        numbers = set()
        for index, raw in enumerate(source, 2):
            if index > 501 or None in raw or None in raw.values():
                raise ValueError("CSV 每行须完整，最多 500 行。")
            if has_number:
                numbers.add(norm(raw["rcti_number"]))
            student = raw["student_id"].strip()
            component = raw["component_uid"].strip()
            key = (student, component, raw["period"].strip().lower())
            if len(student) > 100 or len(component) > 100 or not student or key[2] != "first_year":
                raise ValueError(f"CSV 第 {index} 行须有学生标识和 first_year 期间。")
            if key in seen:
                raise ValueError(f"CSV 第 {index} 行学生明细重复。")
            seen.add(key)
            rows.append(dict(student_id=student, component_uid=component, period=key[2], **_amounts(raw)))
    except (UnicodeDecodeError, csv.Error):
        raise ValueError("请上传有效的 UTF-8 CSV。") from None
    if not rows:
        raise ValueError("CSV 没有学生明细。")
    if has_number and (len(numbers) != 1 or not all(1 <= len(value) <= 100 for value in numbers)):
        raise ValueError("CSV 每一行须填写相同的完整 RCTI 单号。")
    return next(iter(numbers), ""), rows


class VicReconciliation:
    def __init__(self, workflow):
        self.w = workflow
        self.repo = workflow.repo
        self.db = self.repo.connection
        self.terms = workflow.terms()
        if (workflow.target != ("authority", "VIC") or not self.terms
                or self.terms.get("payment_mode") != PAYMENT):
            raise PermissionError("请先确认适用季度 RCTI 的维州合同。")
        if not ready(self.db):
            raise PermissionError("维州 RCTI 对账尚未开放。")
        self.sim = int(workflow.pilot.simulation)
        self.actor = workflow.actor
        self.service = RevenueService(self.repo)

    def list(self):
        return [self.detail(row[0]) for row in self.db.execute(
            """SELECT uid FROM revenue_vic_rcti WHERE contract_uid=? AND simulation=?
               ORDER BY COALESCE(json_extract(review_json,'$.difference_pending'),0) DESC,created_at DESC,uid LIMIT 100""", (self.w.uid, self.sim))]

    def detail(self, uid):
        row = self.db.execute("SELECT * FROM revenue_vic_rcti WHERE uid=? AND contract_uid=? AND simulation=?",
                              (uid, self.w.uid, self.sim)).fetchone()
        if not row:
            raise LookupError("找不到本合同的 RCTI。")
        result = dict(row)
        result["review"] = json.loads(row["review_json"])
        result["confirmed"] = json.loads(row["confirmed_json"]) if row["confirmed_json"] else None
        result["bank"] = [dict(v) for v in self.db.execute(
            """SELECT b.*, d.id AS receipt_id FROM revenue_vic_rcti_bank b
                 LEFT JOIN revenue_documents d ON b.account=? AND d.document_kind=?
                       AND b.transaction_ref=CAST(d.id AS TEXT)
                WHERE b.rcti_uid=? ORDER BY b.confirmed_at,b.uid""",
            (RECEIPT_ACCOUNT, RECEIPT_KIND, uid))]
        result["received_cents"] = sum(item["cents"] for item in result["bank"])
        result["balance"] = (max(0, result["confirmed"]["gross_cents"] - result["received_cents"])
                             if result["confirmed"] else None)
        return result

    def _document(self, item, kind, uid):
        return self.service.register_document(kind, str(item.path), item.sha256, item.mime_type,
                                              item.size_bytes, "upload", self.actor, f"{kind}:{uid}")["id"]

    def _same_number(self, issuer, number, uid=None):
        if issuer and self.db.execute(
            """SELECT 1 FROM revenue_vic_rcti r WHERE simulation=? AND uid!=?
               AND ((issuer_key=? AND number_key=?) OR EXISTS(
                   SELECT 1 FROM json_each(r.review_json,'$.previous_versions') v
                    WHERE json_extract(v.value,'$.issuer_key')=? AND json_extract(v.value,'$.number_key')=?
               ))""", (self.sim, uid or "", issuer, number, issuer, number)).fetchone():
            raise ValueError("已有相同出具方和 RCTI 单号，请核对是否为更正版本。")

    def _find_pdf(self, digest):
        return self.db.execute(
            """SELECT r.uid,r.contract_uid FROM revenue_vic_rcti r WHERE simulation=?
               AND (pdf_sha=? OR EXISTS(
                   SELECT 1 FROM json_each(r.review_json,'$.previous_versions') v
                    WHERE json_extract(v.value,'$.pdf_sha')=?
               ))""", (self.sim, digest, digest)).fetchone()

    def upload(self, pdf, header, csv_file=None, rows=()):
        with self.repo.transaction():
            prior = self._find_pdf(pdf.sha256)
            if prior:
                if prior["contract_uid"] != self.w.uid:
                    raise ValueError("此 RCTI 原件已上传到另一版维州合同，请从原记录打开。")
                return prior["uid"]
            issuer, number = _identity(header)
            self._same_number(issuer, number)
            uid = new_uid("vic-rcti")
            pdf_id = self._document(pdf, "vic_rcti_original", uid)
            csv_id = self._document(csv_file, "vic_rcti_students", uid) if csv_file else None
            review = {"header": header, "rows": list(rows), "choices": {}}
            self.db.execute("""INSERT INTO revenue_vic_rcti(uid,contract_uid,simulation,pdf_id,pdf_sha,csv_id,
                issuer_key,number_key,review_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (uid, self.w.uid, self.sim, pdf_id, pdf.sha256, csv_id, issuer, number,
                 canonical_json(review), self.repo.now()))
            self._remember_difference(uid)
            return uid

    def replace_pdf(self, uid, pdf, header):
        with self.repo.transaction():
            current = self.detail(uid)
            if pdf.sha256 == current["pdf_sha"]:
                return
            if current["confirmed"]:
                raise ValueError("该 RCTI 已进入账本，请先由财务处理更正。")
            if self._find_pdf(pdf.sha256):
                raise ValueError("这份 PDF 已存在于 RCTI 记录或历史版本中。")
            issuer, number = _identity(header)
            _amounts(header)
            _date(header.get("date"))
            previous_student = STUDENT_INVOICE_NUMBER.fullmatch(current["number_key"] or "")
            replacement_student = STUDENT_INVOICE_NUMBER.fullmatch(number or "")
            same_student = (previous_student and replacement_student
                            and previous_student.group(3) == replacement_student.group(3))
            if (not issuer or issuer != current["issuer_key"]
                    or (number != current["number_key"] and not same_student)):
                raise ValueError("重发的 RCTI 必须来自同一出具方，且单号或 Offer 学生号须一致。")
            self._same_number(issuer, number, uid)
            review = current["review"]
            previous = {key: current[key] for key in ("pdf_id", "pdf_sha", "csv_id", "issuer_key", "number_key")}
            previous.update(header=review["header"], replaced_at=self.repo.now(), actor=self.actor)
            review.setdefault("previous_versions", []).append(previous)
            review.update(header=header, rows=[], choices={}, difference_pending=True)
            pdf_id = self._document(pdf, "vic_rcti_original", f"{uid}:{pdf.sha256}")
            self.db.execute("""UPDATE revenue_vic_rcti SET pdf_id=?,pdf_sha=?,csv_id=NULL,
                             issuer_key=?,number_key=?,review_json=? WHERE uid=?""",
                            (pdf_id, pdf.sha256, issuer, number, canonical_json(review), uid))

    def statement_for_csv(self, filename, number, fallback_uid=None):
        filename_number = norm(filename).removeprefix("DET_RCTI_INVOICE_")
        if number and STUDENT_INVOICE_NUMBER.fullmatch(filename_number) and number != filename_number:
            raise ValueError("CSV 内容和文件名的 RCTI 单号不一致。")
        matches = self.db.execute(
            """SELECT uid FROM revenue_vic_rcti
                WHERE contract_uid=? AND simulation=? AND number_key=? LIMIT 2""",
            (self.w.uid, self.sim, number or filename_number),
        ).fetchall()
        if len(matches) == 1:
            return matches[0][0]
        if not matches and fallback_uid and not (number or STUDENT_INVOICE_NUMBER.fullmatch(filename_number)):
            return fallback_uid
        raise ValueError("请用完整 RCTI 单号命名 CSV，或在明细中填写 rcti_number。系统需要找到唯一对应的 PDF。")

    def add_csv(self, uid, file, rows):
        with self.repo.transaction():
            current = self.detail(uid)
            if current["csv_id"]:
                prior_sha = self.db.execute("SELECT sha256 FROM revenue_documents WHERE id=?",
                                            (current["csv_id"],)).fetchone()[0]
                if prior_sha == file.sha256:
                    return
                raise ValueError("此 RCTI 已有学生明细，请核对已有记录。")
            review = {**current["review"], "rows": rows}
            csv_id = next((version["csv_id"] for version in review.get("previous_versions", ())
                           if version["csv_id"] and self.db.execute(
                               "SELECT sha256 FROM revenue_documents WHERE id=?", (version["csv_id"],)
                           ).fetchone()[0] == file.sha256), None)
            if csv_id is None:
                csv_id = self._document(file, "vic_rcti_students", f"{uid}:{file.sha256}")
            self.db.execute("UPDATE revenue_vic_rcti SET csv_id=?,review_json=? WHERE uid=?",
                            (csv_id, canonical_json(review), uid))
            self._remember_difference(uid)

    def match_students(self, uid, choices):
        with self.repo.transaction():
            current = self.detail(uid)
            if current["details_confirmed"]:
                raise ValueError("已确认的学生明细不可覆盖。")
            review = current["review"]
            review["choices"] = {str(index): choices[str(index)] for index in range(len(review["rows"]))
                                 if choices.get(str(index))}
            self.db.execute("UPDATE revenue_vic_rcti SET review_json=? WHERE uid=?",
                            (canonical_json(review), uid))
            self._remember_difference(uid)

    def _student_from_number(self, number, candidates, reserved):
        match = STUDENT_INVOICE_NUMBER.fullmatch(norm(number))
        if not match:
            return None
        year, term, student_number = match.groups()
        rows = self.db.execute(
            """SELECT DISTINCT o.student_id FROM audit_logs a
                 JOIN offers o ON o.id=CAST(a.record_id AS INTEGER)
                              AND CAST(o.id AS TEXT)=a.record_id AND o.student_id=a.student_id
                WHERE a.action_type='extraction_review_confirmed' AND a.module_name='offer'
                  AND CASE WHEN json_valid(a.new_value)
                           THEN json_extract(a.new_value, '$.machine_inferred.school_student_number')
                      END = ? COLLATE BINARY
                LIMIT 2""",
            (student_number,),
        ).fetchall()
        result = {"term": f"{year} Term {term}", "student_number": student_number}
        if len(rows) != 1:
            return {**result, "status": "ambiguous" if rows else "unmatched"}
        student_id = rows[0][0]
        related = [row for row in candidates if row["student_id"] == student_id]
        estimates = [row for row in related
                     if row.get("eligible_on") and row["component_uid"] not in reserved]
        estimate = estimates[0] if len(estimates) == 1 else None
        return {**result, "status": "matched", "student_id": student_id,
                "student_name": related[0].get("student_name", "") if related else "",
                "expected_cents": estimate["gross_cents"] if estimate else None,
                "expected": estimate}

    def compare(self, uid, *, current=False, candidates=None):
        row = self.detail(uid)
        if not current and row["confirmed"] and (row["details_confirmed"] or not row["csv_id"]):
            return row["confirmed"]["comparison"]
        header, data, choices = (row["review"][k] for k in ("header", "rows", "choices"))
        reserved = {v[0] for v in self.db.execute(
            "SELECT component_uid FROM revenue_vic_rcti_student WHERE simulation=? AND rcti_uid!=?",
            (self.sim, uid))}
        if candidates is None:
            candidates = list(self.w.candidates(self.terms))
        number_student = self._student_from_number(header.get("number"), candidates, reserved)
        baseline = number_student.get("expected") if number_student else None
        correction = row["review"].get("expected_correction")
        if correction and baseline and correction["baseline"] == self._expected_basis(baseline):
            corrected = {**baseline, "gross_cents": correction["after_cents"]}
            number_student.update(expected=corrected, expected_cents=corrected["gross_cents"])
        else:
            correction = None
        estimates = {v["component_uid"]: v for v in candidates
                     if v.get("eligible_on")
                     and v["component_uid"] not in reserved}
        lines, selected = [], set()
        for index, actual in enumerate(data):
            if number_student:
                match = (number_student.get("expected")
                         if actual["student_id"] == number_student["student_number"] else None)
            else:
                requested = choices.get(str(index)) or actual["component_uid"]
                options = [v for v in estimates.values() if norm(v["student_id"]) == norm(actual["student_id"])]
                if requested:
                    match = estimates.get(requested)
                    if not choices.get(str(index)) and match not in options:
                        match = None
                else:
                    match = options[0] if len(options) == 1 else None
            if not match or match["component_uid"] in selected:
                lines.append({"actual": actual, "expected": None, "status": "unmatched"})
                continue
            selected.add(match["component_uid"])
            difference = actual["gross_cents"] - match["gross_cents"]
            compared_fields = ("gross_cents",) if correction else MONEY
            lines.append({"actual": actual, "expected": match, "difference": difference,
                          "status": "match" if all(actual[k] == match[k] for k in compared_fields) else "different"})
        totals = {key: sum(v[key] for v in data) for key in MONEY}
        expected_total = sum(line["expected"]["gross_cents"] for line in lines if line["expected"]) if selected else None
        if number_student:
            expected_total = number_student.get("expected_cents")
        model = dict(lines=lines, estimates=list(estimates.values()), totals=totals,
                     expected_total=expected_total, total_only=not row["csv_id"], number_student=number_student,
                     expected_basis=self._expected_basis(baseline) if baseline else None,
                     expected_correction=correction)
        model["digest"] = _digest([uid, self.terms["revision"], header, model])
        return model

    @staticmethod
    def _expected_basis(estimate):
        return {key: estimate[key] for key in ("student_id", "component_uid", *MONEY)}

    def difference(self, record, model):
        header = record["review"]["header"]
        matched_student = model.get("number_student") or {}
        lines = [(line["actual"], line["expected"] and
                  {key: line["expected"][key] for key in ("student_id", "component_uid", *MONEY)})
                 for line in model["lines"]]
        expected = model["expected_total"]
        current_difference = any(line["status"] == "different" for line in model["lines"])
        if expected is not None:
            try:
                official = _amounts(header)
            except ValueError:
                official = {}
            current_difference |= bool(official and official["gross_cents"] != expected)
            estimate = matched_student.get("expected")
            if official and estimate and not model["expected_correction"]:
                current_difference |= any(official[key] != estimate[key] for key in MONEY)
        signature = _digest([record["pdf_sha"], record["csv_id"], expected, matched_student.get("student_id"),
                             [matched_student["expected"][key] for key in MONEY] if matched_student.get("expected") else [],
                             lines])
        review = record["review"]
        history = review.get("difference_resolutions", [])
        last = history[-1] if history else None
        pending = bool(review.get("difference_pending") or current_difference or (last and last["signature"] != signature))
        return dict(pending=pending and not (last and last["signature"] == signature
                                             and not review.get("difference_pending")),
                    signature=signature, mismatch=current_difference, last=last)

    def _remember_difference(self, uid):
        record = self.detail(uid)
        state = self.difference(record, self.compare(uid, current=True))
        if state["pending"] and not record["review"].get("difference_pending"):
            review = {**record["review"], "difference_pending": True}
            self.db.execute("UPDATE revenue_vic_rcti SET review_json=? WHERE uid=?",
                            (canonical_json(review), uid))

    def list_differences(self):
        records = self.list()
        if not records:
            return
        candidates = list(self.w.candidates(self.terms))
        for record in records:
            model = self.compare(record["uid"], current=True, candidates=candidates)
            yield record, model, self.difference(record, model)

    def remember_differences(self):
        for record, _, state in self.list_differences():
            if state["pending"] and not record["review"].get("difference_pending"):
                review = {**record["review"], "difference_pending": True}
                self.db.execute("UPDATE revenue_vic_rcti SET review_json=? WHERE uid=?",
                                (canonical_json(review), record["uid"]))

    def resolve_difference(self, uid, signature, outcome, note, corrected_amount=None):
        if outcome not in {"crm", "crm_amount", "reissued", "explained"} or not note.strip() or len(note) > 1000:
            raise ValueError("请选择处理方式并填写核对结果。")
        with self.repo.transaction():
            record = self.detail(uid)
            model = self.compare(uid, current=True)
            state = self.difference(record, model)
            if signature != state["signature"]:
                raise ValueError("比对数据已变更，请刷新页面后确认。")
            if not state["pending"]:
                return
            review = record["review"]
            if outcome == "reissued" and not review.get("previous_versions"):
                raise ValueError("请先上传教育局重发的 RCTI。")
            resolution = {"outcome": outcome, "note": note.strip(), "actor": self.actor, "at": self.repo.now()}
            if outcome == "crm_amount":
                basis = model["expected_basis"]
                if not basis:
                    raise ValueError("请先确认唯一匹配的学生和 CRM 预计佣金。")
                amount = aud_cents(corrected_amount)
                correction = {"baseline": basis, "before_cents": model["expected_total"], "after_cents": amount}
                review["expected_correction"] = correction
                resolution["correction"] = correction
                self.db.execute("UPDATE revenue_vic_rcti SET review_json=? WHERE uid=?",
                                (canonical_json(review), uid))
                state = self.difference(self.detail(uid), self.compare(uid, current=True))
            resolution["signature"] = state["signature"]
            review.setdefault("difference_resolutions", []).append(resolution)
            review["difference_pending"] = False
            self.db.execute("UPDATE revenue_vic_rcti SET review_json=? WHERE uid=?",
                            (canonical_json(review), uid))

    def confirm(self, uid, digest, reason, reviewed, total_only):
        if not reviewed or len(reason) > 1000:
            raise ValueError("请核对原件并确认。")
        with self.repo.transaction():
            current = self.detail(uid)
            prior = current["confirmed"]
            if prior and (current["details_confirmed"] or not current["csv_id"]):
                return prior
            header = current["review"]["header"]
            amounts = _amounts(header)
            issuer, number = _identity(header)
            if (not issuer or len(number) > 100 or not norm(header.get("issuer"))
                    or norm(header["issuer"]) != norm(self.terms["payer"])
                    or _date(header.get("date")) > _today()):
                raise ValueError("请核对原版 RCTI 上的维州出具方、ABN、单号和日期。")
            self._same_number(issuer, number, uid)
            model = self.compare(uid)
            if model["digest"] != digest:
                raise ValueError("对比结果已变更，请更新页面再确认。")
            if model["total_only"] and not total_only:
                raise ValueError("请确认本次仅核对总额。")
            if not model["total_only"] and (model["totals"] != amounts or
                    any(v["status"] == "unmatched" for v in model["lines"])):
                raise ValueError("请先解决未匹配学生，并使学生明细合计与 RCTI 金额一致。")
            difference = amounts["gross_cents"] != model["expected_total"]
            if (difference or any(line["status"] == "different" for line in model["lines"])) and not reason.strip():
                raise ValueError("有差额或缺少可比对的预计佣金，请填写核对原因。")
            confirmed = dict(uid=uid, header=header, **amounts, comparison=model, reason=reason.strip(),
                             actor=self.actor, confirmed_at=self.repo.now(), contract_revision=self.terms["revision"])
            def operation():
                for line in model["lines"]:
                    self.db.execute("INSERT INTO revenue_vic_rcti_student VALUES(?,?,?,?)",
                                    (uid, self.sim, line["expected"]["component_uid"], line["actual"]["period"]))
                if prior:
                    final = {**prior, "comparison":model, "detail_reason":reason.strip(),
                             "detail_actor":self.actor, "detail_confirmed_at":self.repo.now()}
                else:
                    final = confirmed
                    for key, account in (("net_cents","CommissionRevenue"), ("gst_cents","GSTPayable")):
                        if amounts[key]:
                            self.service.append_balanced_ledger_event("invoiced", "vic_rcti", uid,
                                amounts[key], "CommissionReceivable", account, self.actor, None)
                    outstanding = amounts["gross_cents"]
                    for item in current["bank"]:
                        applied = min(outstanding, item["cents"])
                        if applied:
                            self.service.append_balanced_ledger_event("received", "vic_rcti_bank", item["uid"],
                                applied, "Bank", "CommissionReceivable", self.actor, None)
                            outstanding -= applied
                self.db.execute("""UPDATE revenue_vic_rcti SET confirmed_json=?,details_confirmed=? WHERE uid=?""",
                                (canonical_json(final), int(not model["total_only"]), uid))
                return final
            action = "vic_rcti_students_confirmed" if prior else "vic_rcti_confirmed"
            return self.repo.command(idempotency_key=action+":"+uid, command_type=action,
                aggregate_type="vic_rcti", payload=confirmed, actor_user_id=self.actor, operation=operation)

    def record_bank_receipt(self, uid, received_on, amount, reviewed, submission, receipt=None):
        if not isinstance(submission, str) or not submission.strip() or len(submission) > 200:
            raise ValueError("请刷新页面后重新提交到账记录。")
        with self.repo.transaction():
            if receipt is None:
                return self.bank(uid, "~VIC-RCTI-FORM~", _digest(submission), received_on, amount, reviewed)

            document_id = self._document(receipt, RECEIPT_KIND, receipt.sha256)
            return self.bank(uid, RECEIPT_ACCOUNT, str(document_id), received_on, amount, reviewed)

    def bank(self, uid, account, reference, received_on, amount, reviewed):
        account, reference = norm(account), norm(reference)
        day, cents = _date(received_on), aud_cents(amount)
        if not reviewed or not account or not reference or len(account)>100 or len(reference)>100 or day>_today() or cents<=0:
            raise ValueError("请核对实际到账日期和金额。")
        with self.repo.transaction():
            current = self.detail(uid)
            prior = self.db.execute("""SELECT * FROM revenue_vic_rcti_bank
               WHERE simulation=? AND account=? AND transaction_ref=?""", (self.sim,account,reference)).fetchone()
            if prior:
                if (prior["rcti_uid"],prior["received_on"],prior["cents"]) != (uid,day,cents):
                    if account == RECEIPT_ACCOUNT:
                        raise ValueError("这份收据已关联到账记录，请核对原记录。")
                    raise ValueError("这笔到账已登记，请核对原记录。")
                return dict(prior)
            applied = min(cents, current["balance"] or 0)
            payment = new_uid("vic-rcti-bank")
            def operation():
                self.db.execute("INSERT INTO revenue_vic_rcti_bank VALUES(?,?,?,?,?,?,?,?,?)",
                    (payment,uid,self.sim,account,reference,day,cents,self.actor,self.repo.now()))
                if applied:
                    self.service.append_balanced_ledger_event("received", "vic_rcti_bank", payment, applied,
                        "Bank", "CommissionReceivable", self.actor, None)
                return {"uid":payment,"rcti_uid":uid,"cents":cents,"received_on":day,"reference":reference}
            return self.repo.command(idempotency_key="vic-rcti-bank:"+_digest([self.sim,account,reference]),
                command_type="vic_rcti_bank_confirmed",aggregate_type="vic_rcti_bank",
                payload={"rcti_uid":uid,"day":day,"cents":cents},actor_user_id=self.actor,operation=operation)
