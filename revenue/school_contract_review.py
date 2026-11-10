"""Reviewed PDF evidence and supported executable contract terms."""
import json
import os
import re
from pathlib import Path

from revenue.repository import new_uid
from revenue.school_commission_pilot import FORMAL_CONTRACT
from revenue.school_contract_extract import CONFIG_CONDITIONS, extract_configurable_terms, extract_terms, normalize_value, read_pages

ANALYSED = "toorak_contract_analyzed"
REVIEWED = "toorak_contract_analysis_reviewed"


def _latest(connection, draft_uid, action):
    row = connection.execute(
        """SELECT after_json, actor_user_id, occurred_at FROM revenue_audit_events
           WHERE aggregate_uid=? AND action=? ORDER BY id DESC LIMIT 1""",
        (draft_uid, action),
    ).fetchone()
    return {**json.loads(row[0]), "actor": row[1], "recorded_at": row[2]} if row else None


def review_state(connection, draft_uid):
    return {
        "analysis": _latest(connection, draft_uid, ANALYSED),
        "review": _latest(connection, draft_uid, REVIEWED),
    }


def _draft(repository, pilot, uid):
    draft = repository.require_by_uid("revenue_rule_snapshots", uid)
    config = json.loads(draft["canonical_json"])
    document = repository.require_by_uid("revenue_documents", draft["source_rule_uid"])
    connection = repository.connection
    if (
        not pilot or not pilot.enabled or draft["rule_kind"] != "commission"
        or not config.get("toorak_draft")
        or (not pilot.simulation and not pilot.toorak_draft_release)
        or (config.get("school_commission_configurable") and
            config.get("analysis_profile") not in ({"single", "toorak"} if pilot.simulation else {"toorak"}))
        or config.get("signed_confirmed") is not True
        or config.get("school_id") != pilot.school_id
        or config.get("pilot_entity_id") != pilot.entity_id
        or config.get("pilot_simulation") is not pilot.simulation
        or document["document_kind"] != FORMAL_CONTRACT
        or document["mime_type"] != "application/pdf"
        or connection.execute("SELECT 1 FROM revenue_contract_versions WHERE document_id=?",
                              (document["id"],)).fetchone()
    ):
        raise ValueError("请选择本校仍待确认的签署合同草稿。")
    return document, config


def analyze_draft(repository, pilot, uid, *, ocr, runtime_root):
    from revenue.data_invoice_storage import SUFFIX
    from revenue.school_contract_storage import configured_contract_store
    from revenue.secure_storage import StorageUnavailable, read_secure

    doc, config = _draft(repository, pilot, uid)
    try:
        path = Path(doc["storage_path"])
        if path.suffix == SUFFIX:
            pdf = configured_contract_store().read(path, doc["sha256"], doc["size_bytes"])
        elif pilot.simulation:
            configured = os.environ.get("REVENUE_SECURE_STORAGE_ROOT")
            if not configured:
                raise StorageUnavailable()
            root = Path(configured)
            if not root.is_relative_to(runtime_root):
                raise StorageUnavailable()
            pdf = read_secure(root, path, doc["sha256"], doc["size_bytes"])
        else:
            raise StorageUnavailable()
        pages, status = read_pages(pdf, ocr)
    except (OSError, StorageUnavailable):
        pages, status = [], "unreadable"
    profile = config.get("analysis_profile", "toorak")
    fields = extract_configurable_terms(pages) if profile == "single" else extract_terms(pages)
    if pilot.entity_id == 0:
        from revenue.school_contract_extract import add_onboarding_findings

        school = repository.connection.execute(
            "SELECT school_name FROM schools WHERE id=? AND deleted_at IS NULL", (pilot.school_id,),
        ).fetchone()
        if not school:
            raise ValueError("请选择仍有效的学校。")
        add_onboarding_findings(fields, pages, school[0])
    return {"sha256": doc["sha256"], "document_uid": doc["uid"], "status": status, "fields": fields}


def save_analysis(repository, pilot, uid, result, actor, key):
    def operation():
        document, _ = _draft(repository, pilot, uid)
        if document["sha256"] != result["sha256"] or document["uid"] != result["document_uid"]:
            raise ValueError("原件已变更，请重新分析。")
        return {"uid": uid, "run": new_uid("analysis"), **result}

    return repository.command(idempotency_key=key, command_type=ANALYSED,
                              aggregate_type="school_contract_draft", payload={"draft": uid},
                              actor_user_id=actor, operation=operation)


def save_review(repository, pilot, uid, run, decisions, actor, key):
    def operation():
        _draft(repository, pilot, uid)
        analysis = _latest(repository.connection, uid, ANALYSED)
        if not analysis or analysis["run"] != run:
            raise ValueError("分析结果已更新，请刷新后重新确认。")
        fields = {}
        for name in analysis["fields"]:
            decision, value = decisions.get(name, ("", ""))
            suggested = analysis["fields"][name]["value"]
            if decision == "input" and pilot.entity_id == 0:
                decision = ("pending" if not value.strip() else "accept" if value == suggested else "edit")
            if decision == "accept":
                if not suggested:
                    raise ValueError("待确认条款没有建议值，请修改或保留待确认。")
                value = suggested
            elif decision == "pending":
                value = ""
            elif decision != "edit":
                raise ValueError("请为每项条款选择确认方式。")
            fields[name] = {"decision": decision, "value": normalize_value(name, value) if value else "",
                            "sources": analysis["fields"][name]["sources"]}
            if decision != "pending" and not fields[name]["value"]:
                raise ValueError("请填写修订后的确认值。")
        start, end = (fields[name]["value"] for name in ("start", "end"))
        if (fields["end"]["decision"] == "accept" and analysis["fields"]["end"]["derived"] and any(
            fields[name]["value"] != analysis["fields"][name]["value"] for name in ("start", "term_years")
        )):
            raise ValueError("生效日或期限已修改，请重新核对并手填到期日，或将到期日保留待确认。")
        if start and end and start > end:
            raise ValueError("到期日不得早于生效日。")
        return {"uid": uid, "run": run, "fields": fields}

    return repository.command(idempotency_key=key, command_type=REVIEWED,
                              aggregate_type="school_contract_draft",
                              payload={"draft": uid, "run": run, "decisions": decisions},
                              actor_user_id=actor, operation=operation)


# These are the conditions implemented by the existing Toorak calculator and Claim service.
# Require the complete clauses, so changed or additional conditions cannot silently become the old rule.
_TOORAK_CONDITIONS = {
    "local_scope": r"\d+(?:\.\d+)?% of the first year's local tuition fees for students enrolled in toorak college on a subclass 188 visa or other temporary or permanent resident visa\.",
    "international_scope": r"\d+(?:\.\d+)?% of the first year's international tuition fees for students enrolled in toorak college that are on a full student visa\.",
    "conditions": r"subject to the provisions of this clause toorak college must pay the agent's fee for each student who: i\. is recruited by the agent; and ii\. is enrolled in a course at toorak college; and iii\. has paid the course fee to toorak college; and iv\. has commenced the course at toorak college\.",
    "agency_application": r"for the purposes of this agreement, the agent is regarded as having recruited the student under this agreement if the agent submits the student's application for enrolment and that application also bears the agent's name\.",
    "direct_exclusion": r"an agent's fee is not paid where a prospective student applies directly to toorak college\.",
    "invoice_condition": r"no agent's fee is payable unless the agent has submitted an invoice in a form approved by toorak college\.",
}


def confirmed_rule_inputs(profile, fields):
    """Only translate clauses with a corresponding calculation and eligibility rule."""
    common = {"start", "end", "gst", "payment_days", "payment_trigger", "frequency"}
    required = common | ({"rate", "conditions", "commencement_days", "registration_required"} if profile == "single"
                         else set(_TOORAK_CONDITIONS) | {"local_rate", "international_rate"})
    if not all(fields.get(name, {}).get("value") for name in required):
        raise ValueError("请先确认所有启用所需的佣金、合同日期、GST 和申领条款。")
    values = {name: field["value"] for name, field in fields.items()}
    if "school_name" in values and not values["school_name"]:
        raise ValueError("请核对合同上的学校名称及付款主体。")
    if "tuition_period" in values and values["tuition_period"] != "first_year":
        raise ValueError("已保存计佣周期。目前可启用首年一次性计佣，首半年及每年计佣暂待计算支持。")
    if (values["payment_trigger"] != "收到有效发票" or values["payment_days"] not in {"0", "7", "15", "30"}
            or values["frequency"] != "一次性，仅按学费"):
        raise ValueError("合同付款起算事件、付款期限或佣金周期超出当前支持范围。")
    if profile == "single":
        conditions = CONFIG_CONDITIONS.fullmatch(values["conditions"])
        if (not conditions or normalize_value("commencement_days", conditions[1]) != values["commencement_days"]
                or values["registration_required"] != "1"):
            raise ValueError("申领条件与已确认条款不一致，或超出当前支持范围。")
        tiers = [{"rate_percent": values["rate"], "tuition_kind": "any", "visa_subclasses": []}]
        days, registration = values["commencement_days"], values["registration_required"]
        rate_field = "rate"
    elif profile == "toorak":
        for name, pattern in _TOORAK_CONDITIONS.items():
            if not re.fullmatch(pattern, values[name].replace("’", "'").casefold()):
                raise ValueError("Toorak 签证或申领条件超出当前支持范围，请核对原文。")
        # Source wording is separate from the operational rate, so an edited rate
        # is visibly recorded without losing the English original.
        tiers = [
            {"rate_percent": values["local_rate"], "tuition_kind": "local", "visa_subclasses": ["188"]},
            {"rate_percent": values["international_rate"], "tuition_kind": "international", "visa_subclasses": ["500"]},
        ]
        days, registration = "0", "1"
        rate_field = "local_rate"
    else:
        raise ValueError("当前合同条款格式不支持直接启用。")
    source = fields[rate_field]["sources"]
    if not source:
        raise ValueError("启用规则前须能定位 PDF 原文中的佣金条款。")
    return {
        "effective_from": values["start"], "effective_to": values["end"], "gst_mode": values["gst"],
        "payment_days": values["payment_days"], "commencement_days": days, "registration_required": registration,
        "clause_reference": "、".join(f'第 {p["page"]} 页 {p["clause"]}' for p in source),
        "tiers": tiers,
        **{key: values[key] for key in ("school_name", "invoice_email", "tuition_period") if key in values},
    }
