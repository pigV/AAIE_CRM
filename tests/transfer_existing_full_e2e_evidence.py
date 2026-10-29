"""Database evidence for the two fresh Existing Transfer browser journeys."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Mapping, Sequence


def _rows(connection: sqlite3.Connection, sql: str, parameters=()) -> list[dict]:
    connection.row_factory = sqlite3.Row
    return [dict(row) for row in connection.execute(sql, parameters)]


def _fingerprint(connection: sqlite3.Connection, table: str, key: str, value: object) -> dict:
    rows = _rows(connection, f'SELECT * FROM "{table}" WHERE "{key}"=? ORDER BY rowid', (value,))
    digest = hashlib.sha256(
        (json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str) + "\n").encode()
    ).hexdigest()
    return {"rows": len(rows), "sha256": digest}


def historical_fingerprints(connection: sqlite3.Connection, pre_state: Mapping) -> dict:
    result = {}
    for student_id, identities in pre_state["historical_ids"].items():
        result[student_id] = {
            "ordinary_cycle": _fingerprint(connection, "application_cycles", "id", identities["ordinary_cycle_id"]),
            "ordinary_contract": _fingerprint(connection, "contracts", "id", identities["ordinary_contract_id"]),
            "old_coe": _fingerprint(connection, "coe_oshc", "id", identities["old_coe_id"]),
            "visa": _fingerprint(connection, "visas", "id", identities["visa_id"]),
        }
    return result


def validate_two_source_authority_bindings(authorities: Sequence[Mapping], cycles: Sequence[Mapping]) -> dict[int, Mapping]:
    """Require one immutable Existing authority for each of the two exact cycles."""
    if len(authorities) != 2:
        raise AssertionError("exact two Existing source authorities required")
    if len(cycles) != 2:
        raise AssertionError("exact completed and second active Transfer cycles required")
    cycle_ids = {int(row["id"]) for row in cycles}
    authority_cycles = {int(row["application_cycle_id"]) for row in authorities}
    contract_ids = {int(row["transfer_contract_id"]) for row in authorities}
    if authority_cycles != cycle_ids or len(contract_ids) != 2:
        raise AssertionError("source authorities must bind distinct contracts to the exact two cycles")
    by_cycle = {int(row["application_cycle_id"]): row for row in authorities}
    for cycle in cycles:
        authority = by_cycle[int(cycle["id"])]
        if authority.get("lifecycle_state") != "consumed":
            raise AssertionError("both source authorities must be consumed")
        if not authority.get("current_enrolment_id") or not authority.get("ordinary_relationship_cycle_id"):
            raise AssertionError("Existing dual evidence incomplete")
        if sum(bool(authority.get(key)) for key in (
            "ordinary_relationship_contract_id", "private_school_application_id",
            "public_application_group_id",
        )) != 1:
            raise AssertionError("Existing historical relationship evidence must be exact")
        if int(authority["transfer_contract_id"]) != int(cycle["source_contract_id"]):
            raise AssertionError("source authority contract and Transfer Case contract differ")
    return by_cycle


def verify_completed_existing_transfers(
    connection: sqlite3.Connection,
    *,
    student_ids: Sequence[str],
    pre_state: Mapping,
    browser_result: Mapping,
) -> dict:
    before = pre_state.get("historical_fingerprints") or {}
    after = historical_fingerprints(connection, pre_state)
    if before and before != after:
        raise AssertionError("historical ordinary cycle/contract/COE/Visa changed")
    journeys = []
    for student_id in student_ids:
        authorities = _rows(connection, """
            SELECT * FROM transfer_source_authorities
             WHERE student_id=? AND source_classification='aaie_existing'
             ORDER BY id
        """, (student_id,))
        cycles = _rows(connection, """
            SELECT cycle.*,case_row.id AS case_id,transfer_case.source_contract_id
              FROM application_cycles AS cycle
              JOIN student_cases AS case_row ON case_row.id=cycle.source_student_case_id
              JOIN transfer_cases AS transfer_case ON transfer_case.case_id=case_row.id
             WHERE cycle.student_id=? AND cycle.cycle_type='transfer'
             ORDER BY cycle.id
        """, (student_id,))
        authority_by_cycle = validate_two_source_authority_bindings(authorities, cycles)
        completed = [row for row in cycles if row.get("status") == "closed" and row.get("outcome") == "completed"]
        active = [row for row in cycles if row.get("status") == "active"]
        if len(completed) != 1 or len(active) != 1 or completed[0]["id"] == active[0]["id"]:
            raise AssertionError(f"{student_id}: completed and second active cycles are not distinct")
        cycle_id = int(completed[0]["id"])
        completed_authority = authority_by_cycle[cycle_id]
        acceptances = _rows(connection, """
            SELECT * FROM cycle_final_acceptances
             WHERE application_cycle_id=? ORDER BY id
        """, (cycle_id,))
        active_acceptances = [row for row in acceptances if row.get("acceptance_status") == "active"]
        if len(active_acceptances) != 1:
            raise AssertionError(f"{student_id}: exact one accepted branch required")
        acceptance = active_acceptances[0]
        payments = _rows(connection, """
            SELECT confirmation.* FROM transfer_payment_confirmations AS confirmation
            JOIN case_record_links AS link ON link.id=confirmation.payment_link_id
            JOIN transfer_school_applications AS branch ON branch.id=link.transfer_application_id
            WHERE branch.application_cycle_id=? AND branch.id=?
        """, (cycle_id, int(acceptance["transfer_branch_id"])))
        if len(payments) != 1:
            raise AssertionError(f"{student_id}: exact accepted-branch payment confirmation required")
        coes = _rows(connection, """
            SELECT * FROM transfer_coe_confirmations
             WHERE application_cycle_id=? AND final_acceptance_id=?
        """, (cycle_id, int(acceptance["id"])))
        if len(coes) != 1:
            raise AssertionError(f"{student_id}: exact accepted-branch COE confirmation required")
        contract = connection.execute(
            """SELECT contract.* FROM transfer_cases AS transfer_case
                 JOIN contracts AS contract ON contract.id=transfer_case.source_contract_id
                WHERE transfer_case.case_id=?""",
            (int(completed[0]["case_id"]),),
        ).fetchone()
        if contract is None or contract["contract_type"] != "TransferApplicationContract":
            raise AssertionError(f"{student_id}: authoritative Transfer contract missing")
        fee = str(contract["consulting_and_visa_service_fee"] or "").replace(",", "").strip()
        if fee not in {"1200", "1200.0", "1200.00"}:
            raise AssertionError(f"{student_id}: Existing Transfer base fee is not AUD 1,200")
        if str(contract["contract_status"] or "").lower() not in {
            "fullyexecuted", "fully executed", "completed", "signed"
        }:
            raise AssertionError(f"{student_id}: Transfer contract is not fully signed")
        completion = _rows(connection, "SELECT * FROM transfer_completion_events WHERE application_cycle_id=?", (cycle_id,))
        if len(completion) != 1:
            raise AssertionError(f"{student_id}: exact one completion event required")
        old_id = int(pre_state["historical_ids"][student_id]["current_enrolment_id"])
        old = _rows(connection, "SELECT * FROM student_school_enrolments WHERE id=?", (old_id,))[0]
        current = _rows(connection, """
            SELECT * FROM student_school_enrolments
             WHERE student_id=? AND enrolment_role='current'
        """, (student_id,))
        if old.get("enrolment_role") != "historical" or len(current) != 1 or int(current[0]["id"]) == old_id:
            raise AssertionError(f"{student_id}: old Current to Historical/new Upcoming to Current not proven")
        journeys.append({
            "student_id": student_id,
            "completed_cycle_id": cycle_id,
            "second_cycle_id": int(active[0]["id"]),
            "acceptance_id": int(acceptance["id"]),
            "payment_confirmation_id": int(payments[0]["id"]),
            "coe_confirmation_id": int(coes[0]["id"]),
            "completion_id": int(completion[0]["id"]),
            "contract_id": int(contract["id"]),
            "completed_source_authority_id": int(completed_authority["id"]),
            "second_source_authority_id": int(authority_by_cycle[int(active[0]["id"])]["id"]),
            "base_fee_aud": fee,
            "old_enrolment_role": old["enrolment_role"],
            "new_current_enrolment_id": int(current[0]["id"]),
        })
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != "ok" or foreign_keys:
        raise AssertionError("post-E2E database integrity failed")
    if not browser_result.get("desktop", {}).get("passed") or not browser_result.get("mobile", {}).get("passed"):
        raise AssertionError("both Desktop and Mobile browser journeys must pass")
    return {
        "status": "PASS",
        "journeys": journeys,
        "historical_fingerprints_unchanged": before == after,
        "integrity_check": integrity,
        "foreign_key_violations": 0,
    }


def write_evidence(path: Path, payload: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
