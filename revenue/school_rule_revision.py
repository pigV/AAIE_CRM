"""Append-only corrections of activated school rule packages."""

import json

from revenue.institution_commission_draft import GST_MODES, _rate, _value
from revenue.school_commission_pilot import FORMAL_CONTRACT
from revenue.school_contract_extract import normalize_value
from revenue.services import RevenueService

PERIODS = {"first_year": "首年学费", "first_half_year": "首半年学费", "whole_course": "全课程学费"}

RULES = """SELECT r.uid, r.version_no, r.effective_from, r.effective_to, v.uid AS contract_uid, d.uid AS document_uid,
                  s.canonical_json
             FROM revenue_commission_rule_versions r
             JOIN revenue_contract_versions v ON v.id=r.contract_version_id
             JOIN revenue_contract_groups g ON g.id=v.contract_group_id
             JOIN revenue_documents d ON d.id=v.document_id
             JOIN revenue_rule_snapshots s ON s.id=r.rule_snapshot_id
            WHERE g.revenue_entity_id=? AND g.contract_kind='direct_commission'
              AND v.status='active' AND r.status='active' AND d.document_kind=?
              AND json_extract(s.canonical_json,'$.school_id')=?
              AND json_extract(s.canonical_json,'$.pilot_simulation')=?
              AND r.version_no=(SELECT max(latest.version_no) FROM revenue_commission_rule_versions latest
                                 WHERE latest.contract_version_id=v.id AND latest.status='active')"""


def school_rules(connection, scope):
    return connection.execute(RULES, (scope.entity_id, FORMAL_CONTRACT, scope.school_id, scope.simulation)).fetchall()


def revise_school_rule(repository, scope, document_uid, form, actor, key):
    def operation():
        rows = school_rules(repository.connection, scope)
        current = next((row for row in rows if row["document_uid"] == document_uid), None)
        if not current or current["uid"] != _value(form, "revision"):
            raise ValueError("合同条款已变更，请刷新后再编辑。")
        start, end = (normalize_value(name, _value(form, name)) for name in ("start", "end"))
        if start > end:
            raise ValueError("合同到期日不得早于生效日。")
        if any(row["document_uid"] != document_uid and start <= (row["effective_to"] or "9999-12-31")
               and end >= row["effective_from"] for row in rows):
            raise ValueError("合同有效期与该校其他合同重叠。")
        config = json.loads(current["canonical_json"])
        gst, period = _value(form, "gst"), _value(form, "period")
        days, minimum = _value(form, "payment_days"), _value(form, "commencement_days")
        registration = _value(form, "registration_required")
        if (gst not in GST_MODES or period not in PERIODS or days not in {"0", "7", "15", "30"}
                or not minimum.isascii() or not minimum.isdecimal() or len(minimum)>3 or int(minimum)>365
                or registration not in {"0", "1"}):
            raise ValueError("请核对计佣周期、GST、开学天数及付款期限。")
        if config.get("tiers"):
            for index, tier in enumerate(config["tiers"]):
                tier["rate_basis_points"] = _rate(_value(form, f"rate_{index}"))
        else:
            for index, rate in enumerate(config["instalments"]):
                rate["rate_basis_points"] = _rate(_value(form, f"rate_{index}"))
        config.update(effective_from=start, effective_to=end, gst_mode=gst, tuition_period=period,
                      previous_rule_uid=current["uid"])
        config.setdefault("claim", {}).update(
            payment_days_from_valid_invoice=int(days), commencement_days=int(minimum),
            registration_required=registration == "1",
        )
        return RevenueService(repository).activate_commission_rule(
            current["contract_uid"], config, actor, "Admin", key + ":rule",
            school_commission_formal=True, revision_dates=(start, end),
        )

    return repository.command(
        idempotency_key=key, command_type="school_commission_rule_revised", aggregate_type="commission_rule",
        payload={"document_uid": document_uid, "form": dict(form)},
        actor_user_id=actor, operation=operation,
    )
