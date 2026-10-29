"""Canonical exact-contract ownership gate for Existing Transfer."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from contract_domain import TRANSFER_APPLICATION_CONTRACT


@dataclass(frozen=True)
class TransferContractGateDecision:
    source_contract_id: int | None
    allowed: bool
    status: str
    label: str
    contract_status: str | None = None
    final_pdf_path: str | None = None
    application_cycle_id: int | None = None


def _decision(
    source_contract_id: int | None,
    *,
    allowed: bool,
    status: str,
    label: str,
    contract_status: str | None = None,
    final_pdf_path: str | None = None,
    application_cycle_id: int | None = None,
) -> TransferContractGateDecision:
    return TransferContractGateDecision(
        source_contract_id=source_contract_id,
        allowed=allowed,
        status=status,
        label=label,
        contract_status=contract_status,
        final_pdf_path=final_pdf_path,
        application_cycle_id=application_cycle_id,
    )


def _contract_row(connection: sqlite3.Connection, contract_id: int):
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(contracts)")
    }
    deleted = "deleted_at" if "deleted_at" in columns else "NULL"
    voided = "voided_at" if "voided_at" in columns else "NULL"
    cycle = "application_cycle_id" if "application_cycle_id" in columns else "NULL"
    final_pdf = "final_signed_file_path" if "final_signed_file_path" in columns else "NULL"
    return connection.execute(
        f"""SELECT id,student_id,contract_type,contract_status,
                   {deleted} AS deleted_at,{voided} AS voided_at,
                   {cycle} AS application_cycle_id,
                   {final_pdf} AS final_signed_file_path
              FROM contracts WHERE id=?""",
        (contract_id,),
    ).fetchone()


def evaluate_transfer_contract_gate(
    connection: sqlite3.Connection,
    *,
    student_id: str,
    source_contract_id: int | None,
    expected_cycle_id: int | None,
) -> TransferContractGateDecision:
    """Evaluate only the explicitly selected Transfer contract.

    No Student-level fallback search is performed. Before activation the
    contract must be unbound; after activation it must be bound to the exact
    expected canonical Cycle.
    """

    if source_contract_id is None:
        return _decision(
            None,
            allowed=False,
            status="waiting_contract_creation",
            label="等待建立转学合同",
        )
    try:
        contract_id = int(source_contract_id)
    except (TypeError, ValueError):
        return _decision(
            None,
            allowed=False,
            status="invalid_ownership",
            label="合同归属不匹配",
        )
    row = _contract_row(connection, contract_id)
    if row is None:
        return _decision(
            contract_id,
            allowed=False,
            status="invalid_ownership",
            label="合同归属不匹配",
        )
    (
        _contract_id,
        owner_student_id,
        contract_type,
        contract_status,
        deleted_at,
        voided_at,
        application_cycle_id,
        final_pdf_path,
    ) = tuple(row)
    cycle_matches = (
        application_cycle_id is None
        if expected_cycle_id is None
        else application_cycle_id is not None
        and int(application_cycle_id) == int(expected_cycle_id)
    )
    if (
        str(owner_student_id or "") != str(student_id)
        or str(contract_type or "") != TRANSFER_APPLICATION_CONTRACT
        or deleted_at is not None
        or voided_at is not None
        or not cycle_matches
    ):
        return _decision(
            contract_id,
            allowed=False,
            status="invalid_ownership",
            label="合同归属不匹配",
            contract_status=str(contract_status or "") or None,
            application_cycle_id=(
                int(application_cycle_id) if application_cycle_id is not None else None
            ),
        )
    normalized_status = str(contract_status or "")
    if normalized_status == "FullyExecuted":
        normalized_final_pdf = str(final_pdf_path or "").strip()
        if not normalized_final_pdf:
            return _decision(
                contract_id,
                allowed=False,
                status="waiting_final_pdf",
                label="等待生成双方签署最终版合同",
                contract_status=normalized_status,
                application_cycle_id=(
                    int(application_cycle_id)
                    if application_cycle_id is not None else None
                ),
            )
        return _decision(
            contract_id,
            allowed=True,
            status="complete",
            label="转学合同已完成",
            contract_status=normalized_status,
            final_pdf_path=normalized_final_pdf,
            application_cycle_id=(
                int(application_cycle_id) if application_cycle_id is not None else None
            ),
        )
    if normalized_status == "ParentSigned":
        return _decision(
            contract_id,
            allowed=False,
            status="waiting_aaie_execution",
            label="等待AAIE签章",
            contract_status=normalized_status,
        )
    return _decision(
        contract_id,
        allowed=False,
        status="waiting_parent_signature",
        label="等待家长签署",
        contract_status=normalized_status or None,
    )
