"""Upload review and confirmation of commission contracts for registered institutions."""

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
import json
import re
from zoneinfo import ZoneInfo

from revenue import vic_rcti
from revenue.money import aud_cents
from revenue.repository import canonical_json, new_uid
from revenue.school_commission_pilot import FORMAL_CONTRACT
from revenue.school_contract_extract import (
    add_onboarding_findings, extract_configurable_terms, read_pages,
)
from revenue.school_contracts import PROVIDER_REGISTRATION_ACTION, SCHOOL_REGISTRATION_ACTION
from revenue.secure_storage import StorageUnavailable
from revenue.state_school_commission import (
    DISCARDED_CONTRACT_UPLOAD, STATE_AUTHORITIES, authority_version_starts, discarded_contract_upload,
    school_commission_assignment,
)


CONFIRMED = "institution_commission_terms_confirmed"
FACTS = "institution_commission_evidence_confirmed"
TIER_LABELS = {
    "onshore_full": "境内招募 · 全日制",
    "offshore_full": "境外招募 · 全日制",
    "onshore_short": "境内招募 · 短期／英语",
    "offshore_short": "境外招募 · 短期／英语",
    "standard": "统一比例",
}
FEE_PERIODS = {
    "first_year": "首年学费",
    "first_half_year": "首半年学费",
    "each_year": "每年学费",
    "whole_course": "全课程学费",
}
GST_MODES = {"inclusive": "已含 10% GST", "exclusive": "另加 10% GST", "none": "不加 GST"}


def _digest(value):
    return sha256(canonical_json(value).encode()).hexdigest()


def extract_draft_terms(pages, institution, *, authority=None):
    """Read supported structures from actual contract language, retaining each source."""
    fields = extract_configurable_terms(pages)
    add_onboarding_findings(fields, pages, institution)
    if authority == "VIC" and (findings := vic_rcti.extract(pages)):
        fields.update(findings)
        return vic_rcti.PROFILE, fields
    rates, evidence = {}, {}
    condition_lines = []
    for page, text in enumerate(pages, 1):
        for match in re.finditer(
            r"(?im)^\s*(\d+)\.\s*A commission of (\d+(?:\.\d+)?)%\s+inclusive GST of total tuition fee"
            r"(?:(?!\n\s*\d+\.\s*A commission of)[\s\S]){0,520}?"
            r"\((?:Material(?:s)? fees and Enrolment\s+fee are not included)\)",
            text,
        ):
            wording = " ".join(match[0].split())
            location = re.search(r"\b(ONSHORE|OFFSHORE)\b", wording, re.I)
            if not location:
                continue
            course = "short" if re.search(r"short.course|English Courses", wording, re.I) else (
                "full" if re.search(r"full.time courses", wording, re.I) else ""
            )
            if course:
                tier = f"{location[1].lower()}_{course}"
                rates.setdefault(tier, set()).add(match[2])
                evidence.setdefault(tier, []).append(
                    {"page": page, "clause": f"Item 3({match[1]})", "text": wording}
                )
        for match in re.finditer(r"(?im)^\s*(6\.1\.\d+|6\.3)\s+([^\r\n]+)", text):
            condition_lines.append({"page": page, "clause": match[1], "text": " ".join(match[0].split())})
        for match in re.finditer(
            r"(?im)^\s*(\d+(?:\.\d+)+)\s+([^\n]{0,120}?\bwithin\s+(\d+)\s+days?\s+of\s+receipt\s+of\s+a\s+valid\s+invoice(?:\s+from\s+THE\s+AGENT)?\.?)",
            text,
        ):
            fields["payment_days"] = {
                "value": match[3], "sources": [{"page": page, "clause": match[1], "text": " ".join(match[0].split())}],
            }
        term = re.search(r"(?im)^Item\s+1:\s*Term\s*\n([^\n]+)", text)
        if term:
            evidence["term"] = [{"page": page, "clause": "Item 1", "text": term[1].strip()}]
    matrix = set(TIER_LABELS) - {"standard"}
    if rates or any(
        re.search(r"\bonshore\b", text, re.I) and re.search(r"\boffshore\b", text, re.I)
        and re.search(r"inclusive\s+GST of total tuition fee", text, re.I)
        for text in pages
    ):
        for tier in matrix:
            fields[tier] = {
                "value": next(iter(rates[tier])) if len(rates.get(tier, ())) == 1 else "",
                "sources": evidence.get(tier, []),
            }
        fields["gst"] = {"value": "inclusive", "sources": evidence.get("onshore_full", [])}
        fields["tuition_period"] = {"value": "whole_course", "sources": evidence.get("onshore_full", [])}
        if condition_lines:
            fields["conditions"] = {
                "value": "\n".join(item["text"] for item in condition_lines), "sources": condition_lines,
            }
        if evidence.get("term"):
            for field in ("start", "end"):
                if not fields[field]["sources"]:
                    fields[field]["sources"] = evidence["term"]
        profile = "recruitment_matrix"
    else:
        tuition_rates = set()
        for text in pages:
            for clause in re.split(r"[;\n]|(?<=[.!?])\s+", text):
                if re.search(r"\b(?:commission|agent[’']s fee)\b", clause, re.I):
                    tuition_rates.update(re.findall(r"(\d+(?:\.\d+)?)\s*%", clause))
        profile = "unsupported" if len(tuition_rates) > 1 else "standard"
        fields["standard"] = fields["rate"]
    return profile, fields


def _value(form, name):
    value = form.get(name, "")
    if not isinstance(value, str):
        raise ValueError("合同表单字段重复，请重新打开页面。")
    return value.strip()


def _rate(value):
    try:
        points = Decimal(value) * 100
        if not points.is_finite() or not 0 < points <= 10000 or points != int(points):
            raise ValueError
        return int(points)
    except (InvalidOperation, ValueError):
        raise ValueError("佣金比例须介于 0% 至 100% 之间，最多两位小数。") from None


class InstitutionCommissionDraft:
    def __init__(self, repository, pilot, uid, actor):
        self.repo = repository
        self.db = repository.connection
        self.pilot = pilot
        self.uid = uid
        self.actor = actor
        if not pilot or not pilot.contract_intake or pilot.self_service:
            raise PermissionError("当前账号无权使用佣金草稿。")
        if discarded_contract_upload(self.db, uid):
            raise LookupError("该合同上传已删除。")
        draft = repository.require_by_uid("revenue_rule_snapshots", uid)
        config = json.loads(draft["canonical_json"])
        self.document = repository.require_by_uid("revenue_documents", draft["source_rule_uid"])
        if (draft["rule_kind"] != "commission" or config.get("pilot_entity_id") != 0
                or config.get("pilot_simulation") is not pilot.simulation or config.get("signed_confirmed") is not True
                or self.document["document_kind"] != FORMAL_CONTRACT
                or self.document["mime_type"] != "application/pdf"):
            raise PermissionError("请选择已登记机构的签署合同草稿。")
        provider = config.get("higher_education_provider_id")
        school = config.get("school_id")
        authority = config.get("education_authority")
        states = getattr(pilot, "education_authorities", ())
        if isinstance(authority, str) and authority in STATE_AUTHORITIES and provider is None and school is None:
            if authority not in states:
                raise PermissionError("州／领地教育局尚未开放。")
            self.target = ("authority", authority)
            self.name = STATE_AUTHORITIES[authority]
            return
        if authority is not None:
            raise PermissionError("合同草稿的机构身份不明确。")
        if type(provider) is int and provider > 0 and school is None:
            self.target = ("provider", provider)
            party = self.db.execute(
                """SELECT provider_name AS name FROM higher_education_providers WHERE id=?
                   AND active_status='Active' AND review_status IN ('Pending','Approved')""", (provider,),
            ).fetchone()
            action, aggregate = PROVIDER_REGISTRATION_ACTION, "school_commission_provider"
        elif type(school) is int and school > 0 and provider is None and config.get("school_commission_configurable"):
            self.target = ("school", school)
            party = self.db.execute("SELECT school_name AS name FROM schools WHERE id=? AND deleted_at IS NULL", (school,)).fetchone()
            action, aggregate = SCHOOL_REGISTRATION_ACTION, "school_commission_school"
        else:
            raise PermissionError("合同草稿的机构身份不明确。")
        if not party or not self.db.execute(
            "SELECT 1 FROM revenue_audit_events WHERE action=? AND aggregate_type=? AND aggregate_uid=?",
            (action, aggregate, f"{int(pilot.simulation)}:{self.target[1]}"),
        ).fetchone():
            raise PermissionError("该机构尚未登记或已失效。")
        self.name = party["name"]

    def analyse(self, ocr):
        from revenue.school_contract_storage import configured_contract_store

        try:
            pdf = configured_contract_store().read(
                Path(self.document["storage_path"]), self.document["sha256"], self.document["size_bytes"],
            )
            pages, status = read_pages(pdf, ocr)
        except (OSError, StorageUnavailable):
            pages, status = [], "unreadable"
        profile, fields = extract_draft_terms(
            pages, self.name, authority=self.target[1] if self.target[0] == "authority" else None,
        )
        return {"profile": profile, "fields": fields, "status": status}

    def terms(self):
        row = self.db.execute(
            """SELECT after_json FROM revenue_audit_events WHERE action=? AND aggregate_uid=?
                ORDER BY id DESC LIMIT 1""", (CONFIRMED, self.uid),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def discard(self, key):
        def operation():
            if self.terms() or self.db.execute(
                "SELECT 1 FROM revenue_contract_versions WHERE document_id=?", (self.document["id"],),
            ).fetchone():
                raise ValueError("已确认的合同不能删除。")
            if discarded_contract_upload(self.db, self.uid):
                raise ValueError("该合同上传已删除。")
            return {"uid": self.uid, "document_uid": self.document["uid"], "target": list(self.target)}

        return self.repo.command(
            idempotency_key=key, command_type=DISCARDED_CONTRACT_UPLOAD, aggregate_type="rule_snapshot",
            payload={"draft": self.uid}, actor_user_id=self.actor, operation=operation,
        )

    def confirm(self, form, analysis, key):
        if discarded_contract_upload(self.db, self.uid):
            raise ValueError("该合同上传已删除。")
        profile = analysis["profile"]
        if profile == "unsupported":
            raise ValueError("此合同包含尚未支持的多档佣金，请保留草稿并复核各档规则。")
        if _value(form, "analysis_revision") != _digest(analysis):
            raise ValueError("提取结果已变更，请重新核对。")
        start, end = (date.fromisoformat(_value(form, name)).isoformat() for name in ("start", "end"))
        if start > end:
            raise ValueError("合同到期日不得早于生效日。")
        if self.target[0] == "authority" and any(
            start <= previous for previous in authority_version_starts(self.db, self.target[1], self.pilot.simulation)
        ):
            raise ValueError("续约生效日须晚于该教育局上一版合同的生效日。")
        rcti = profile == vic_rcti.PROFILE
        if rcti and self.target != ("authority", "VIC"):
            raise PermissionError("该佣金条款仅适用于维州教育局合同。")
        names = tuple(vic_rcti.LABELS) if rcti else tuple(
            k for k in TIER_LABELS if (k == "standard") == (profile == "standard")
        )
        rates = {name: _rate(_value(form, name)) for name in names}
        gst, period = _value(form, "gst"), _value(form, "tuition_period")
        if gst not in GST_MODES or period not in FEE_PERIODS:
            raise ValueError("请确认 GST 和计佣学费周期。")
        if rcti and period != "first_year":
            raise ValueError("维州基础佣金以首年实付学费为基础。")
        if profile == "recruitment_matrix" and period != "whole_course":
            raise ValueError("这组课程档位须按合同中的全课程学费复核。")
        conditions = _value(form, "conditions")
        payer = _value(form, "school_name")
        payment_days = _value(form, "payment_days")
        if (not payer or len(payer) > 200 or not conditions or len(conditions) > 6000
                or (not rcti and (not re.fullmatch(r"\d{1,3}", payment_days) or int(payment_days) > 365))):
            raise ValueError("请核对付款主体、申领条件和付款天数。")
        if _value(form, "reviewed") != "1":
            raise ValueError("请确认已核对合同原件。")
        result = {"uid": self.uid, "revision": new_uid("terms"), "target": list(self.target), "profile": profile,
                  "payer": payer, "rates": rates, "gst": gst, "period": period, "conditions": conditions,
                  "start": start, "end": end, "payment_days": None if rcti else int(payment_days),
                  "pdf_sha256": self.document["sha256"], "findings": analysis["fields"]}
        if rcti:
            result["payment_mode"] = vic_rcti.PAYMENT
        def operation():
            if discarded_contract_upload(self.db, self.uid):
                raise ValueError("该合同上传已删除。")
            return result

        return self.repo.command(
            idempotency_key=key, command_type=CONFIRMED, aggregate_type="institution_commission_terms",
            payload={"draft": self.uid, "form": dict(form)}, actor_user_id=self.actor, operation=operation,
        )

    def _components(self):
        from revenue.read_models import RevenueReadModels

        if self.target != ("authority", "VIC"):
            return []
        assignment = school_commission_assignment(self.db)
        school_ids = [key for key, (sector, state) in assignment.items() if state == "VIC" and sector != "private"]
        rows = RevenueReadModels(self.db).school_claim_components(None, authority_school_ids=school_ids)
        return [
            {**row, "assignment_reason": "请补齐学校公私立分类。"} if not assignment[row["school_id"]][0] else row
            for row in rows
        ]

    def _facts(self, terms):
        rows = self.db.execute(
            """SELECT after_json FROM revenue_audit_events
                WHERE action=? AND aggregate_uid=? AND json_extract(after_json,'$.revision')=?
                ORDER BY id""", (FACTS, self.uid, terms["revision"]),
        )
        result = {}
        for row in rows:
            value = json.loads(row[0])
            result[value["component_uid"]] = value
        return result

    def confirm_student_evidence(self, form, terms, key):
        if self.target != ("authority", "VIC") or terms.get("payment_mode") != vic_rcti.PAYMENT or terms != self.terms():
            raise PermissionError("请先核对有效的维州 RCTI 合同。")
        component, reference = _value(form, "component"), _value(form, "reference")
        if not reference or len(reference) > 240 or _value(form, "eligible") != "1":
            raise ValueError("请确认已核对申领条件和凭证。")
        if not any(row["component_uid"] == component and not row.get("assignment_reason") for row in self._components()):
            raise ValueError("请选择本机构已关联的学生入学项目。")
        duration, english = _value(form, "duration"), _value(form, "english")
        if duration not in {"short", "full"} or _value(form, "visa") != "500":
            raise ValueError("请核对课程期限和学生 500 签证。")
        if english not in {"no", "yes"}:
            raise ValueError("请核对是否符合两学期强化英语课程要求。")
        paid = aud_cents(_value(form, "paid_tuition") or ("0" if duration == "short" else ""))
        bonus = aud_cents(_value(form, "english_paid")) if english == "yes" and duration == "full" else 0
        if duration == "full" and (paid <= 0 or (english == "yes" and bonus <= 0)):
            raise ValueError("请分别填写已核对的实付学费。")
        result = {"uid": self.uid, "revision": terms["revision"], "component_uid": component,
                  "tier": "standard", "reference": reference,
                  "offer_on": date.fromisoformat(_value(form, "offer_on")).isoformat(),
                  "duration": duration, "studying": _value(form, "studying") == "1",
                  "paid_tuition_cents": paid, "english_paid_cents": bonus}
        return self.repo.command(
            idempotency_key=key, command_type=FACTS, aggregate_type="institution_commission_student_evidence",
            payload=result, actor_user_id=self.actor, operation=lambda: result,
        )

    def candidates(self, terms):
        if self.target == ("authority", "VIC") and terms.get("payment_mode") == vic_rcti.PAYMENT:
            yield from self._vic_estimates(terms, self._facts(terms))

    def _vic_estimates(self, terms, facts):
        today = datetime.now(ZoneInfo("Australia/Melbourne")).date().isoformat()
        next_start = min((start for start in authority_version_starts(self.db, "VIC", self.pilot.simulation)
                          if start > terms["start"]), default="")
        for student in self._components():
            component = student["component_uid"]
            row = {key: student[key] for key in ("student_id", "student_name", "component_uid", "school_name")
                   if key in student}
            if student.get("assignment_reason"):
                yield {**row, "reason": student["assignment_reason"]}
                continue
            commenced = self.db.execute(
                """SELECT actual_commencement_date FROM revenue_commencement_facts
                    WHERE enrolment_component_id=? AND status='confirmed'""", (student["component_id"],),
            ).fetchone()
            fact = facts.get(component)
            result = vic_rcti.estimate(terms, fact, commenced[0] if commenced else "", today, next_start=next_start)
            if "gross_cents" in result:
                result["eligible_on"] = (date.fromisoformat(commenced[0]) + timedelta(weeks=4)).isoformat()
            yield {**row, **result, "evidence": fact["reference"] if fact else ""}
