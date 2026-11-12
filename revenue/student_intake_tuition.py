"""Capture document-confirmed school intake tuition and project the chosen source."""

from hashlib import sha256
from html import escape
from urllib.parse import quote
import json
import re

from revenue.first_year_tuition import COE_KIND, OFFER_KIND, first_year_records
from revenue.money import aud_cents
from revenue.operational_visibility import RevenueOperationalVisibility
from revenue.repository import RevenueRepository
from revenue.services import RevenueService

SOURCE_EVENT = "student_intake_tuition_source_saved"
LINK_EVENT = "student_intake_tuition_linked"
CORRECTION_EVENT = "first_year_tuition_amount_corrected"
OFFER_TYPES = {"Offer", "SignedOffer", "OfferAcceptance"}
COE_TYPES = {"COE", "COE_CAAW"}


def review_source(module, document_type):
    if module == "Offer" and document_type in OFFER_TYPES:
        return "offer"
    if module == "COE + OSHC" and document_type in COE_TYPES:
        return "coe"
    return None


def read_review_fields(form, index):
    def value(field):
        raw = form.get(f"intake_{field}_{index}", "")
        if not isinstance(raw, str):
            raise ValueError("入学资料字段重复，请重新核对。")
        return raw.strip()

    year, grade, amount = (value(field) for field in ("year", "grade", "aud"))
    if not any((year, grade, amount)):
        return None
    if not re.fullmatch(r"(?:19|20|21)\d{2}", year):
        raise ValueError("请填写四位入学年份，例如 2027。")
    match = re.fullmatch(r"(?:Year\s*)?(1[0-2]|[1-9])", grade, re.IGNORECASE)
    if match:
        grade = f"Year {match[1]}"
    elif grade.casefold() in {"foundation", "prep", "kindergarten", "reception", "transition", "pre-primary"}:
        grade = grade.casefold().title()
    else:
        raise ValueError("请填写入学年级，例如 Year 9 或 Foundation。")
    cents = aud_cents(amount)
    if cents <= 0:
        raise ValueError("入学当年学费须大于 AUD 0。")
    return {"year": int(year), "grade": grade, "cents": cents}


def source_record(connection, kind, record_id, student_id):
    row = connection.execute(
        """SELECT after_json FROM revenue_audit_events
            WHERE action=? AND aggregate_type=? AND aggregate_uid=?
              AND json_extract(after_json,'$.student_id')=? ORDER BY id DESC LIMIT 1""",
        (SOURCE_EVENT, f"{kind}_intake_tuition", str(record_id), student_id),
    ).fetchone()
    return json.loads(row[0]) if row else None


def save_source(connection, kind, record_id, student_id, school_id, values, staged, actor,
                *, acceptance_id=None, decision=None, source_school=""):
    repository = RevenueRepository(connection)
    document_kind = OFFER_KIND if kind == "offer" else COE_KIND
    key = f"student-intake:{kind}:{record_id}"

    def save():
        row = connection.execute(
            """SELECT id FROM revenue_documents
                WHERE document_kind=? AND storage_path=? AND sha256=? AND size_bytes=?""",
            (document_kind, str(staged.path), staged.sha256, staged.size_bytes),
        ).fetchone()
        document_id = row[0] if row else RevenueService(repository).register_document(
            document_kind, str(staged.path), staged.sha256, staged.mime_type, staged.size_bytes,
            "imported", actor, key + ":document",
        )["id"]
        return {**payload, "uid":str(record_id), "document_id":document_id}

    payload = {
        "kind":kind, "student_id":student_id, "school_id":school_id, "source_school":source_school,
        "values":values, "sha256":staged.sha256, "filename":staged.original_filename,
        "acceptance_id":acceptance_id, "decision":decision,
    }
    return repository.command(
        idempotency_key=key, command_type=SOURCE_EVENT, aggregate_type=f"{kind}_intake_tuition",
        payload=payload, actor_user_id=actor, operation=save,
    )


def accepted_offer_context(connection, student_id, *, acceptance_id=None):
    required = {row[0] for row in connection.execute(
        """SELECT name FROM sqlite_master WHERE type='table'
             AND name IN ('revenue_audit_events','cycle_final_acceptances','revenue_enrolment_components')"""
    )}
    if len(required) != 3:
        return None
    offer_columns = {row[1] for row in connection.execute("PRAGMA table_info(offer_received_events)")}
    if not {"legacy_offer_id", "school_id", "student_id"} <= offer_columns:
        return None
    rows = connection.execute(
        """SELECT a.id AS acceptance_id, a.student_id, a.school_id, s.school_name,
                  e.legacy_offer_id, ec.id AS component_id, ec.uid AS component_uid,
                  c.uid AS case_uid, c.application_cycle_id
             FROM cycle_final_acceptances a
             JOIN application_cycles x ON x.id=a.application_cycle_id AND x.student_id=a.student_id
             JOIN offer_received_events e ON e.id=a.offer_received_event_id
                  AND e.student_id=a.student_id AND e.application_cycle_id=x.id AND e.school_id=a.school_id
             JOIN offers o ON o.id=e.legacy_offer_id AND o.student_id=a.student_id AND o.school_id=a.school_id
             JOIN schools s ON s.id=a.school_id
             JOIN revenue_commercial_cases c ON c.application_cycle_id=x.id AND c.student_id=a.student_id
             JOIN revenue_enrolment_components ec ON ec.commercial_case_id=c.id AND ec.final_acceptance_id=a.id
            WHERE a.student_id=? AND a.acceptance_status='active' AND x.status='active'
              AND x.education_stream='school_education' AND ec.status='active' AND ec.component_kind='school'
              AND ec.source_kind='final_acceptance' AND (? IS NULL OR a.id=?)""",
        (student_id,acceptance_id,acceptance_id),
    ).fetchall()
    if len(rows) != 1:
        return None
    context = dict(rows[0])
    offer = source_record(connection, "offer", context["legacy_offer_id"], student_id)
    if offer and (
        offer["school_id"] == context["school_id"] or (
            offer["school_id"] is None and _school_key(offer["source_school"]) == _school_key(context["school_name"])
        )
    ):
        context["offer"] = offer
    else:
        context["offer"] = None
    return context


def _school_key(name):
    return re.sub(r"[\W_]+", "", str(name or "").casefold())


def choose_coe(context, provider, acceptance_id, values, choice):
    if (not context or str(acceptance_id) != str(context["acceptance_id"])
            or not _school_key(provider) or _school_key(provider) != _school_key(context["school_name"])):
        raise ValueError("请核对 CoE 的学校与同一次入学的已接受 Offer 后再确认学费。")
    if choice not in {"", "offer", "coe"}:
        raise ValueError("请选择有效的学费来源。")
    offer = context["offer"]
    if not offer:
        if choice == "offer":
            raise ValueError("当前 Offer 尚无已确认的学费。")
        return "coe"
    if values == offer["values"]:
        return "offer"
    if not choice:
        raise ValueError("CoE 与 Offer 的年份、年级或学费有差异，请选择保留 Offer 或采用 CoE。")
    return choice


def validate_tuition_update(connection, context, values):
    rows = first_year_records(connection, context["component_id"])
    if len(rows) > 1 or (rows and rows[0]["currency_code"] != "AUD"):
        raise ValueError("该入学已有多笔或非 AUD 首年学费，请先核对。")
    if rows and rows[0]["tuition_cents"] != values["cents"] and connection.execute(
        "SELECT 1 FROM revenue_tuition_adjustments WHERE tuition_fact_id=?", (rows[0]["id"],),
    ).fetchone():
        raise ValueError("该学费已有减免或调整，请先核对再更改金额。")


def sync_tuition(connection, context, source, actor, *, keep_existing=False, coe_id=None):
    repository = RevenueRepository(connection)
    component_id = context["component_id"]
    values = source["values"]
    key = f"intake-tuition-link:{context['acceptance_id']}:{source['kind']}:{source['uid']}"
    if coe_id is not None:
        key += f":coe:{coe_id}"
    payload = {
        "component_uid": context["component_uid"],
        "values": values,
        "evidence_document_id": source["document_id"],
        "source_kind": source["kind"],
        "source_record_id": source["uid"],
    }

    def save():
        prior_rows = first_year_records(connection, component_id)
        if len(prior_rows) > 1 or (prior_rows and prior_rows[0]["currency_code"] != "AUD"):
            if keep_existing:
                return {**payload, "uid":prior_rows[0]["uid"], "preserved":True}
            raise ValueError("该入学已有多笔或非 AUD 首年学费，请先核对。")
        prior = prior_rows[0] if prior_rows else None
        source_label = f"{'Offer' if source['kind']=='offer' else 'CoE'} #{source['uid']} · {source['filename']}"
        if prior and prior["evidence_document_id"] == source["document_id"] and prior["tuition_cents"] == values["cents"]:
            fact = repository.require_by_uid("revenue_tuition_facts", prior["uid"])
        elif prior and keep_existing and prior["evidence_document_id"]:
            return {**payload, "uid":prior["uid"], "preserved":True}
        elif prior and prior["tuition_cents"] == values["cents"]:
            connection.execute(
                """UPDATE revenue_tuition_facts SET evidence_document_id=?,authority_level='official',
                          confirmed_by=? WHERE id=?""", (source["document_id"],actor,prior["id"]),
            )
            fact = repository.require_by_uid("revenue_tuition_facts", prior["uid"])
        else:
            if prior and connection.execute(
                "SELECT 1 FROM revenue_tuition_adjustments WHERE tuition_fact_id=?", (prior["id"],),
            ).fetchone():
                raise ValueError("该学费已有减免或调整，请先核对再更改金额。")
            fact = RevenueService(repository).record_tuition(
                context["component_uid"], prior["academic_period"] if prior else "year_1",
                prior["fee_component"] if prior else "tuition", values["cents"], "official",
                actor, key + ":tuition", source["document_id"],
            )
        return {**fact, "intake_year":values["year"], "intake_grade":values["grade"], "source_label":source_label}

    return repository.command(
        idempotency_key=key, command_type=LINK_EVENT, aggregate_type="tuition_fact",
        payload=payload, actor_user_id=actor, operation=save,
    )


def link_offer_tuition(connection, acceptance_id, actor):
    if not connection.execute("SELECT 1 FROM revenue_audit_events WHERE action=? LIMIT 1", (SOURCE_EVENT,)).fetchone():
        return None
    row = connection.execute(
        "SELECT student_id FROM cycle_final_acceptances WHERE id=?", (acceptance_id,),
    ).fetchone()
    if not row:
        return None
    context = accepted_offer_context(connection, row[0], acceptance_id=acceptance_id)
    if context and context["offer"]:
        return sync_tuition(connection, context, context["offer"], actor, keep_existing=True)
    return None


def _tuition_revision(context, record):
    payload = [context["acceptance_id"], record["uid"], record["tuition_cents"],
               record["evidence_document_id"], record["authority_level"],
               record["intake_year"], record["intake_grade"]]
    return sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def _current_first_year(connection, student_id):
    context = accepted_offer_context(connection, student_id)
    if not context or not RevenueOperationalVisibility(connection).is_student_visible(student_id):
        return None
    rows = first_year_records(connection, context["component_id"])
    if len(rows) != 1:
        return None
    record = rows[0]
    if (record["currency_code"] != "AUD" or record["authority_level"] not in {"official", "manual"}
            or not record["evidence_document_id"]):
        return None
    return context, record


def student_tuition_details(connection, student_id):
    current = _current_first_year(connection, student_id)
    if current is None:
        return None
    context, record = current
    history = [
        {**json.loads(row[0]), "actor_user_id":row[1], "occurred_at":row[2]}
        for row in connection.execute(
            """SELECT after_json,actor_user_id,occurred_at FROM revenue_audit_events
                WHERE action=? AND json_extract(after_json,'$.component_uid')=?
                ORDER BY id DESC""", (CORRECTION_EVENT, context["component_uid"]),
        )
    ]
    return {"context":context, "record":record, "revision":_tuition_revision(context, record), "history":history}


def correct_first_year_tuition(connection, student_id, form, actor):
    cents = aud_cents(form.get("tuition_aud"))
    if cents <= 0:
        raise ValueError("首年学费须大于 AUD 0。")
    token, revision = form.get("idempotency_key"), form.get("revision")
    if (not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", token)
            or not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision)):
        raise ValueError("请重新打开学生学费表单。")
    repository = RevenueRepository(connection)
    key = f"student-intake-amount:{student_id}:{token}"

    def save():
        current = _current_first_year(connection, student_id)
        if current is None:
            raise ValueError("该学生当前没有可修改的已确认首年学费。")
        context, prior = current
        if _tuition_revision(context, prior) != revision:
            raise ValueError("当前入学或学费已变更，请重新打开表单核对。")
        if cents == prior["tuition_cents"]:
            raise ValueError("首年学费金额没有变化。")
        validate_tuition_update(connection, context, {"cents":cents})
        fact = RevenueService(repository).record_tuition(
            context["component_uid"], prior["academic_period"], prior["fee_component"], cents,
            prior["authority_level"], actor, key + ":tuition", prior["evidence_document_id"],
        )
        return {**fact, "component_uid":context["component_uid"], "before_cents":prior["tuition_cents"],
                "after_cents":cents, "intake_year":prior["intake_year"], "intake_grade":prior["intake_grade"],
                "source_label":prior["source_label"]}

    return repository.command(
        idempotency_key=key, command_type=CORRECTION_EVENT, aggregate_type="tuition_fact",
        payload={"student_id":student_id,"revision":revision,"tuition_cents":cents},
        actor_user_id=actor, operation=save,
    )


def render_student_tuition(details, student_id, csrf, token, *, show_original):
    from revenue.read_models import melbourne_datetime_display

    def amount(cents):
        return f"AUD {cents//100:,}.{cents%100:02d}"

    record = details["record"]
    year_grade = " · ".join(str(value) for value in (record["intake_year"],record["intake_grade"]) if value)
    source = ""
    if show_original and record["document_uid"]:
        href = "/revenue/documents/" + quote(record["document_uid"], safe="") + "/view"
        source = f' · <a href="{href}" target="_blank" rel="noopener">查看学费原件</a>'
    changes = "".join(
        f'<li>{amount(row["before_cents"])} → {amount(row["after_cents"])} · '
        f'{melbourne_datetime_display(row["occurred_at"])} · 操作人 #{int(row["actor_user_id"])}</li>'
        for row in details["history"]
    )
    history = f'<details><summary>修改记录</summary><ul>{changes}</ul></details>' if changes else ""
    cents = record["tuition_cents"]
    action = f'/students/{quote(student_id,safe="")}/offer/first-year-tuition'
    return f"""<section id="student-intake-tuition">
      <h3>入学与学费</h3>
      <p>{escape(details["context"]["school_name"])}{f' · {escape(year_grade)}' if year_grade else ''}</p>
      <p>首年学费 <strong>{amount(cents)}</strong>{source}</p>
      <details><summary class="secondary">修改首年学费</summary>
        <form method="post" action="{action}" class="stack-form">
          <input type="hidden" name="csrf" value="{escape(csrf,quote=True)}">
          <input type="hidden" name="revision" value="{details["revision"]}">
          <input type="hidden" name="idempotency_key" value="{escape(token,quote=True)}">
          <label>首年学费（AUD）<input name="tuition_aud" inputmode="decimal" maxlength="12"
                 value="{cents//100}.{cents%100:02d}" required></label>
          <button type="submit" class="primary">保存首年学费</button>
        </form>
      </details>
      {history}
    </section>"""


def render_review_fields(index, module, document_type, context=None):
    kind = review_source(module, document_type)
    offer = (context or {}).get("offer") if module == "COE + OSHC" else None
    values = offer["values"] if offer else {}
    year, grade, cents = values.get("year",""), values.get("grade",""), values.get("cents")
    amount = f"{cents//100}.{cents%100:02d}" if cents is not None else ""
    offer_reference = ""
    if offer:
        offer_reference = (
            f'<p>已接受 Offer #{int(offer["uid"])} · {escape(context["school_name"])} · '
            f'{year} · {escape(grade)} · AUD {cents//100:,}.{cents%100:02d}</p>'
        )
    elif module == "COE + OSHC":
        offer_reference = "<p>当前录取暂无可带出的 Offer 学费。请按 CoE 或学校收费单核对入学当年金额。</p>"
    decision = ""
    if offer:
        decision = f'''<label data-intake-choice hidden>与 Offer 不一致时
          <select name="intake_choice_{index}">
            <option value="">请选择</option><option value="offer">保留 Offer</option><option value="coe">采用 CoE</option>
          </select></label>'''
    hidden = "" if kind else " hidden disabled"
    return f'''<fieldset data-student-intake-tuition data-module="{escape(module,quote=True)}"
      data-offer-year="{year}" data-offer-grade="{escape(grade,quote=True)}" data-offer-amount="{amount}"{hidden}>
      <legend>入学与学费</legend>{offer_reference}
      <input type="hidden" name="intake_acceptance_{index}" value="{(context or {}).get('acceptance_id','')}">
      <label>入学年份<input name="intake_year_{index}" inputmode="numeric" maxlength="4" value="{year}" placeholder="2027"></label>
      <label>入学年级<input name="intake_grade_{index}" maxlength="24" value="{escape(grade,quote=True)}" placeholder="Year 9"></label>
      <label>入学当年学费（AUD）<input name="intake_aud_{index}" inputmode="decimal" maxlength="12" value="{amount}" placeholder="25000.00"></label>
      {decision}
    </fieldset>'''
