"""Deterministic legacy-data backfill for school-plan and Offer migration 014.

This module deliberately operates only on an already-migrated database.  Its
dry-run path opens SQLite read-only and returns the same pending mutations that
an immediately following applied run would make.  Applied runs use one
explicit transaction so an ownership guard or other write failure leaves no
partial lifecycle, Offer, canonical-ranking, or exception changes behind.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from school_plan_offer_preflight import _normalise_name, _selected_school_ids


_FORMAL_DATABASE = Path("/Volumes/Zhang/AAIE CodeX/CRM/database/aaie_crm.db")
_FALLBACK_TIMESTAMP = "1970-01-01T00:00:00+00:00"

# A legacy accepted Offer has no reliable previous status.  Future acceptance
# switching/restoration services must block while this value is present, or
# require an explicit manual resolution; they must never restore a guessed
# status such as ``accepted``, ``received``, or ``signed``.
LEGACY_ACCEPTANCE_STATUS_UNKNOWN = "__legacy_status_unknown__"
FORMAL_BACKFILL_CONFIRMATION = "APPLY-SCHOOL-PLAN-OFFER-014-BACKFILL"


@dataclass(frozen=True, order=True)
class BackfillException:
    """One deterministic manual-review item discovered by the backfill."""

    entity_type: str
    entity_id: int
    student_id: str | None
    exception_type: str
    details_json: str
    created_at: str


@dataclass(frozen=True)
class BackfillResult:
    """Pending or applied mutations and all manual-review items discovered."""

    report_rows_changed: int
    canonical_rankings_copied: int
    offer_rows_changed: int
    acceptances_created: int
    exception_rows_written: int
    ambiguous_rows_changed: int
    exceptions: tuple[BackfillException, ...]

    @property
    def rows_changed(self) -> int:
        return (
            self.report_rows_changed
            + self.canonical_rankings_copied
            + self.offer_rows_changed
            + self.acceptances_created
            + self.exception_rows_written
        )


@dataclass(frozen=True)
class _Scope:
    status: str
    city_id: int | None = None
    city_name: str | None = None
    sector: str | None = None


@dataclass
class _BackfillPlan:
    operations: list[tuple[str, tuple[Any, ...]]]
    result: BackfillResult


def _readonly_connection(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _require_migrated_schema(connection: sqlite3.Connection) -> None:
    required = {
        "school_plan_reports": {
            "id", "student_id", "selected_school_ids", "report_version", "created_at",
            "city_id", "city_name_snapshot", "plan_sector", "is_current",
            "superseded_by_report_id", "scope_review_status",
        },
        "school_plan_report_confirmations": {"id", "report_id", "student_id"},
        "school_plan_report_school_rankings": {
            "id", "confirmation_id", "report_id", "school_id", "school_name",
            "rank_order", "created_at",
        },
        "school_plan_confirmed_schools": {"confirmation_id", "report_id", "student_id"},
        "offers": {
            "id", "student_id", "school_name", "offer_status", "school_id", "city_id",
            "city_name_snapshot", "sector_key", "classification_status",
        },
        "student_offer_acceptances": {"student_id", "offer_id"},
        "school_plan_offer_migration_exceptions": {
            "entity_type", "entity_id", "exception_type", "details_json", "created_at",
        },
        "schools": {"id", "school_name", "city_id", "sector_key"},
        "school_cities": {"id", "city_name"},
    }
    actual_tables = {
        str(row["name"])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing: list[str] = []
    for table, columns in required.items():
        if table not in actual_tables:
            missing.append(table)
            continue
        missing.extend(
            f"{table}.{column}" for column in sorted(columns - _table_columns(connection, table))
        )
    if missing:
        raise ValueError("backfill requires migrated schema: " + ", ".join(missing))


def _timestamp(*values: Any) -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value)
    return _FALLBACK_TIMESTAMP


def _json_details(**details: Any) -> str:
    return json.dumps(details, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _scope_for_report(
    connection: sqlite3.Connection, selected_school_ids: Any
) -> _Scope:
    school_ids, invalid_selection = _selected_school_ids(selected_school_ids)
    if invalid_selection:
        return _Scope("unresolved_school")
    if not school_ids:
        return _Scope("no_selected_schools")
    placeholders = ",".join("?" for _ in school_ids)
    rows = connection.execute(
        f"""SELECT s.id,s.city_id,s.sector_key,c.city_name
              FROM schools AS s
              LEFT JOIN school_cities AS c ON c.id=s.city_id
              WHERE s.id IN ({placeholders})""",
        school_ids,
    ).fetchall()
    schools_by_id = {int(row["id"]): row for row in rows}
    if len(schools_by_id) != len(school_ids):
        return _Scope("unresolved_school")
    scopes: list[tuple[int, str, str]] = []
    for school_id in school_ids:
        school = schools_by_id[school_id]
        city_name = str(school["city_name"] or "").strip()
        sector = str(school["sector_key"] or "").strip().casefold()
        if not school["city_id"] or not city_name or sector not in {"public", "private"}:
            return _Scope("unresolved_school")
        scopes.append((int(school["city_id"]), city_name, sector))
    if len({(city_id, city_name) for city_id, city_name, _ in scopes}) > 1:
        return _Scope("mixed_city")
    if len({sector for _, _, sector in scopes}) > 1:
        return _Scope("mixed_sector")
    city_id, city_name, sector = scopes[0]
    return _Scope("unambiguous", city_id, city_name, sector)


def _append_exception(
    connection: sqlite3.Connection,
    operations: list[tuple[str, tuple[Any, ...]]],
    pending: list[BackfillException],
    exception: BackfillException,
) -> None:
    exists = connection.execute(
        """SELECT 1 FROM school_plan_offer_migration_exceptions
           WHERE entity_type=? AND entity_id=? AND exception_type=? AND details_json=?
           LIMIT 1""",
        (
            exception.entity_type,
            exception.entity_id,
            exception.exception_type,
            exception.details_json,
        ),
    ).fetchone()
    if exists is None:
        pending.append(exception)
        operations.append(
            (
                """INSERT INTO school_plan_offer_migration_exceptions(
                       entity_type,entity_id,student_id,exception_type,details_json,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    exception.entity_type,
                    exception.entity_id,
                    exception.student_id,
                    exception.exception_type,
                    exception.details_json,
                    exception.created_at,
                ),
            )
        )


def _operation_if_changed(
    operations: list[tuple[str, tuple[Any, ...]]],
    row: sqlite3.Row,
    table: str,
    identity_column: str,
    desired: dict[str, Any],
) -> bool:
    if not any(row[column] != value for column, value in desired.items()):
        return False
    assignments = ",".join(f"{column}=?" for column in desired)
    operations.append(
        (
            f"UPDATE {table} SET {assignments} WHERE {identity_column}=?",
            tuple(desired.values()) + (row[identity_column],),
        )
    )
    return True


def _build_plan(connection: sqlite3.Connection) -> _BackfillPlan:
    _require_migrated_schema(connection)
    operations: list[tuple[str, tuple[Any, ...]]] = []
    pending_exceptions: list[BackfillException] = []
    all_exceptions: list[BackfillException] = []
    reports = connection.execute(
        """SELECT id,student_id,selected_school_ids,report_version,created_at,
                  city_id,city_name_snapshot,plan_sector,is_current,
                  superseded_by_report_id,scope_review_status
           FROM school_plan_reports ORDER BY id"""
    ).fetchall()
    scopes: dict[int, _Scope] = {}
    public_groups: dict[tuple[str, int], list[sqlite3.Row]] = {}
    report_desired: dict[int, dict[str, Any]] = {}

    for report in reports:
        report_id = int(report["id"])
        scope = _scope_for_report(connection, report["selected_school_ids"])
        scopes[report_id] = scope
        if scope.status != "unambiguous":
            exception = BackfillException(
                "school_plan_report",
                report_id,
                str(report["student_id"]),
                scope.status,
                _json_details(
                    selected_school_ids=report["selected_school_ids"], scope_status=scope.status
                ),
                _timestamp(report["created_at"]),
            )
            all_exceptions.append(exception)
            _append_exception(connection, operations, pending_exceptions, exception)
            continue
        report_desired[report_id] = {
            "city_id": scope.city_id,
            "city_name_snapshot": scope.city_name,
            "plan_sector": scope.sector,
            "scope_review_status": "resolved",
        }
        if scope.sector == "public":
            public_groups.setdefault((str(report["student_id"]), int(scope.city_id)), []).append(report)
        else:
            report_desired[report_id].update({"is_current": 1, "superseded_by_report_id": None})

    public_winners: set[int] = set()
    for grouped_reports in public_groups.values():
        winner = max(
            grouped_reports,
            key=lambda report: (
                int(report["report_version"] or 0),
                str(report["created_at"] or ""),
                int(report["id"]),
            ),
        )
        winner_id = int(winner["id"])
        public_winners.add(winner_id)
        for report in grouped_reports:
            report_id = int(report["id"])
            report_desired[report_id].update(
                {
                    "is_current": 1 if report_id == winner_id else 0,
                    "superseded_by_report_id": None if report_id == winner_id else winner_id,
                }
            )

    report_rows_changed = 0
    ordered_report_ids = sorted(
        report_desired,
        key=lambda report_id: (
            1 if report_id in public_winners else 0,
            report_id,
        ),
    )
    reports_by_id = {int(report["id"]): report for report in reports}
    for report_id in ordered_report_ids:
        if _operation_if_changed(
            operations,
            reports_by_id[report_id],
            "school_plan_reports",
            "id",
            report_desired[report_id],
        ):
            report_rows_changed += 1

    canonical_rankings_copied = 0
    ranking_rows = connection.execute(
        """SELECT ranking.id AS ranking_id,ranking.confirmation_id,ranking.report_id,
                  ranking.school_id,ranking.school_name,ranking.rank_order,
                  ranking.created_at AS ranking_created_at,
                  confirmation.report_id AS confirmation_report_id,
                  confirmation.student_id AS confirmation_student_id,
                  confirmation.confirmed_at,confirmation.created_at AS confirmation_created_at,
                  report.student_id AS report_student_id,report.created_at AS report_created_at
           FROM school_plan_report_school_rankings AS ranking
           JOIN school_plan_report_confirmations AS confirmation
             ON confirmation.id=ranking.confirmation_id
           JOIN school_plan_reports AS report ON report.id=ranking.report_id
           ORDER BY ranking.id"""
    ).fetchall()
    for ranking in ranking_rows:
        report_id = int(ranking["report_id"])
        scope = scopes.get(report_id)
        if scope is None or scope.status != "unambiguous" or scope.sector != "public":
            continue
        if (
            int(ranking["confirmation_report_id"]) != report_id
            or str(ranking["confirmation_student_id"] or "")
            != str(ranking["report_student_id"])
        ):
            exception = BackfillException(
                "school_plan_ranking",
                int(ranking["ranking_id"]),
                str(ranking["report_student_id"]),
                "ranking_ownership_mismatch",
                _json_details(
                    confirmation_id=int(ranking["confirmation_id"]), report_id=report_id
                ),
                _timestamp(ranking["ranking_created_at"], ranking["report_created_at"]),
            )
            all_exceptions.append(exception)
            _append_exception(connection, operations, pending_exceptions, exception)
            continue
        existing = connection.execute(
            """SELECT id FROM school_plan_confirmed_schools
               WHERE confirmation_id=? AND school_id IS ? AND school_name_snapshot=?
               LIMIT 1""",
            (ranking["confirmation_id"], ranking["school_id"], ranking["school_name"]),
        ).fetchone()
        if existing is not None:
            continue
        timestamp = _timestamp(
            ranking["ranking_created_at"],
            ranking["confirmed_at"],
            ranking["confirmation_created_at"],
            ranking["report_created_at"],
        )
        operations.append(
            (
                """INSERT INTO school_plan_confirmed_schools(
                       confirmation_id,report_id,student_id,school_id,school_name_snapshot,
                       city_id,city_name_snapshot,plan_sector,decision_mode,rank_order,
                       is_selected,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ranking["confirmation_id"],
                    report_id,
                    ranking["report_student_id"],
                    ranking["school_id"],
                    ranking["school_name"],
                    scope.city_id,
                    scope.city_name,
                    "public",
                    "ranked",
                    ranking["rank_order"],
                    1,
                    timestamp,
                    timestamp,
                ),
            )
        )
        canonical_rankings_copied += 1

    school_matches: dict[str, list[sqlite3.Row]] = {}
    for school in connection.execute(
        """SELECT school.id,school.school_name,school.city_id,school.sector_key,city.city_name
           FROM schools AS school LEFT JOIN school_cities AS city ON city.id=school.city_id
           ORDER BY school.id"""
    ):
        normalised = _normalise_name(school["school_name"])
        if normalised:
            school_matches.setdefault(normalised, []).append(school)

    offer_rows_changed = 0
    offers = connection.execute(
        """SELECT id,student_id,school_name,offer_status,accepted_date,created_at,
                  school_id,city_id,city_name_snapshot,sector_key,classification_status
           FROM offers ORDER BY id"""
    ).fetchall()
    accepted_by_student: dict[str, list[sqlite3.Row]] = {}
    for offer in offers:
        offer_id = int(offer["id"])
        matches = school_matches.get(_normalise_name(offer["school_name"]), [])
        desired: dict[str, Any] | None = None
        exception_type: str | None = None
        if len(matches) == 1:
            school = matches[0]
            city_name = str(school["city_name"] or "").strip()
            sector = str(school["sector_key"] or "").strip().casefold()
            if school["city_id"] and city_name and sector in {"public", "private"}:
                desired = {
                    "school_id": school["id"],
                    "city_id": school["city_id"],
                    "city_name_snapshot": city_name,
                    "sector_key": sector,
                    "classification_status": "resolved",
                }
            else:
                desired = {"classification_status": "unresolved"}
                exception_type = "offer_school_scope_unresolved"
        elif len(matches) > 1:
            desired = {"classification_status": "ambiguous"}
            exception_type = "offer_school_ambiguous"
        else:
            desired = {"classification_status": "unresolved"}
            exception_type = "offer_school_unresolved"
        if _operation_if_changed(operations, offer, "offers", "id", desired):
            offer_rows_changed += 1
        if exception_type is not None:
            exception = BackfillException(
                "offer",
                offer_id,
                str(offer["student_id"]),
                exception_type,
                _json_details(school_name=offer["school_name"]),
                _timestamp(offer["created_at"]),
            )
            all_exceptions.append(exception)
            _append_exception(connection, operations, pending_exceptions, exception)
        if str(offer["offer_status"] or "").strip().casefold() == "accepted":
            accepted_by_student.setdefault(str(offer["student_id"]), []).append(offer)

    acceptances_created = 0
    for student_id, accepted_offers in sorted(accepted_by_student.items()):
        if len(accepted_offers) != 1:
            offer_ids = [int(offer["id"]) for offer in accepted_offers]
            for offer in accepted_offers:
                exception = BackfillException(
                    "offer",
                    int(offer["id"]),
                    student_id,
                    "duplicate_accepted_offer",
                    _json_details(accepted_offer_ids=offer_ids),
                    _timestamp(offer["accepted_date"], offer["created_at"]),
                )
                all_exceptions.append(exception)
                _append_exception(connection, operations, pending_exceptions, exception)
            continue
        offer = accepted_offers[0]
        existing = connection.execute(
            """SELECT offer_id,status_before_acceptance
               FROM student_offer_acceptances WHERE student_id=?""",
            (student_id,),
        ).fetchone()
        timestamp = _timestamp(offer["accepted_date"], offer["created_at"])
        legacy_restore_exception = BackfillException(
            "offer",
            int(offer["id"]),
            student_id,
            "legacy_acceptance_restore_status_unknown",
            _json_details(
                required_manual_resolution=True,
                status_before_acceptance=LEGACY_ACCEPTANCE_STATUS_UNKNOWN,
            ),
            timestamp,
        )
        if existing is None:
            operations.append(
                (
                    """INSERT INTO student_offer_acceptances(
                           student_id,offer_id,status_before_acceptance,accepted_at,updated_at
                       ) VALUES(?,?,?,?,?)""",
                    (
                        student_id,
                        offer["id"],
                        LEGACY_ACCEPTANCE_STATUS_UNKNOWN,
                        timestamp,
                        timestamp,
                    ),
                )
            )
            acceptances_created += 1
            all_exceptions.append(legacy_restore_exception)
            _append_exception(
                connection, operations, pending_exceptions, legacy_restore_exception
            )
        elif (
            int(existing["offer_id"]) == int(offer["id"])
            and existing["status_before_acceptance"]
            == LEGACY_ACCEPTANCE_STATUS_UNKNOWN
        ):
            all_exceptions.append(legacy_restore_exception)
            _append_exception(
                connection, operations, pending_exceptions, legacy_restore_exception
            )
        elif int(existing["offer_id"]) != int(offer["id"]):
            exception = BackfillException(
                "offer",
                int(offer["id"]),
                student_id,
                "current_acceptance_conflict",
                _json_details(existing_offer_id=int(existing["offer_id"])),
                _timestamp(offer["accepted_date"], offer["created_at"]),
            )
            all_exceptions.append(exception)
            _append_exception(connection, operations, pending_exceptions, exception)

    result = BackfillResult(
        report_rows_changed=report_rows_changed,
        canonical_rankings_copied=canonical_rankings_copied,
        offer_rows_changed=offer_rows_changed,
        acceptances_created=acceptances_created,
        exception_rows_written=len(pending_exceptions),
        ambiguous_rows_changed=0,
        exceptions=tuple(sorted(all_exceptions)),
    )
    return _BackfillPlan(operations, result)


def _is_formal_database(database_path: Path) -> bool:
    try:
        return database_path.samefile(_FORMAL_DATABASE)
    except OSError:
        return database_path == _FORMAL_DATABASE.resolve(strict=False)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_safebox_manifest(manifest_path: str | Path) -> Path:
    path = Path(manifest_path).expanduser().resolve(strict=True)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("SAFEBOX manifest cannot be read") from exc
    if manifest.get("complete") is not True:
        raise ValueError("SAFEBOX manifest is incomplete")
    database = manifest.get("database")
    if not isinstance(database, dict) or not database.get("backup_path"):
        raise ValueError("SAFEBOX manifest has no database backup")
    root = Path(str(manifest.get("root") or path.parents[3])).expanduser()
    backup_path = Path(str(database["backup_path"])).expanduser()
    if not backup_path.is_absolute():
        backup_path = root / backup_path
    backup_path = backup_path.resolve(strict=True)
    if not backup_path.is_file():
        raise ValueError("SAFEBOX database backup is missing")
    expected_size = database.get("backup_size")
    if expected_size is None or int(expected_size) != backup_path.stat().st_size:
        raise ValueError("SAFEBOX database backup size does not match")
    with closing(_readonly_connection(backup_path)) as connection:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SAFEBOX database backup quick_check failed")
    return backup_path


def apply_formal_school_plan_offer_014(
    database_path: str | Path,
    *,
    confirmation: str,
    expected_database_sha256: str,
    backup_manifest_path: str | Path,
    formal_database_path: str | Path = _FORMAL_DATABASE,
) -> BackfillResult:
    """Apply the reviewed backfill through an explicit formal-data safety gate."""

    database = Path(database_path).expanduser().resolve(strict=True)
    formal_database = Path(formal_database_path).expanduser().resolve(strict=True)
    try:
        is_formal = database.samefile(formal_database)
    except OSError:
        is_formal = database == formal_database
    if not is_formal:
        raise ValueError("formal backfill target does not match the formal database")
    if confirmation != FORMAL_BACKFILL_CONFIRMATION:
        raise ValueError("formal backfill confirmation is missing or invalid")
    if not expected_database_sha256 or _file_sha256(database) != expected_database_sha256:
        raise ValueError("formal database SHA-256 does not match")
    _validate_safebox_manifest(backup_manifest_path)

    with closing(sqlite3.connect(database, timeout=60)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=60000")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            if _file_sha256(database) != expected_database_sha256:
                raise ValueError("formal database SHA-256 changed before backfill lock")
            plan = _build_plan(connection)
            for statement, parameters in plan.operations:
                connection.execute(statement, parameters)
            foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_rows:
                raise ValueError("formal backfill left foreign-key violations")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("formal backfill quick_check failed")
            connection.commit()
            return plan.result
        except Exception:
            connection.rollback()
            raise


def backfill_school_plan_offer_014(
    database_path: str | Path, apply: bool = False
) -> BackfillResult:
    """Preview or transactionally apply the safe deterministic Migration 014 backfill.

    ``apply=False`` never opens a writable connection.  ``apply=True`` refuses
    the formal CRM database as an additional guard; callers must use a migrated
    temporary or isolated copy and obtain the separately required formal gate.
    """
    database = Path(database_path).expanduser().resolve(strict=True)
    if not database.is_file():
        raise ValueError(f"backfill database is not a file: {database}")
    if not apply:
        with closing(_readonly_connection(database)) as connection:
            return _build_plan(connection).result
    if _is_formal_database(database):
        raise ValueError("applied backfill must not target the formal CRM database")

    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            plan = _build_plan(connection)
            for statement, parameters in plan.operations:
                connection.execute(statement, parameters)
            connection.commit()
            return plan.result
        except Exception:
            connection.rollback()
            raise
