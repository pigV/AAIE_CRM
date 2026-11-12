"""Production draft-only commission workflow for registered institutions."""

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
import json
import re
from zoneinfo import ZoneInfo

from revenue import vic_rcti
from revenue.money import apply_basis_points, aud_cents, commission_gst_amounts
from revenue.repository import canonical_json, new_uid
from revenue.school_commission_pilot import FORMAL_CONTRACT
from revenue.school_contract_extract import (
    NSW_SCHEDULE_CONFIRM_BLOCK, NSW_SCHEDULE_PROFILE, add_onboarding_findings,
    extract_configurable_terms, extract_nsw_schedule, normalize_value, read_pages,
)
from revenue.school_contracts import PROVIDER_REGISTRATION_ACTION, SCHOOL_REGISTRATION_ACTION
from revenue.secure_storage import StorageUnavailable
from revenue.state_school_commission import (
    DISCARDED_CONTRACT_UPLOAD, STATE_AUTHORITIES, authority_version_starts, discarded_contract_upload,
    school_commission_assignment,
)


CONFIRMED = "institution_commission_terms_confirmed"
NSW_REVIEW_SAVED = "nsw_commission_review_saved"
NSW_CLAIMS_ONLY = "nsw_claims_only"
NSW_BLOCK_INVOICE = "NSW 当前仅开放核对申领条件和预计佣金。"
NSW_RATE_LABELS = {"standard": "基础佣金", "iec_rate": "IEC 佣金"}
FACTS = "institution_commission_evidence_confirmed"
DRAFT = "institution_commission_invoice_draft_created"
DRAFT_GST_CHANGED = "institution_commission_invoice_draft_gst_changed"
DRAFT_REPREVIEWED = "institution_commission_invoice_draft_repreviewed"
INVOICE_ISSUED = "institution_commission_invoice_issued"
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
    if authority == "NSW" and (findings := extract_nsw_schedule(pages)):
        fields.update(findings)
        return NSW_SCHEDULE_PROFILE, fields
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
            pages, status = read_pages(pdf, ocr, review_nsw_schedule=self.target == ("authority", "NSW"))
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

    def nsw_review(self):
        if self.target != ("authority", "NSW"):
            return None
        row = self.db.execute(
            """SELECT after_json FROM revenue_audit_events
                 WHERE action=? AND aggregate_uid=? AND json_extract(after_json,'$.pdf_sha256')=?
                 ORDER BY id DESC LIMIT 1""",
            (NSW_REVIEW_SAVED, self.uid, self.document["sha256"]),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_nsw_review(self, form, analysis, key):
        if self.target != ("authority", "NSW") or analysis["profile"] != NSW_SCHEDULE_PROFILE:
            raise PermissionError("此操作仅适用于已识别的 NSW 佣金合同。")
        analysis_revision = _value(form, "analysis_revision")
        if analysis_revision != _digest(analysis):
            raise ValueError("提取结果已变更，请刷新后重新核对。")
        values = {
            name: normalize_value(name, _value(form, name))
            for name in ("standard", "iec_rate", "start", "end", "gst")
        }
        if values["start"] > values["end"]:
            raise ValueError("合同到期日不得早于生效日。")
        conditions = _value(form, "conditions")
        if not conditions or len(conditions) > 6000:
            raise ValueError("请填写申领条件，最多 6000 个字符。")
        payment_days = _value(form, "payment_days")
        values.update(conditions=conditions, payment_days=(
            normalize_value("payment_days", payment_days) if payment_days else ""
        ))
        if "school_name" in form:
            school_name = _value(form, "school_name")
            values["school_name"] = normalize_value("school_name", school_name) if school_name else ""
        revision = _value(form, "review_revision")

        def operation():
            if self.terms() or discarded_contract_upload(self.db, self.uid):
                raise ValueError("合同状态已变更，请刷新后核对。")
            current = self.nsw_review()
            if revision != (current["revision"] if current else ""):
                raise ValueError("已有更新的核对结果，请刷新后重试。")
            saved_values = {**(current["values"] if current else {}), **values}
            return {"uid": self.uid, "revision": new_uid("nsw-review"), "values": saved_values,
                    "pdf_sha256": self.document["sha256"], "analysis": analysis}

        return self.repo.command(
            idempotency_key=key, command_type=NSW_REVIEW_SAVED, aggregate_type="nsw_commission_review",
            payload={"contract": self.uid, "review_revision": revision,
                     "analysis_revision": analysis_revision, "values": values},
            actor_user_id=self.actor, operation=operation,
        )

    def enable_nsw_claims(self, review_revision, key):
        if self.target != ("authority", "NSW") or not self.pilot.enabled:
            raise PermissionError("当前未开放 NSW 佣金申领。")

        def operation():
            review = self.nsw_review()
            if self.terms() or not review or review_revision != review["revision"] or discarded_contract_upload(
                self.db, self.uid
            ):
                raise ValueError("合同核对状态已变更，请刷新后重试。")
            values = review["values"]
            if not values.get("school_name"):
                raise ValueError("请先核对合同付款主体。")
            if any(values["start"] <= previous for previous in authority_version_starts(
                self.db, "NSW", self.pilot.simulation
            )):
                raise ValueError("续约生效日须晚于该教育局上一版合同的生效日。")
            return {
                "uid": self.uid, "revision": new_uid("terms"), "target": list(self.target),
                "profile": NSW_SCHEDULE_PROFILE, "payment_mode": NSW_CLAIMS_ONLY,
                "payer": values["school_name"],
                "rates": {name: _rate(values[name]) for name in NSW_RATE_LABELS},
                "start": values["start"], "end": values["end"], "gst": values["gst"],
                "period": "first_year", "conditions": values["conditions"],
                "payment_days": int(values["payment_days"]) if values["payment_days"] else None,
                "pdf_sha256": self.document["sha256"], "findings": review["analysis"]["fields"],
                "review_revision": review_revision,
            }

        return self.repo.command(
            idempotency_key=key, command_type=CONFIRMED, aggregate_type="institution_commission_terms",
            payload={"draft": self.uid, "enable_nsw_review": review_revision},
            actor_user_id=self.actor, operation=operation,
        )

    def revise_terms(self, form, key):
        revision = _value(form, "revision")

        def operation():
            current = self.terms()
            if not current or revision != current["revision"]:
                raise ValueError("合同条款已变更，请刷新后再编辑。")
            start, end = (normalize_value(name, _value(form, name)) for name in ("start", "end"))
            if start > end:
                raise ValueError("合同到期日不得早于生效日。")
            if self.target[0] == "authority":
                from revenue.state_school_commission import authority_contracts

                for uid, payload in authority_contracts(self.db, self.target[1], self.pilot.simulation):
                    if uid == self.uid or not payload:
                        continue
                    other = json.loads(payload)
                    if (current["start"] < other["start"]) != (start < other["start"]) or start == other["start"]:
                        raise ValueError("合同生效日不能越过其他已确认版本的生效日。")
            gst = _value(form, "gst")
            conditions = _value(form, "conditions")
            if gst not in GST_MODES or not conditions or len(conditions) > 6000:
                raise ValueError("请核对 GST 和申领条件。")
            mode = current.get("payment_mode")
            period = current["period"] if mode in {NSW_CLAIMS_ONLY, vic_rcti.PAYMENT} else _value(form, "period")
            if period not in FEE_PERIODS or (current["profile"] == "recruitment_matrix" and period != "whole_course"):
                raise ValueError("请选择合同支持的计佣学费周期。")
            days = _value(form, "payment_days")
            if mode != vic_rcti.PAYMENT and (days or mode != NSW_CLAIMS_ONLY):
                days = int(normalize_value("payment_days", days))
            else:
                days = None
            rates = {name: _rate(_value(form, name)) for name in current["rates"]}
            version = new_uid("terms")
            evidence = (current.get("evidence_revision", current["revision"])
                        if conditions == current["conditions"] else version)
            return {**current, "revision": version, "previous_revision": revision,
                    "evidence_revision": evidence, "start": start, "end": end,
                    "gst": gst, "conditions": conditions, "period": period,
                    "payment_days": days, "rates": rates}

        return self.repo.command(
            idempotency_key=key, command_type=CONFIRMED, aggregate_type="institution_commission_terms",
            payload={"draft": self.uid, "revision": revision, "form": dict(form)},
            actor_user_id=self.actor, operation=operation,
        )

    def _billing_payer(self, *, create=False, key=""):
        terms = self.terms()
        if not terms:
            raise ValueError("请先确认学校佣金条款。")
        if self.target[0] not in {"school", "provider"}:
            raise PermissionError("请从学校或高等教育机构合同填写开票资料。")
        from revenue.services import RevenueService
        from revenue.school_contracts import SchoolCommissionContracts

        column, entity_type = {
            "school": ("school_id", "school"),
            "provider": ("higher_education_provider_id", "higher_education_provider"),
        }[self.target[0]]
        rows = self.db.execute(
            f"""SELECT e.* FROM revenue_entity_provider_links l JOIN revenue_entities e
                  ON e.id=l.revenue_entity_id WHERE l.{column}=? AND l.effective_to IS NULL""",
            (self.target[1],),
        ).fetchall()
        if rows:
            if (len(rows) != 1 or rows[0]["status"] != "active" or rows[0]["entity_type"] != entity_type
                    or rows[0]["legal_name"].casefold() != terms["payer"].casefold()):
                raise ValueError("该学校的付款主体关联与已确认合同不一致，请核对。")
            return dict(rows[0])
        if not create:
            return None
        service = RevenueService(self.repo)
        if self.target[0] == "school":
            return SchoolCommissionContracts(service, self.actor, "Admin").bind(
                self.target[1], terms["payer"], terms["start"], key + ":school-payer",
            )
        candidates = self.db.execute(
            "SELECT * FROM revenue_entities WHERE entity_type=? AND lower(legal_name)=lower(?)",
            (entity_type, terms["payer"]),
        ).fetchall()
        if candidates:
            if (len(candidates) != 1 or candidates[0]["status"] != "active" or self.db.execute(
                    "SELECT 1 FROM revenue_entity_provider_links WHERE revenue_entity_id=?",
                    (candidates[0]["id"],),
            ).fetchone()):
                raise ValueError("存在已绑定或冲突的同名付款主体，请核对。")
            payer = dict(candidates[0])
        else:
            payer = service.create_revenue_entity(entity_type, terms["payer"], self.actor, key + ":provider-payer")
        self.db.execute(
            """INSERT INTO revenue_entity_provider_links
               (uid,revenue_entity_id,higher_education_provider_id,effective_from,created_at)
               VALUES(?,?,?,?,?)""",
            (new_uid("provider-link"), payer["id"], self.target[1], terms["start"], self.repo.now()),
        )
        return payer

    def invoice_billing_settings(self):
        from revenue.invoice_billing_details import billing_details

        payer = self._billing_payer()
        if payer:
            return billing_details(self.db, payer["id"], self.pilot.simulation)
        return dict(name=self.terms()["payer"], address="", phone="", recipient="", revision="",
                    settings_revision="", enabled=False, verified=False)

    def save_invoice_billing(self, form, key, *, allow_auto_mail=False):
        from revenue.invoice_billing_details import billing_details, save_billing_details

        with self.repo.transaction():
            payer = self._billing_payer(create=True, key=key)
            current = billing_details(self.db, payer["id"], self.pilot.simulation)
            if not allow_auto_mail and form.get("enabled") == "1" and not current["enabled"]:
                raise ValueError("请先完成开票资料核对，再单独启用该学校的自动发信。")
            # This step edits addressee details, never the payer's delivery permission.
            details = form if allow_auto_mail else {**form, "enabled": "1" if current["enabled"] else "0"}
            return save_billing_details(self.repo, payer["id"], self.actor, key, details,
                                        simulation=self.pilot.simulation)

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
        if profile == NSW_SCHEDULE_PROFILE:
            raise ValueError(NSW_SCHEDULE_CONFIRM_BLOCK)
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

        model = RevenueReadModels(self.db)
        kind, target_id = self.target
        if kind == "school":
            rows = model.school_claim_components(target_id)
            assignment = school_commission_assignment(self.db)
            if target_id in assignment:
                sector, state = assignment[target_id]
                if state in getattr(self.pilot, "education_authorities", ()) and sector != "private":
                    reason = ("请补齐学校公私立分类。" if not sector
                              else "该公校的佣金由所在州／领地教育局结算。")
                    return [{**row, "assignment_reason": reason} for row in rows]
            return rows
        if kind == "authority":
            assignment = school_commission_assignment(self.db)
            school_ids = [school for school, (sector, state) in assignment.items()
                          if state == target_id and sector != "private"]
            return [
                {**row, "assignment_reason": "请补齐学校公私立分类。"}
                if not assignment[row["school_id"]][0] else row
                for row in model.school_claim_components(None, authority_school_ids=school_ids)
            ]
        rows = self.db.execute(
            """SELECT a.id AS application_id,a.course_name,a.course_type,a.submission_channel,
                      x.student_id,c.id AS case_id,c.uid AS case_uid,
                      ec.id AS component_id,ec.uid AS component_uid,ec.status AS component_status,
                      EXISTS(SELECT 1 FROM higher_education_offers o
                               WHERE o.application_id=a.id AND o.status='Accepted') AS accepted
                 FROM higher_education_applications a
                 JOIN application_cycles x ON x.id=a.application_cycle_id
                 JOIN students s ON s.student_id=x.student_id
                 LEFT JOIN revenue_commercial_cases c ON c.application_cycle_id=x.id AND c.student_id=x.student_id
                 LEFT JOIN revenue_enrolment_components ec ON ec.commercial_case_id=c.id
                      AND ec.component_kind='higher_education' AND ec.higher_education_application_id=a.id
                WHERE a.provider_id=? AND a.is_current=1 AND x.education_stream='higher_education'
                  AND a.status NOT IN ('Closed','Withdrawn','Superseded') AND """
            + model.visibility.student_predicate("s") + " ORDER BY x.student_id,a.id",
            (target_id,),
        )
        return [{**dict(row), "student_name": model._student_display_name(row["student_id"])} for row in rows]

    def _drafts_for_target(self, *, all_targets=False):
        rows = self.db.execute(
            """SELECT coalesce((
                    SELECT change.after_json FROM revenue_audit_events change
                     WHERE change.action IN (?,?) AND change.aggregate_uid=draft.aggregate_uid
                     ORDER BY change.id DESC LIMIT 1
                ), draft.after_json)
                FROM revenue_audit_events draft
                WHERE draft.action=? AND (? OR (json_extract(draft.after_json,'$.target[0]')=?
                  AND json_extract(draft.after_json,'$.target[1]')=?))
                  AND json_extract(draft.after_json,'$.simulation')=? ORDER BY draft.id DESC""",
            (DRAFT_GST_CHANGED, DRAFT_REPREVIEWED, DRAFT, all_targets, *self.target, int(self.pilot.simulation)),
        )
        return [json.loads(row[0]) for row in rows]

    def invoice(self, uid):
        return next((row for row in self._drafts_for_target()
                     if row["uid"] == uid and row["contract_draft_uid"] == self.uid), None)

    def invoice_issued(self, draft_uid):
        if self.target[0] == "authority" and self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='revenue_authority_invoices'"
        ).fetchone():
            return bool(self.db.execute(
                "SELECT 1 FROM revenue_authority_invoices WHERE draft_uid=? AND contract_draft_uid=?",
                (draft_uid, self.uid),
            ).fetchone())
        return bool(self.db.execute(
            """SELECT 1 FROM revenue_audit_events WHERE action=?
                 AND json_extract(after_json,'$.draft_uid')=?
                 AND json_extract(after_json,'$.contract_draft_uid')=?
                 AND json_extract(after_json,'$.simulation')=? LIMIT 1""",
            (INVOICE_ISSUED, draft_uid, self.uid, int(self.pilot.simulation)),
        ).fetchone())

    @staticmethod
    def invoice_line_gst(line, gst_mode):
        commission = apply_basis_points(line["tuition_cents"], line["rate_basis_points"])
        return {**line, **commission_gst_amounts(commission, gst_mode)}

    def draft_needs_repreview(self, invoice, terms):
        return bool(terms and invoice["terms_revision"] != terms["revision"] and not self.invoice_issued(invoice["uid"]))

    def repreview_invoice(self, draft_uid, revision, key):
        def operation():
            invoice, terms = self.invoice(draft_uid), self.terms()
            if not invoice or not self.draft_needs_repreview(invoice, terms) or revision != terms["revision"]:
                raise ValueError("草稿或合同已更新，请刷新后重新预览。")
            available = [row for row in self.candidates(terms, excluding_draft=draft_uid) if row.get("uid")]
            lines = []
            for old in invoice["lines"]:
                choices = [row for row in available if row["component_uid"] == old["component_uid"]]
                exact = next((row for row in choices if row["academic_period"] == old["academic_period"]), None)
                if not exact and len(choices) != 1:
                    raise ValueError("学生学费或申领条件已变更，请先核对学生资料，再重新预览。")
                lines.append(exact or choices[0])
            if len({line["uid"] for line in lines}) != len(lines):
                raise ValueError("计佣周期已变更，原草稿中有重叠明细，请先核对学生资料。")
            if "gst_mode" in invoice:
                lines = [self.invoice_line_gst(row, invoice["gst_mode"]) for row in lines]
            totals = {field: sum(line[field] for line in lines) for field in ("net_cents", "gst_cents", "gross_cents")}
            return {**invoice, "terms_revision": revision, "payment_days": terms["payment_days"], "lines": lines, **totals}

        return self.repo.command(
            idempotency_key=key, command_type=DRAFT_REPREVIEWED, aggregate_type="institution_commission_invoice_draft",
            payload={"draft_uid": draft_uid, "revision": revision, "contract": self.uid},
            actor_user_id=self.actor, operation=operation,
        )

    def save_invoice_gst(self, draft_uid, gst_mode, revision, key):
        if self.target[0] not in {"school", "provider"}:
            raise PermissionError("请从学校发票草稿修改 GST。")
        if not isinstance(gst_mode, str) or gst_mode not in GST_MODES:
            raise ValueError("请选择有效的发票 GST 方式。")

        def operation():
            draft = self.invoice(draft_uid)
            terms = self.terms()
            if not draft or not terms or draft["terms_revision"] != terms["revision"]:
                raise ValueError("找不到当前合同的发票草稿，请刷新后重试。")
            if self.invoice_issued(draft_uid):
                raise ValueError("这张发票已签发，不能修改 GST。")
            if revision != _digest(draft):
                raise ValueError("草稿已更新，请刷新后重新选择 GST。")
            lines = [self.invoice_line_gst(line, gst_mode) for line in draft["lines"]]
            totals = {field: sum(line[field] for line in lines) for field in ("net_cents", "gst_cents", "gross_cents")}
            return {**draft, "gst_mode": gst_mode, "lines": lines, **totals}

        return self.repo.command(
            idempotency_key=key, command_type=DRAFT_GST_CHANGED, aggregate_type="institution_commission_invoice_draft",
            payload={"draft_uid": draft_uid, "gst_mode": gst_mode, "revision": revision},
            actor_user_id=self.actor, operation=operation,
        )

    def _facts(self, terms):
        rows = self.db.execute(
            """SELECT after_json FROM revenue_audit_events
                WHERE action=? AND aggregate_uid=? AND json_extract(after_json,'$.revision')=?
                ORDER BY id""", (FACTS, self.uid, terms.get("evidence_revision", terms["revision"])),
        )
        result = {}
        for row in rows:
            value = json.loads(row[0])
            result[value["component_uid"]] = value
        return result

    @staticmethod
    def _nsw_evidence(form, terms):
        period, iec = _value(form, "base_period"), _value(form, "iec")
        if period not in {"first_year", "whole_course"} or iec not in {"yes", "no"}:
            raise ValueError("请核对基础学费周期及是否符合 IEC 条件。")
        result = {"tier": "standard", "base_period": period, "iec_tuition_cents": 0}
        if iec == "no":
            return result
        if _value(form, "iec_followed_secondary") != "1":
            raise ValueError("请确认学生已开始 IEC 并衔接 NSW 公立中学课程。")
        commenced = _value(form, "iec_commenced_on")
        today = datetime.now(ZoneInfo("Australia/Melbourne")).date().isoformat()
        if not commenced or not terms["start"] <= date.fromisoformat(commenced).isoformat() <= min(today, terms["end"]):
            raise ValueError("IEC 实际开学日须在本合同有效期内，且已经开学。")
        term_count, week_count = _value(form, "iec_terms"), _value(form, "iec_weeks")
        if term_count not in {"1", "2"} or not re.fullmatch(r"(?:[1-9]|1[0-9]|20)", week_count):
            raise ValueError("IEC 上限为两学期、最多 20 周。请按凭证填写限额内的合资格 IEC 学费。")
        tuition = aud_cents(_value(form, "iec_tuition"))
        if tuition <= 0:
            raise ValueError("请填写限额内有凭证的 IEC 学费。")
        return {**result, "iec_tuition_cents": tuition, "iec_commenced_on": commenced,
                "iec_terms": int(term_count), "iec_weeks": int(week_count)}

    def confirm_student_evidence(self, form, terms, key):
        component = _value(form, "component")
        tier, reference = _value(form, "tier"), _value(form, "reference")
        rcti = terms.get("payment_mode") == vic_rcti.PAYMENT
        nsw = terms.get("payment_mode") == NSW_CLAIMS_ONLY
        if (not reference or len(reference) > 240 or _value(form, "eligible") != "1"
                or (not (rcti or nsw) and tier not in terms["rates"])):
            raise ValueError("请确认已核对申领条件和凭证。")
        if not any(row["component_uid"] == component and not row.get("assignment_reason")
                   for row in self._components()):
            raise ValueError("请选择本机构已关联的学生入学项目。")
        result = {"uid": self.uid, "revision": terms.get("evidence_revision", terms["revision"]), "component_uid": component,
                  "tier": tier, "reference": reference}
        if nsw:
            result.update(self._nsw_evidence(form, terms))
        if rcti:
            duration = _value(form, "duration")
            if duration not in {"short", "full"} or _value(form, "visa") != "500":
                raise ValueError("请核对课程期限和学生 500 签证。")
            english = _value(form, "english")
            if english not in {"no", "yes"}:
                raise ValueError("请核对是否符合两学期强化英语课程要求。")
            paid = aud_cents(_value(form, "paid_tuition") or ("0" if duration == "short" else ""))
            bonus = aud_cents(_value(form, "english_paid")) if english == "yes" and duration == "full" else 0
            if duration == "full" and (paid <= 0 or (english == "yes" and bonus <= 0)):
                raise ValueError("请分别填写已核对的实付学费。")
            result.update(offer_on=date.fromisoformat(_value(form, "offer_on")).isoformat(),
                          duration=duration, studying=_value(form, "studying") == "1",
                          paid_tuition_cents=paid, english_paid_cents=bonus, tier="standard")
        return self.repo.command(
            idempotency_key=key, command_type=FACTS, aggregate_type="institution_commission_student_evidence",
            payload=result, actor_user_id=self.actor, operation=lambda: result,
        )

    @staticmethod
    def _period_matches(mode, academic_period):
        value = academic_period.casefold().strip()
        if mode == "first_year":
            return value in {"year_1", "first_year"}
        if mode == "first_half_year":
            return value in {"first_half_year", "semester_1", "half_year_1"}
        if mode == "whole_course":
            return value in {"whole_course", "total_course", "full_course"}
        return bool(re.fullmatch(r"year_[1-9]\d*", value))

    def candidates(self, terms, *, excluding_draft=None):
        facts = self._facts(terms)
        if terms.get("payment_mode") == vic_rcti.PAYMENT:
            yield from self._vic_estimates(terms, facts)
            return
        nsw = terms.get("payment_mode") == NSW_CLAIMS_ONLY
        drafts = self._drafts_for_target(all_targets=True)
        reserved = {(line["component_uid"], line["academic_period"])
                    for draft in drafts if draft["uid"] != excluding_draft for line in draft["lines"]}
        next_start = min((
            start for start in authority_version_starts(self.db, self.target[1], self.pilot.simulation)
            if start > terms["start"]
        ), default="") if self.target[0] == "authority" else ""
        today = datetime.now(ZoneInfo("Australia/Melbourne")).date().isoformat()
        for student in self._components():
            component = student["component_uid"]
            base = {"student_id":student["student_id"],"student_name":student["student_name"],
                    "component_uid":component,"course":student.get("course_name","")}
            if student.get("school_name"):
                base.update(school_id=student["school_id"], school_name=student["school_name"])
            if student.get("assignment_reason"):
                yield {**base, "reason": student["assignment_reason"]}
                continue
            if self.target[0] == "provider" and (
                not student["accepted"] or student["component_status"] != "active"
                or student["submission_channel"] != "Direct"
            ):
                yield {**base, "reason":"缺少有效录取或入学项目，或经主代理递交。"}
                continue
            commencement = self.db.execute(
                """SELECT uid,actual_commencement_date FROM revenue_commencement_facts
                    WHERE enrolment_component_id=? AND status='confirmed'""", (student["component_id"],),
            ).fetchone()
            if not commencement or not terms["start"] <= commencement["actual_commencement_date"] <= min(today,terms["end"]):
                yield {**base, "reason":"缺少已确认的实际开学日，或开学日不在合同有效期内。"}
                continue
            if next_start and commencement["actual_commencement_date"] >= next_start:
                yield {**base, "reason":"实际开学日适用该教育局的新版合同。"}
                continue
            previous = self.db.execute(
                """SELECT 1 FROM revenue_commission_lines l
                    JOIN revenue_commission_instalments i ON i.commission_line_id=l.id
                    JOIN revenue_claim_allocations a ON a.commission_instalment_id=i.id
                   WHERE l.enrolment_component_id=?""", (student["component_id"],),
            ).fetchone()
            if previous:
                yield {**base, "reason":"已有佣金申领记录，请核对历史单据。"}
                continue
            fact = facts.get(component)
            if nsw and not fact:
                yield {**base, "reason": "请先核对申领条件和凭证。", "needs_evidence": True}
                continue
            period_mode = fact["base_period"] if nsw else terms["period"]
            tuition_rows = [
                row for row in self.db.execute(
                    """SELECT t.id,t.uid,t.academic_period,t.tuition_cents,
                              COALESCE((SELECT SUM(a.amount_cents) FROM revenue_tuition_adjustments a
                                         WHERE a.tuition_fact_id=t.id AND a.commission_effect='reduces_base'),0) AS reduction
                         FROM revenue_tuition_facts t
                        WHERE t.enrolment_component_id=? AND t.status='active'
                          AND t.currency_code='AUD' AND t.authority_level IN ('official','manual')
                          AND t.evidence_document_id IS NOT NULL
                          AND t.fee_component IN ('tuition','tuition_local','tuition_international')
                        ORDER BY t.academic_period,t.id""", (student["component_id"],),
                ) if self._period_matches(period_mode, row["academic_period"])
            ]
            groups = {}
            for tuition in tuition_rows:
                period = tuition["academic_period"].casefold().strip() if period_mode == "each_year" else period_mode
                groups.setdefault(period,[]).append(tuition)
            if not groups:
                yield {**base, "reason": f'缺少有凭证的已确认 AUD {FEE_PERIODS[period_mode]}。',
                       **({"needs_evidence": True} if nsw else {})}
                continue
            for period, rows in groups.items():
                row = {**base,"academic_period":period}
                prior_period = [p for c,p in reserved if c==component]
                if period in prior_period or (prior_period and (
                    terms["period"] != "each_year" or any(p in FEE_PERIODS for p in prior_period)
                )):
                    yield {**row,"reason":"已有发票草稿，避免重复开票。"}
                    continue
                if len(rows) != 1:
                    yield {**row,"reason":"同一计佣周期有多份学费记录，请先核对。"}
                    continue
                if not fact:
                    yield {**row,"reason":"请先核对申领条件和凭证。","needs_evidence":True}
                    continue
                tuition = rows[0]
                base_cents = tuition["tuition_cents"] - tuition["reduction"]
                if base_cents <= 0:
                    yield {**row,"reason":"扣除已确认减免后，计佣学费为零。"}
                    continue
                points = terms["rates"][fact["tier"]]
                standard_cents = apply_basis_points(base_cents, points)
                iec_cents = apply_basis_points(fact["iec_tuition_cents"], terms["rates"]["iec_rate"]) if nsw else 0
                amounts = commission_gst_amounts(standard_cents + iec_cents, terms["gst"])
                if nsw:
                    row.update(standard_cents=standard_cents, iec_cents=iec_cents,
                               iec_tuition_cents=fact["iec_tuition_cents"])
                if amounts["gross_cents"] <= 0:
                    yield {**row,"reason":"计算后佣金金额为零。"}
                    continue
                row.update({"uid":f'{component}:{period}',"tier":fact["tier"],"rate_basis_points":points,
                            "tuition_cents":base_cents,"evidence":fact["reference"],**amounts})
                row["revision"] = _digest([terms["revision"],dict(commencement),dict(tuition),row])
                yield row

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

    def create_invoice_draft(self, selection, revision, terms, key, *, customer_note=""):
        if terms.get("payment_mode") == NSW_CLAIMS_ONLY:
            raise ValueError(NSW_BLOCK_INVOICE + " 发票功能未开放。")
        if terms.get("payment_mode") == vic_rcti.PAYMENT:
            raise PermissionError(vic_rcti.BLOCK_INVOICE)
        if isinstance(selection,str):
            selection = [selection]
        if not selection or len(selection) > 50 or len(set(selection)) != len(selection):
            raise ValueError("请勾选 1 至 50 条不同的可申领记录。")
        from revenue.invoice_templates import invoice_customer_note

        customer_note = invoice_customer_note(customer_note)
        ids = sorted(selection)
        payload = {"contract_draft_uid":self.uid,"terms":terms["revision"],"selection":ids,"revision":revision}
        if customer_note:
            payload["customer_note"] = customer_note

        def operation():
            candidates = {row["uid"]:row for row in self.candidates(terms) if row.get("uid")}
            if not all(uid in candidates for uid in ids):
                raise ValueError("所选记录已生成草稿或资料已变更，请刷新后复核。")
            rows = [candidates[uid] for uid in ids]
            if _digest(sorted(row["revision"] for row in candidates.values())) != revision:
                raise ValueError("学生资料或佣金金额已变更，请刷新后复核。")
            return {"uid":new_uid("commission-draft"),"target":list(self.target),
                    "contract_draft_uid":self.uid,"terms_revision":terms["revision"],
                    "simulation":self.pilot.simulation,"payer":terms["payer"],"payment_days":terms["payment_days"],
                    "created_on":datetime.now(ZoneInfo("Australia/Melbourne")).date().isoformat(),
                    "lines":rows, **({"customer_note": customer_note} if customer_note else {}),
                    **{field:sum(row[field] for row in rows) for field in ("net_cents","gst_cents","gross_cents")}}
        return self.repo.command(
            idempotency_key=key, command_type=DRAFT, aggregate_type="institution_commission_invoice_draft",
            payload=payload, actor_user_id=self.actor, operation=operation,
        )

    def render_pdf(self, invoice):
        from revenue.invoice_templates import _bill_to_layout, _version, default_layout, invoice_layout_selection, invoice_template_context
        from revenue.invoice_billing_details import bill_to_fields
        from revenue.pdf import render_configured_commission_invoice

        data = {
            "bill_to":invoice["payer"],"document_number":"DRAFT",
            "issue_date":invoice["created_on"],"due_date":None,
            "draft_notice":True,"test_only":self.pilot.simulation,
            "gst_mode":invoice.get("gst_mode") or (self.terms() or {}).get("gst"),
            "payment_terms":f'Within {invoice["payment_days"]} days of receipt of a valid invoice',
            **{field:invoice[field] for field in ("net_cents","gst_cents","gross_cents")},
            "lines":[{"description":" | ".join(filter(None, (
                         row["student_name"], row["student_id"], row.get("school_name"), row["academic_period"],
                     ))),
                      **{field:row[field] for field in ("net_cents","gst_cents","gross_cents")}}
                     for row in invoice["lines"]],
        }
        context = invoice_template_context(self.pilot, self.target)
        layout = default_layout()
        if context["library_entity_id"]:
            uid, source = invoice_layout_selection(
                self.db, formal=True, entity_id=context["library_entity_id"],
                simulation=self.pilot.simulation, **context,
            )
            if uid:
                layout = _version(self.db, uid, source)["value"]
            if self.target[0] in {"school", "provider"}:
                payer = self._billing_payer()
            else:
                from revenue.authority_formal import authority_payer

                payer = authority_payer(self.db, self.target[1])
            if payer:
                data.update(bill_to_fields(self.db, payer["id"], self.pilot.simulation))
            layout = _bill_to_layout(layout, data)
        return render_configured_commission_invoice(data,layout)
