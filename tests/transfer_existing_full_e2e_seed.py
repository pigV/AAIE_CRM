#!/usr/bin/env python3
"""Precondition-only fixture for the Existing Transfer browser acceptance.

The fixture deliberately stops before the new Transfer begins.  In particular,
source authority, Transfer contracts, Transfer cycles, Offers, Acceptance,
Payments, new COEs, enrolment transitions, and completion must be created by the
real application during the browser journey.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Callable, Mapping, Sequence


PRODUCTION_ROOT = Path("/Volumes/Zhang/AAIE CodeX/CRM").resolve()
PRODUCTION_DB = PRODUCTION_ROOT / "database/aaie_crm.db"
NAS_ROOT = Path("/Volumes/Server").resolve()
FROZEN_PORTS = {8780, 8781, 8880, 8881}
STUDENTS = ("99197001", "99197002")
STAMP = "2026-09-17T10:00:00+10:00"


class IsolationViolation(RuntimeError):
    pass


def _resolved_without_symlink(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise IsolationViolation("absolute isolated paths are required")
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise IsolationViolation(f"symlink path rejected: {path}")
    return path.resolve()


def _contains(parent: Path, child: Path) -> bool:
    return parent == child or parent in child.parents


def validate_isolated_runtime(
    runtime_root: Path,
    database_path: Path,
    storage_root: Path,
    ports: Sequence[int],
) -> tuple[Path, Path, Path]:
    runtime = _resolved_without_symlink(runtime_root)
    database = _resolved_without_symlink(database_path)
    storage = _resolved_without_symlink(storage_root)
    if len(tuple(ports)) != 2 or len(set(ports)) != 2:
        raise IsolationViolation("two distinct isolated ports are required")
    if FROZEN_PORTS.intersection(int(port) for port in ports):
        raise IsolationViolation("Production or frozen Staging port rejected")
    if any(not 1024 <= int(port) <= 65535 for port in ports):
        raise IsolationViolation("invalid isolated port")
    if not _contains(runtime, database) or not _contains(runtime, storage):
        raise IsolationViolation("database and storage must stay inside runtime root")
    for protected in (PRODUCTION_DB, PRODUCTION_ROOT / "STAGING_DATA", NAS_ROOT):
        protected = protected.resolve()
        if any(
            _contains(protected, candidate) or _contains(candidate, protected)
            for candidate in (runtime, database, storage)
        ):
            raise IsolationViolation("Production, live Staging, or NAS path rejected")
    if database.exists() and (not database.is_file() or database.stat().st_nlink != 1):
        raise IsolationViolation("database must be a single-link regular file")
    if storage.exists() and not storage.is_dir():
        raise IsolationViolation("storage must be a directory")
    return runtime, database, storage


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _insert(connection: sqlite3.Connection, table: str, values: Mapping[str, object]) -> int:
    if not _table_exists(connection, table):
        raise IsolationViolation(f"required table missing: {table}")
    allowed = _columns(connection, table)
    filtered = {key: value for key, value in values.items() if key in allowed}
    if not filtered:
        raise IsolationViolation(f"no compatible columns for {table}")
    names = ",".join(f'"{name}"' for name in filtered)
    placeholders = ",".join("?" for _ in filtered)
    cursor = connection.execute(
        f'INSERT INTO "{table}"({names}) VALUES({placeholders})', tuple(filtered.values())
    )
    return int(cursor.lastrowid)


def _count_student_rows(
    connection: sqlite3.Connection, table: str, student_ids: Sequence[str], extra: str = ""
) -> int:
    if not _table_exists(connection, table):
        return 0
    columns = _columns(connection, table)
    if "student_id" not in columns:
        return 0
    marks = ",".join("?" for _ in student_ids)
    return int(connection.execute(
        f'SELECT count(*) FROM "{table}" WHERE student_id IN ({marks}) {extra}',
        tuple(student_ids),
    ).fetchone()[0])


def assert_precondition_only(
    connection: sqlite3.Connection, student_ids: Sequence[str] = STUDENTS
) -> None:
    forbidden = {
        "transfer_source_authorities": "",
        "transfer_offer_bindings": "",
        "transfer_payment_confirmations": "",
        "transfer_coe_confirmations": "",
        "school_enrolment_transition_intents": "",
        "student_cases": "AND case_type='transfer'",
        "application_cycles": "AND cycle_type='transfer'",
    }
    violations = {
        table: _count_student_rows(connection, table, student_ids, extra)
        for table, extra in forbidden.items()
    }
    violations = {table: count for table, count in violations.items() if count}
    if violations:
        raise AssertionError(f"fixture crossed the precondition-only boundary: {violations}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def seed_preconditions(
    connection: sqlite3.Connection,
    *,
    storage_root: Path,
    password_hashes: Mapping[str, str],
    publish_contract_authority: Callable[[sqlite3.Connection, Path, int], None],
    student_ids: Sequence[str] = STUDENTS,
) -> dict[str, object]:
    """Create only synthetic facts that predate the two new Transfers."""
    if tuple(student_ids) != STUDENTS:
        raise IsolationViolation("the reviewed two-student fixture identity changed")
    if set(password_hashes) != {"e2e_admin", "e2e_manager", "e2e_staff"}:
        raise IsolationViolation("exact three ephemeral actor credentials are required")
    if any(not value for value in password_hashes.values()):
        raise IsolationViolation("empty credential hash rejected")
    if _count_student_rows(connection, "students", student_ids):
        raise IsolationViolation("fixture students already exist; refusing reuse")
    assert_precondition_only(connection, student_ids)

    roles = dict(connection.execute("SELECT name,id FROM roles"))
    staff_role = "Consultant" if "Consultant" in roles else "Staff"
    if not {"Admin", "Manager", staff_role} <= set(roles):
        raise IsolationViolation("required Admin, Manager, and Staff roles are missing")
    actor_ids: dict[str, int] = {}
    for username, role_name, label in (
        ("e2e_admin", "Admin", "Existing E2E Admin"),
        ("e2e_manager", "Manager", "Existing E2E Manager"),
        ("e2e_staff", staff_role, "Existing E2E Staff"),
    ):
        actor_ids[username] = _insert(connection, "users", {
            "username": username, "name": label, "display_name": label,
            "password_hash": password_hashes[username], "role_id": int(roles[role_name]),
            "is_active": 1, "must_change_password": 0, "created_at": STAMP,
        })
    admin_id = actor_ids["e2e_admin"]
    case_permissions = {
        "student_cases.view", "student_cases.create", "student_cases.edit",
        "student_cases.assign", "student_cases.complete", "student_cases.undo_complete",
        "contracts.manage",
    }
    for role_name in ("Admin", "Manager", staff_role):
        current = set(json.loads(connection.execute(
            "SELECT permissions_json FROM roles WHERE name=?", (role_name,)
        ).fetchone()[0]))
        current.update(case_permissions)
        connection.execute(
            "UPDATE roles SET permissions_json=? WHERE name=?",
            (json.dumps(sorted(current)), role_name),
        )
    ordinary_modules = (
        "dashboard", "students", "parent_form", "applications", "offer", "coe_oshc",
        "visa", "payments", "student_contracts", "contract_management_center",
        "revenue_commission",
    )
    for username in ("e2e_manager", "e2e_staff"):
        for module in ordinary_modules:
            _insert(connection, "user_module_permissions", {
                "user_id": actor_ids[username], "module_key": module,
                "can_access": 1, "can_view": 1, "can_create": 1,
                "can_edit": 1, "can_complete": 1,
                "can_upload": 1 if module == "payments" else 0,
                "can_confirm_upload_review": 1 if module == "payments" else 0,
                "created_at": STAMP, "updated_at": STAMP, "updated_by": admin_id,
            })

    # Seed the ordinary, effective-dated commercial authority through the same
    # exact-Admin domain service used by governed environments.  This is a
    # prerequisite policy fact, not a Transfer outcome or a hard-coded fee.
    from revenue.client_fee_rules import FeeRuleAuthorityService
    from revenue.repository import RevenueRepository

    fee_authority = FeeRuleAuthorityService(
        RevenueRepository(connection, now=lambda: STAMP)
    ).initialize_2026(
        actor_user_id=admin_id,
        actor_role="Admin",
        idempotency_key="existing-transfer-full-e2e-fee-authority-2026-v1",
    )

    schools = []
    for offset, (name, sector, school_type) in enumerate((
        ("E2E Current College", "private", "Independent"),
        ("E2E Target Grammar", "private", "Independent"),
        ("E2E Target High", "public", "Government"),
    ), 1):
        schools.append(_insert(connection, "schools", {
            "school_id": f"E2E-SCHOOL-{offset}", "school_name": name,
            "school_name_english": name, "school_name_display": name,
            "sector_key": sector, "school_type": school_type, "state": "VIC",
            "region": "Melbourne", "basic_info_json": "{}",
            "library_structure_json": "{}", "dynamic_tags_json": "[]",
            "zone_data_json": "{}", "academic_info_json": "{}",
            "data_quality_status": "Reviewed", "notes": "Synthetic browser acceptance only",
            "created_at": STAMP, "updated_at": STAMP, "updated_by": admin_id,
        }))

    before = {}
    for index, student_id in enumerate(student_ids, 1):
        _insert(connection, "students", {
            "student_id": student_id, "passport_last_name": "E2E",
            "passport_first_name": f"Existing {index}", "english_nickname": f"E2E Existing {index}",
            "date_of_birth": "2012-01-01", "current_year_level": "Year 8",
            "applying_year_level": "Year 9", "preferred_intake_year": "2027",
            "preferred_intake_term": "Term 1", "current_status": "Active",
            "nas_status": "pending", "general_notes": "SYNTHETIC EXISTING TRANSFER E2E",
            "created_date": STAMP, "updated_at": STAMP, "updated_by": admin_id,
            "form_data_json": "{}", "application_stream": "school_education",
        })
        parent_id = _insert(connection, "parents", {
            "student_id": student_id, "parent_type": "mother",
            "passport_last_name": "PARENT", "passport_first_name": f"Synthetic {index}",
            "mobile": "0400000000", "email": f"parent-{student_id}@example.test",
        })
        cycle_id = _insert(connection, "application_cycles", {
            "cycle_uid": f"e2e-existing-history-{student_id}", "student_id": student_id,
            "cycle_type": "initial", "education_stream": "school_education",
            "sequence_no": 1, "status": "closed", "outcome": "completed",
            "opened_at": "2025-01-01", "closed_at": "2025-12-01",
            "created_by_user_id": admin_id, "created_by_name": "Existing E2E Admin",
            "created_at": "2025-01-01", "updated_by_user_id": admin_id,
            "updated_by_name": "Existing E2E Admin", "updated_at": "2025-12-01",
        })
        ordinary_contract_id = _insert(connection, "contracts", {
            "student_id": student_id, "contract_uid": f"e2e-ordinary-{student_id}",
            "contract_category": "学生申请合同", "contract_type": "LocalStudentApplicationContract",
            "contract_title": "Synthetic historical ordinary application",
            "contract_status": "FullyExecuted", "contract_date": "2025-01-01",
            "service_type": "K-12 Application", "service_scope_json": "[]",
            "generated_by": admin_id, "generated_at": "2025-01-01",
            "fully_executed_at": "2025-01-02", "updated_at": "2025-01-02",
            "application_cycle_id": cycle_id,
        })
        enrolment_id = _insert(connection, "student_school_enrolments", {
            "student_id": student_id, "school_id": schools[0],
            "school_name_snapshot": "E2E Current College", "school_sector": "independent",
            "enrolment_role": "current", "year_level_snapshot": "Year 8",
            "start_date": "2026-01-27", "status": "active", "created_by": admin_id,
            "created_at": STAMP, "updated_by": admin_id, "updated_at": STAMP,
            "application_cycle_id": cycle_id, "lifecycle_context": "initial",
            "confirmation_reason": "Synthetic historical current enrolment",
            "confirmed_by": admin_id, "confirmed_at": STAMP,
        })
        old_coe_id = _insert(connection, "coe_oshc", {
            "student_id": student_id, "document_kind": "COE",
            "coe_provider": "E2E Current College", "coe_course": "Year 8",
            "coe_start_date": "2026-01-27", "coe_end_date": "2026-12-15",
            "file_path": str(storage_root / f"historical/{student_id}/old-coe.pdf"),
            "application_cycle_id": cycle_id, "created_at": "2025-12-15",
        })
        visa_id = _insert(connection, "visas", {
            "student_id": student_id, "visa_type": "Subclass 500",
            "visa_sector": "Schools", "visa_grant_date": "2026-01-01",
            "visa_start_date": "2026-01-01", "visa_expiry_date": "2028-01-01",
            "visa_status": "Granted", "created_at": STAMP,
        })
        before[student_id] = {
            "ordinary_cycle_id": cycle_id,
            "ordinary_contract_id": ordinary_contract_id,
            "parent_id": parent_id,
            "current_enrolment_id": enrolment_id,
            "old_coe_id": old_coe_id,
            "visa_id": visa_id,
        }

    publish_contract_authority(connection, storage_root, admin_id)
    assert_precondition_only(connection, student_ids)
    return {
        "fixture": "existing-transfer-preconditions-v1",
        "student_ids": list(student_ids),
        "actor_usernames": sorted(actor_ids),
        "school_ids": schools,
        "historical_ids": before,
        "fee_authority": fee_authority,
        "credentials": "ephemeral values intentionally omitted",
    }


def write_pre_state(
    connection: sqlite3.Connection, database_path: Path, storage_root: Path, output: Path,
    receipt: Mapping[str, object],
) -> None:
    assert_precondition_only(connection)
    storage_names = sorted(
        str(path.relative_to(storage_root)) for path in storage_root.rglob("*")
        if path.is_file()
    )
    payload = dict(receipt)
    payload.update({
        "database_sha256": _sha256(database_path),
        "storage_files": storage_names,
        "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_key_violations": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
    })
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
