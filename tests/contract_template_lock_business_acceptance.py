#!/usr/bin/env python3
"""Production-derived isolated acceptance for template lock recovery.

The input database and artifacts must stay under this worktree's SAFEBOX.  The
baseline is opened read-only and every write targets the disposable candidate
database supplied on the command line.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import contract_domain
import contract_generation_authority as authority
from contract_generation_cutover import create_contract_atomic
from database import Database


TEMPLATE_TYPE = "TransferApplicationContract"
MARKER = "LOCK_RECOVERY_AFTER_RESTART"
ACTOR_ID = 2
SYNTHETIC_STUDENT_ID = "20260006"
PROTECTED_TABLES = (
    "contracts",
    "contract_versions",
    "contract_signatures",
    "contract_signing_links",
    "student_documents",
)


def digest(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def row_fingerprints(connection: sqlite3.Connection, table: str) -> dict[int, str]:
    return {
        int(row["id"]): digest(dict(row))
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")
    }


def snapshot(path: Path, *, read_only: bool = False) -> dict[str, dict[int, str]]:
    target = f"file:{path}?mode=ro&immutable=1" if read_only else str(path)
    connection = sqlite3.connect(target, uri=read_only)
    connection.row_factory = sqlite3.Row
    try:
        return {
            table: row_fingerprints(connection, table)
            for table in PROTECTED_TABLES
        }
    finally:
        connection.close()


def run(database_path: Path, baseline_path: Path, output_root: Path) -> dict[str, object]:
    output_root.mkdir(parents=True, exist_ok=True)
    baseline_rows = snapshot(baseline_path, read_only=True)
    before_rows = snapshot(database_path)
    if before_rows != baseline_rows:
        raise AssertionError("browser rehearsal changed a protected historical row")

    database = Database(database_path)
    connection = database.connect()
    package = authority.load_published_template(connection, TEMPLATE_TYPE)
    pointer = connection.execute(
        "SELECT current_published_version_id FROM contract_template_identities "
        "WHERE template_type=?",
        (TEMPLATE_TYPE,),
    ).fetchone()[0]
    actions = [
        row[0]
        for row in connection.execute(
            "SELECT action FROM contract_template_audit_events "
            "WHERE version_id=? ORDER BY id",
            (package.version_id,),
        )
    ]
    connection.close()
    if int(pointer) != int(package.version_id):
        raise AssertionError("generation resolver did not load the current pointer")
    if MARKER not in json.dumps(package.snapshot, ensure_ascii=False):
        raise AssertionError("published Transfer snapshot lacks the recovery marker")
    required_actions = (
        "template_simulation_passed",
        "template_validation_passed",
        "template_preview_confirmed",
        "template_published",
    )
    if any(action not in actions for action in required_actions):
        raise AssertionError("Apply publication gates are incomplete")

    selected_services = [
        str(item["service_id"])
        for item in package.snapshot.get("services", [])
        if item.get("enabled", True) and item.get("default_selected", False)
    ]
    payload = {
        "contract_type": TEMPLATE_TYPE,
        "contract_uid": "CRMTEST-LOCK-RECOVERY-20260826",
        "contract_date": "2026-08-26",
        "generation_request_id": "crmtest-lock-recovery-20260826-001",
        "student_snapshot": {
            "student_name": "CRM TEST Lock Recovery",
            "student_full_name": "CRM TEST Lock Recovery",
            "student_id": SYNTHETIC_STUDENT_ID,
            "date_of_birth": "2011-06-14",
            "current_year_level": "Year 8",
            "applying_year_level": "Year 9",
            "preferred_intake_year": "2027",
            "preferred_intake_term": "02",
            "home_address": "Synthetic Melbourne VIC 3000",
            "father_passport_first_name": "Synthetic",
            "father_passport_last_name": "Parent",
            "father_mobile": "0400000000",
            "father_email": "synthetic.parent@example.test",
            "company_authorised_person": "Sonny Zhang",
        },
        "signer": {
            "source": "father",
            "name": "Synthetic Parent",
            "relationship": "父亲",
            "phone": "0400000000",
            "email": "synthetic.parent@example.test",
        },
        "service_scope_ids": selected_services,
        "payment_method": "澳洲公户",
        "company_authorised_person": "Sonny Zhang",
        "other_service_scope_text": "Synthetic isolated lock recovery acceptance.",
        "special_terms": "Synthetic Transfer lock recovery acceptance.",
        "parent_address": "Synthetic Melbourne VIC 3000",
        "notes": "SAFEBOX isolated fixture",
    }
    bound = authority.bind_new_contract(
        package,
        payload,
        actor={"id": ACTOR_ID, "role_name": "Manager + Student Cases"},
        generated_at="2026-08-26T03:45:00+00:00",
    )
    rendered = authority.render_bound_contract(bound)
    if rendered.template_version_id != package.version_id:
        raise AssertionError("new contract bound a non-current template")
    if MARKER not in rendered.authority_content:
        raise AssertionError("new contract did not render the applied template marker")

    generated_root = output_root / "generated"
    generated_root.mkdir(parents=True, exist_ok=True)
    target_docx = generated_root / "CRMTEST-Lock-Recovery-Transfer.docx"
    target_pdf = generated_root / "CRMTEST-Lock-Recovery-Transfer.pdf"
    target_signing = generated_root / "CRMTEST-Lock-Recovery-Transfer-signing.html"
    target_pdf.write_bytes(rendered.pdf_bytes)
    target_signing.write_bytes(rendered.signing_preview_bytes)
    persisted = create_contract_atomic(
        database,
        bound=bound,
        rendered=rendered,
        student_data={"synthetic": True, "student_id": SYNTHETIC_STUDENT_ID},
        student_id=SYNTHETIC_STUDENT_ID,
        contract_uid="CRMTEST-LOCK-RECOVERY-20260826",
        target=target_docx,
        file_version=1,
        user={"id": ACTOR_ID, "username": "tongy"},
        occurred_at="2026-08-26T03:45:00+00:00",
    )
    stored = database.one(
        "SELECT standalone_data_json FROM contracts WHERE id=?",
        (persisted.contract_id,),
    )
    frozen = contract_domain.snapshot_payload(stored["standalone_data_json"])
    if int(frozen["template_version_id"]) != int(package.version_id):
        raise AssertionError("stored contract did not freeze the applied template version")
    if MARKER not in authority.render_bound_contract(frozen).authority_content:
        raise AssertionError("stored contract cannot reproduce the applied marker")

    after_rows = snapshot(database_path)
    unchanged_existing = {
        table: all(
            after_rows[table].get(row_id) == row_hash
            for row_id, row_hash in baseline_rows[table].items()
        )
        for table in PROTECTED_TABLES
    }
    if not all(unchanged_existing.values()):
        raise AssertionError("a pre-existing contract/signing/archive row changed")

    check = sqlite3.connect(database_path)
    check.row_factory = sqlite3.Row
    quick_check = check.execute("PRAGMA quick_check").fetchone()[0]
    foreign_keys = [dict(row) for row in check.execute("PRAGMA foreign_key_check")]
    check.close()
    if quick_check != "ok" or foreign_keys:
        raise AssertionError("candidate database integrity failed")

    report = {
        "production_derived": True,
        "production_write": False,
        "database": str(database_path),
        "template_type": TEMPLATE_TYPE,
        "published_version_id": int(package.version_id),
        "marker": MARKER,
        "apply_gates": list(required_actions),
        "synthetic_contract": {
            "contract_id": persisted.contract_id,
            "document_id": persisted.document_id,
            "template_version_id": int(frozen["template_version_id"]),
            "docx": str(target_docx),
            "pdf": str(target_pdf),
            "signing_preview": str(target_signing),
            "marker_rendered": True,
        },
        "historical_immutability": {
            "all_preexisting_rows_unchanged": unchanged_existing,
            "before_counts": {
                table: len(rows) for table, rows in baseline_rows.items()
            },
            "after_counts": {
                table: len(rows) for table, rows in after_rows.items()
            },
        },
        "integrity": {
            "quick_check": quick_check,
            "foreign_key_check_rows": foreign_keys,
        },
    }
    report_path = output_root / "acceptance-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("output_root", type=Path)
    args = parser.parse_args()
    safebox_root = (ROOT / "SAFEBOX").resolve()
    values = (args.database.resolve(), args.baseline.resolve(), args.output_root.resolve())
    if any(not value.is_relative_to(safebox_root) for value in values):
        raise SystemExit("acceptance requires worktree SAFEBOX paths")
    print(json.dumps(run(*values), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
