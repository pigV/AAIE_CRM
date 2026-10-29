"""Read-only SQL predicates used by existing CRM list handlers."""

import re


def _alias(value: str) -> str:
    alias = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("invalid SQL alias")
    return alias


def unsigned_contract_predicate(contract_alias: str = "c") -> str:
    alias = _alias(contract_alias)
    return (
        f"{alias}.contract_status IN ('Draft','Sent') "
        f"AND {alias}.signed_at IS NULL "
        f"AND {alias}.deleted_at IS NULL "
        f"AND {alias}.voided_at IS NULL"
    )


def missing_school_plan_predicate(student_alias: str = "s") -> str:
    alias = _alias(student_alias)
    return (
        "NOT EXISTS (SELECT 1 FROM school_plan_reports mobile_plan "
        f"WHERE mobile_plan.student_id={alias}.student_id "
        "AND mobile_plan.is_current=1 "
        "AND mobile_plan.invalidated_at IS NULL)"
    )
