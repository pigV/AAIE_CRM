"""Read signed contract coverage and resolve the fee for a Visa case."""

from decimal import Decimal, InvalidOperation
import json
import re

from revenue.client_fee_rules import (
    CLIENT_FEE_RULES_2026, ClientFeeRuleFacts, MissingFeeRule, ResolvedClientFeeRule,
)


STUDENT_VISA_PACKAGE = "signed_student_visa_package"


VISA_FEE_KINDS = {
    rule["milestone_code"]: next(value for key, _, value in rule["conditions"] if key == "service_kind")
    for rule in CLIENT_FEE_RULES_2026
    if any(key == "service_kind" and value != "base" for key, _, value in rule["conditions"])
}
VISA_FEE_KINDS[STUDENT_VISA_PACKAGE] = "student_visa"
VISA_FEE_KINDS["visitor-600"] = "visitor_600"
VISA_CATEGORY_LABELS = {"590": "陪读签证", "600": "旅游签证", "500": "学生签证"}


class SignedVisaPackageRequired(MissingFeeRule):
    """A default package pricing rule needs evidence from a signed contract."""


def fully_executed_contracts(contracts):
    return [
        contract for contract in map(dict, contracts)
        if str(contract.get("contract_status") or "").replace("_", "").replace(" ", "").lower() == "fullyexecuted"
        and not any(contract.get(field) for field in ("deleted_at", "voided_at", "superseded_by_contract_id"))
    ]


def signed_student_visa_package(contracts, student_id):
    signed = fully_executed_contracts(contracts)
    if len(signed) != 1 or signed[0].get("student_id") != student_id:
        return None
    contract = signed[0]
    try:
        scope = json.loads(contract.get("service_scope_json") or "[]")
        base = Decimal(str(contract.get("consulting_and_visa_service_fee") or "").replace(",", ""))
        discount = Decimal(str(contract.get("discount_amount") or "0").replace(",", ""))
        final = Decimal(str(contract.get("final_fee_amount") or "").replace(",", ""))
    except (ValueError, TypeError, InvalidOperation):
        return None
    # Contract payment options can add 10% GST. Coverage follows the signed
    # service price before GST, independent of later payment allocations.
    if (isinstance(scope, list) and "student_visa_lodgement" in scope
            and base == 2400 and discount == 0 and final in (2400, 2640)):
        return contract
    return None


def visa_subclass(visa):
    for field in ("visa_subclass", "case_type", "visa_type", "visa_case_name"):
        text = str(visa.get(field) or "").strip().casefold()
        if code := re.search(r"(?<!\d)(500|590|600)(?!\d)", text):
            return code[1]
        if any(term in text for term in ("陪读", "guardian")):
            return "590"
        if any(term in text for term in ("student visa", "学生签证", "留学生签证")):
            return "500"
    return None


def visa_fee_facts(visa, *, education_stream, cycle_type, existing_aaie):
    visa = dict(visa)
    text = " ".join(str(visa.get(key) or "").strip().casefold() for key in (
        "case_type", "application_type",
    ))
    renewal = any(word in text for word in ("renew", "extension", "续签", "延期"))
    independent = renewal or "standalone" in text or "independent" in text or "单独申请" in text
    return ClientFeeRuleFacts(
        service_kind={"590": "guardian_590", "600": "visitor_600"}.get(visa_subclass(visa), "student_visa"),
        education_stream=education_stream, cycle_type=cycle_type, existing_aaie_k12=existing_aaie,
        application_type="renewal" if renewal else "new", independent_service=independent,
    )


def visa_fee_description(visa, *, show_unclassified=True):
    visa = dict(visa)
    name = str(visa.get("applicant_name") or visa.get("main_applicant") or "申请人待核实").strip()
    code = visa_subclass(visa)
    if code:
        label = f"{code} {VISA_CATEGORY_LABELS[code]}"
    else:
        label = "类别待核实 签证" if show_unclassified else "签证"
    return f"{name} · {label}服务费 · 案件 #{visa['id']}"


def fee_visa_case(connection, fee_uid):
    """Resolve a fee's Visa case from its snapshot and exact student/cycle scope."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(visa_cases)")}
    if not {"student_id", "application_cycle_id", "applicant_name"}.issubset(columns):
        return None
    rows = connection.execute(
        """SELECT visa.* FROM revenue_fee_items fee
             JOIN revenue_rule_snapshots snap ON snap.id=fee.rule_snapshot_id
             JOIN json_each(snap.canonical_json,'$.items') item
             JOIN revenue_commercial_cases cc ON cc.id=fee.commercial_case_id AND cc.student_id=fee.student_id
             JOIN visa_cases visa ON visa.id=json_extract(item.value,'$.visa_case_id')
              AND visa.student_id=fee.student_id AND visa.application_cycle_id=cc.application_cycle_id
            WHERE fee.uid=?""", (fee_uid,),
    ).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def fee_display_description(connection, fee_uid, fallback):
    visa = fee_visa_case(connection, fee_uid)
    return visa_fee_description(visa, show_unclassified=False) if visa else (
        "Client service fee" if fallback == "Controlled onboarding client service fee" else fallback
    )


def resolve_visa_fee(resolver, facts, visa, contracts, student_id, engagement_date):
    student_visa = visa_subclass(dict(visa)) == "500"
    if facts.service_kind == "student_visa" and facts.application_type == "new" and student_visa:
        package = signed_student_visa_package(contracts, student_id)
        if package:
            return ResolvedClientFeeRule(
                rule_uid=None,
                policy_code=STUDENT_VISA_PACKAGE,
                net_cents=None,
                milestone_code=STUDENT_VISA_PACKAGE,
                applicability={"matched": True, "contract_id": package["id"], "service_kind": "student_visa"},
                amount_basis={"kind": "signed_contract_package", "package_cents": 240000, "included": True},
                source_engagement_date=engagement_date,
            )

    resolved = resolver.resolve(facts, engagement_date)
    if resolved.policy_code in {"k12-new-student-visa-included", "new-transfer-student-visa-included"}:
        raise SignedVisaPackageRequired("首次学生签证套餐待核对。请确认同一申请轮次的 A$2,400 合同已双方签妥，且包含学生签证递交。")
    return resolved
