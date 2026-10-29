"""Canonical contract gate for Progression business actions.

The caller must supply an explicit business type and, for school progression,
an explicit contract identity.  This module never searches a Student's other
contracts and never infers business type from notes, filenames, or free text.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from contract_domain import LOCAL_STUDENT_APPLICATION_CONTRACT


HIGHER_EDUCATION = "higher_education"
SCHOOL_PROGRESSION = "school_progression"
BUSINESS_TYPES = frozenset({HIGHER_EDUCATION, SCHOOL_PROGRESSION})


@dataclass(frozen=True)
class ProgressionContractGateDecision:
    business_type: str | None
    source_contract_id: int | None
    contract_required: bool
    allowed: bool
    status: str
    label: str
    contract_status: str | None = None


def _decision(
    business_type: str | None,
    source_contract_id: int | None,
    *,
    contract_required: bool,
    allowed: bool,
    status: str,
    label: str,
    contract_status: str | None = None,
) -> ProgressionContractGateDecision:
    return ProgressionContractGateDecision(
        business_type=business_type,
        source_contract_id=source_contract_id,
        contract_required=contract_required,
        allowed=allowed,
        status=status,
        label=label,
        contract_status=contract_status,
    )


def _contract_row(
    connection: sqlite3.Connection, contract_id: int
) -> sqlite3.Row | tuple | None:
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(contracts)")
    }
    deleted = "deleted_at" if "deleted_at" in columns else "NULL"
    voided = "voided_at" if "voided_at" in columns else "NULL"
    return connection.execute(
        f"""SELECT id,student_id,contract_type,contract_status,
                   {deleted} AS deleted_at,{voided} AS voided_at
              FROM contracts WHERE id=?""",
        (contract_id,),
    ).fetchone()


def evaluate_progression_contract_gate(
    connection: sqlite3.Connection,
    *,
    student_id: str,
    business_type: str | None,
    source_contract_id: int | None,
) -> ProgressionContractGateDecision:
    """Return one canonical, current decision for a Progression action."""

    if business_type not in BUSINESS_TYPES:
        return _decision(
            business_type,
            source_contract_id,
            contract_required=False,
            allowed=False,
            status="unclassified",
            label="请先明确升学业务类型",
        )

    if business_type == HIGHER_EDUCATION:
        if source_contract_id is not None:
            return _decision(
                business_type,
                source_contract_id,
                contract_required=False,
                allowed=False,
                status="invalid_ownership",
                label="合同归属不匹配",
            )
        return _decision(
            business_type,
            None,
            contract_required=False,
            allowed=True,
            status="not_required",
            label="无需合同",
        )

    if source_contract_id is None:
        return _decision(
            business_type,
            None,
            contract_required=True,
            allowed=False,
            status="waiting_contract_creation",
            label="等待建立合同",
        )

    try:
        contract_id = int(source_contract_id)
    except (TypeError, ValueError):
        return _decision(
            business_type,
            None,
            contract_required=True,
            allowed=False,
            status="invalid_ownership",
            label="合同归属不匹配",
        )
    contract = _contract_row(connection, contract_id)
    if contract is None:
        return _decision(
            business_type,
            contract_id,
            contract_required=True,
            allowed=False,
            status="invalid_ownership",
            label="合同归属不匹配",
        )

    values = tuple(contract)
    _id, owner_student_id, contract_type, contract_status, deleted_at, voided_at = values
    if (
        str(owner_student_id or "") != str(student_id)
        or str(contract_type or "") != LOCAL_STUDENT_APPLICATION_CONTRACT
        or deleted_at is not None
        or voided_at is not None
    ):
        return _decision(
            business_type,
            contract_id,
            contract_required=True,
            allowed=False,
            status="invalid_ownership",
            label="合同归属不匹配",
            contract_status=str(contract_status or "") or None,
        )

    normalized_status = str(contract_status or "")
    if normalized_status == "FullyExecuted":
        return _decision(
            business_type,
            contract_id,
            contract_required=True,
            allowed=True,
            status="complete",
            label="合同已完成",
            contract_status=normalized_status,
        )
    if normalized_status == "ParentSigned":
        return _decision(
            business_type,
            contract_id,
            contract_required=True,
            allowed=False,
            status="waiting_aaie_execution",
            label="等待AAIE签章",
            contract_status=normalized_status,
        )
    return _decision(
        business_type,
        contract_id,
        contract_required=True,
        allowed=False,
        status="waiting_parent_signature",
        label="等待家长签署",
        contract_status=normalized_status or None,
    )


def resolve_progression_entry_gate(
    connection: sqlite3.Connection,
    *,
    student_id: str,
) -> ProgressionContractGateDecision:
    """Fail closed until this Progression's business type is explicit.

    A Student's current or historical education stream does not identify the
    target of a new Progression: a school Student may be progressing either to
    another K-12 stage or to Higher Education.  For the same reason this entry
    resolver never searches the Student's contracts.  Exact ownership starts
    only when the create command receives and transactionally validates the
    explicit business type and contract ID.
    """

    return evaluate_progression_contract_gate(
        connection,
        student_id=student_id,
        business_type=None,
        source_contract_id=None,
    )
