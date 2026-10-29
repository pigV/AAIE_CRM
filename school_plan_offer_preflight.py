"""Read-only legacy-data preflight for school-plan and Offer migration 014."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sqlite3
from typing import Any


REPORT_SCOPE_STATUSES = (
    "unambiguous",
    "no_selected_schools",
    "unresolved_school",
    "mixed_city",
    "mixed_sector",
)


@dataclass(frozen=True)
class PublicScopeCollision:
    student_id: str
    city_id: int
    city_name: str
    city_state: str
    report_ids: tuple[int, ...]


@dataclass(frozen=True)
class PreflightReport:
    report_scope_counts: dict[str, int]
    public_scope_collisions: tuple[PublicScopeCollision, ...]
    offer_match_counts: dict[str, int]
    duplicate_accepted_students: tuple[str, ...]
    orphan_counts: dict[str, int]
    source_database_path: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "report_scope_counts": self.report_scope_counts,
            "public_scope_collisions": [
                asdict(collision) for collision in self.public_scope_collisions
            ],
            "offer_match_counts": self.offer_match_counts,
            "duplicate_accepted_students": list(
                self.duplicate_accepted_students
            ),
            "orphan_counts": self.orphan_counts,
        }


def _normalise_name(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _readonly_connection(database_path: str | Path) -> sqlite3.Connection:
    database_uri = Path(database_path).expanduser().resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(database_uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_columns(connection: sqlite3.Connection) -> dict[str, set[str]]:
    tables = [
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    ]
    return {
        table: {
            row["name"]
            for row in connection.execute(f'PRAGMA table_info("{table}")')
        }
        for table in tables
    }


def _count_orphans(
    connection: sqlite3.Connection,
    columns_by_table: dict[str, set[str]],
) -> dict[str, int]:
    checks = (
        ("reports_missing_students", "school_plan_reports", "student_id", "students", "student_id"),
        ("report_links_missing_reports", "school_plan_report_links", "report_id", "school_plan_reports", "id"),
        ("report_links_missing_students", "school_plan_report_links", "student_id", "students", "student_id"),
        ("confirmations_missing_reports", "school_plan_report_confirmations", "report_id", "school_plan_reports", "id"),
        ("confirmations_missing_students", "school_plan_report_confirmations", "student_id", "students", "student_id"),
        ("confirmations_missing_links", "school_plan_report_confirmations", "link_id", "school_plan_report_links", "id"),
        ("rankings_missing_confirmations", "school_plan_report_school_rankings", "confirmation_id", "school_plan_report_confirmations", "id"),
        ("rankings_missing_reports", "school_plan_report_school_rankings", "report_id", "school_plan_reports", "id"),
        ("rankings_missing_schools", "school_plan_report_school_rankings", "school_id", "schools", "id"),
        ("offers_missing_students", "offers", "student_id", "students", "student_id"),
    )
    counts: dict[str, int] = {}
    for name, source_table, source_column, target_table, target_column in checks:
        if (
            source_table not in columns_by_table
            or target_table not in columns_by_table
            or source_column not in columns_by_table[source_table]
            or target_column not in columns_by_table[target_table]
        ):
            counts[name] = 0
            continue
        row = connection.execute(
            f'''SELECT COUNT(*) AS count
                FROM "{source_table}" AS source
                LEFT JOIN "{target_table}" AS target
                  ON target."{target_column}" = source."{source_column}"
                WHERE source."{source_column}" IS NOT NULL
                  AND target."{target_column}" IS NULL'''
        ).fetchone()
        counts[name] = int(row["count"])
    return counts


def _selected_school_ids(raw_value: Any) -> tuple[tuple[int, ...], bool]:
    try:
        values = json.loads(raw_value or "[]")
    except (TypeError, json.JSONDecodeError):
        return (), True
    if not isinstance(values, list):
        return (), True
    if not values:
        return (), False
    ids: list[int] = []
    for value in values:
        try:
            school_id = int(str(value).strip())
        except (TypeError, ValueError):
            return (), True
        if school_id not in ids:
            ids.append(school_id)
    return tuple(ids), False


def _classify_reports(
    connection: sqlite3.Connection,
) -> tuple[dict[str, int], tuple[PublicScopeCollision, ...]]:
    counts = {status: 0 for status in REPORT_SCOPE_STATUSES}
    public_scopes: dict[tuple[str, int, str, str], list[int]] = {}
    reports = connection.execute(
        "SELECT id, student_id, selected_school_ids FROM school_plan_reports ORDER BY id"
    )
    for report in reports:
        school_ids, invalid_selection = _selected_school_ids(
            report["selected_school_ids"]
        )
        if invalid_selection:
            counts["unresolved_school"] += 1
            continue
        if not school_ids:
            counts["no_selected_schools"] += 1
            continue
        placeholders = ",".join("?" for _ in school_ids)
        schools = connection.execute(
            f'''SELECT s.id, s.city_id, s.sector_key, c.city_name, c.state AS city_state
                FROM schools AS s
                LEFT JOIN school_cities AS c ON c.id = s.city_id
                WHERE s.id IN ({placeholders})''',
            school_ids,
        ).fetchall()
        schools_by_id = {int(school["id"]): school for school in schools}
        if len(schools_by_id) != len(school_ids):
            counts["unresolved_school"] += 1
            continue
        scopes = []
        for school_id in school_ids:
            school = schools_by_id[school_id]
            city_name = str(school["city_name"] or "").strip()
            city_state = str(school["city_state"] or "").strip()
            sector = str(school["sector_key"] or "").strip().casefold()
            if not school["city_id"] or not city_name or not sector in {"public", "private"}:
                counts["unresolved_school"] += 1
                break
            scopes.append((int(school["city_id"]), city_name, city_state, sector))
        else:
            city_scopes = {(scope[0], scope[1], scope[2]) for scope in scopes}
            sector_scopes = {scope[3] for scope in scopes}
            if len(city_scopes) > 1:
                counts["mixed_city"] += 1
            elif len(sector_scopes) > 1:
                counts["mixed_sector"] += 1
            else:
                counts["unambiguous"] += 1
                city_id, city_name, city_state, sector = scopes[0]
                if sector == "public":
                    key = (str(report["student_id"]), city_id, city_name, city_state)
                    public_scopes.setdefault(key, []).append(int(report["id"]))
    collisions = tuple(
        PublicScopeCollision(
            student_id=student_id,
            city_id=city_id,
            city_name=city_name,
            city_state=city_state,
            report_ids=tuple(report_ids),
        )
        for (student_id, city_id, city_name, city_state), report_ids in sorted(
            public_scopes.items()
        )
        if len(report_ids) > 1
    )
    return counts, collisions


def _offer_match_counts(connection: sqlite3.Connection) -> dict[str, int]:
    school_names: dict[str, int] = {}
    for school in connection.execute("SELECT school_name FROM schools"):
        normalised = _normalise_name(school["school_name"])
        if normalised:
            school_names[normalised] = school_names.get(normalised, 0) + 1
    counts = {"matched": 0, "unresolved": 0}
    for offer in connection.execute("SELECT school_name FROM offers"):
        normalised = _normalise_name(offer["school_name"])
        if school_names.get(normalised) == 1:
            counts["matched"] += 1
        else:
            counts["unresolved"] += 1
    return counts


def _duplicate_accepted_students(connection: sqlite3.Connection) -> tuple[str, ...]:
    rows = connection.execute(
        """SELECT student_id
             FROM offers
             WHERE lower(trim(offer_status)) = 'accepted'
             GROUP BY student_id
             HAVING COUNT(*) > 1
             ORDER BY student_id"""
    )
    return tuple(str(row["student_id"]) for row in rows)


def build_preflight_report(database_path: str | Path) -> PreflightReport:
    """Classify legacy migration data without modifying the source database."""
    with _readonly_connection(database_path) as connection:
        columns_by_table = _table_columns(connection)
        required_tables = {
            "school_plan_reports",
            "schools",
            "school_cities",
            "offers",
        }
        missing_tables = sorted(required_tables - columns_by_table.keys())
        if missing_tables:
            raise ValueError(
                "preflight requires tables: " + ", ".join(missing_tables)
            )
        report_scope_counts, public_scope_collisions = _classify_reports(
            connection
        )
        return PreflightReport(
            report_scope_counts=report_scope_counts,
            public_scope_collisions=public_scope_collisions,
            offer_match_counts=_offer_match_counts(connection),
            duplicate_accepted_students=_duplicate_accepted_students(connection),
            orphan_counts=_count_orphans(connection, columns_by_table),
            source_database_path=Path(database_path).expanduser().resolve(),
        )


def _target_is_source_database(
    source_database_path: Path, target_path: Path
) -> bool:
    source = source_database_path.expanduser().resolve(strict=True)
    target = target_path.expanduser()
    if target.resolve(strict=False) == source:
        return True
    try:
        return target.exists() and os.path.samefile(source, target)
    except OSError:
        return False


def write_preflight_json(report: PreflightReport, target_path: str | Path) -> Path:
    """Write a deterministic JSON rendering of a preflight report."""
    target = Path(target_path)
    if report.source_database_path is None:
        raise ValueError("preflight report has no source database path")
    if _target_is_source_database(report.source_database_path, target):
        raise ValueError("preflight target must not refer to the source database")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a read-only school-plan and Offer migration preflight."
    )
    parser.add_argument("database_path", type=Path)
    parser.add_argument("target_path", type=Path)
    arguments = parser.parse_args(argv)
    write_preflight_json(
        build_preflight_report(arguments.database_path), arguments.target_path
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
