import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from school_plan_offer_preflight import (
    PublicScopeCollision,
    build_preflight_report,
    main,
    write_preflight_json,
)


class SchoolPlanOfferPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database_path = self.root / "preflight.db"
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(
                """
                CREATE TABLE students (student_id TEXT PRIMARY KEY);
                CREATE TABLE school_cities (
                    id INTEGER PRIMARY KEY,
                    city_name TEXT NOT NULL,
                    state TEXT NOT NULL
                );
                CREATE TABLE schools (
                    id INTEGER PRIMARY KEY,
                    school_name TEXT NOT NULL,
                    city_id INTEGER,
                    sector_key TEXT NOT NULL
                );
                CREATE TABLE school_plan_reports (
                    id INTEGER PRIMARY KEY,
                    student_id TEXT NOT NULL,
                    selected_school_ids TEXT NOT NULL
                );
                CREATE TABLE school_plan_report_links (
                    id INTEGER PRIMARY KEY,
                    report_id INTEGER NOT NULL,
                    student_id TEXT NOT NULL
                );
                CREATE TABLE school_plan_report_confirmations (
                    id INTEGER PRIMARY KEY,
                    report_id INTEGER NOT NULL,
                    student_id TEXT NOT NULL,
                    link_id INTEGER NOT NULL
                );
                CREATE TABLE school_plan_report_school_rankings (
                    id INTEGER PRIMARY KEY,
                    confirmation_id INTEGER NOT NULL,
                    report_id INTEGER NOT NULL,
                    school_id INTEGER
                );
                CREATE TABLE offers (
                    id INTEGER PRIMARY KEY,
                    student_id TEXT NOT NULL,
                    school_name TEXT NOT NULL,
                    offer_status TEXT NOT NULL
                );
                """
            )
            connection.executemany(
                "INSERT INTO students(student_id) VALUES(?)",
                [
                    ("PUB001",),
                    ("MIX001",),
                    ("MISS001",),
                    ("EMPTY001",),
                    ("CITY001",),
                    ("UNIQUE001",),
                    ("DUP001",),
                ],
            )
            connection.executemany(
                "INSERT INTO school_cities(id,city_name,state) VALUES(?,?,?)",
                [(1, "Melbourne", "VIC"), (2, "Sydney", "NSW")],
            )
            connection.executemany(
                "INSERT INTO schools(id,school_name,city_id,sector_key) VALUES(?,?,?,?)",
                [
                    (1, "  Public College  ", 1, "public"),
                    (2, "Private College", 1, "private"),
                    (3, "Sydney Public College", 2, "public"),
                ],
            )
            connection.executemany(
                "INSERT INTO school_plan_reports(id,student_id,selected_school_ids) VALUES(?,?,?)",
                [
                    (1, "PUB001", "[1]"),
                    (2, "MIX001", "[1, 2]"),
                    (3, "MISS001", "[999]"),
                    (4, "EMPTY001", "[]"),
                    (5, "CITY001", "[1, 3]"),
                ],
            )
            connection.executemany(
                "INSERT INTO offers(id,student_id,school_name,offer_status) VALUES(?,?,?,?)",
                [
                    (1, "UNIQUE001", " public    college ", " accepted "),
                    (2, "DUP001", "Public College", "accepted"),
                    (3, "DUP001", "Private College", "ACCEPTED"),
                    (4, "DUP001", "Private College", "signed"),
                    (5, "UNIQUE001", "Unknown College", "received"),
                ],
            )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_classifies_reports_offers_and_duplicate_acceptances_without_writing_source(self):
        before_size = self.database_path.stat().st_size
        with sqlite3.connect(self.database_path) as connection:
            before_schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]

        report = build_preflight_report(self.database_path)

        self.assertEqual(report.report_scope_counts["unambiguous"], 1)
        self.assertEqual(report.report_scope_counts["mixed_sector"], 1)
        self.assertEqual(report.report_scope_counts["unresolved_school"], 1)
        self.assertEqual(report.report_scope_counts["no_selected_schools"], 1)
        self.assertEqual(report.report_scope_counts["mixed_city"], 1)
        self.assertEqual(report.duplicate_accepted_students, ("DUP001",))
        self.assertEqual(report.offer_match_counts, {"matched": 4, "unresolved": 1})
        self.assertEqual(report.public_scope_collisions, ())
        self.assertTrue(all(count == 0 for count in report.orphan_counts.values()))
        self.assertEqual(self.database_path.stat().st_size, before_size)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA schema_version").fetchone()[0],
                before_schema_version,
            )

    def test_write_preflight_json_serializes_a_report_to_the_requested_path(self):
        report = build_preflight_report(self.database_path)
        target_path = self.root / "preflight.json"

        written_path = write_preflight_json(report, target_path)

        self.assertEqual(written_path, target_path)
        self.assertEqual(
            json.loads(target_path.read_text(encoding="utf-8"))["report_scope_counts"],
            {
                "mixed_city": 1,
                "mixed_sector": 1,
                "no_selected_schools": 1,
                "unambiguous": 1,
                "unresolved_school": 1,
            },
        )

    def assert_rejected_target_preserves_source(self, target_path, writer):
        before_bytes = self.database_path.read_bytes()
        with sqlite3.connect(self.database_path) as connection:
            before_schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]

        with self.assertRaisesRegex(ValueError, "source database"):
            writer(target_path)

        self.assertEqual(self.database_path.read_bytes(), before_bytes)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA schema_version").fetchone()[0],
                before_schema_version,
            )

    def test_write_rejects_the_source_database_as_the_json_target(self):
        report = build_preflight_report(self.database_path)

        self.assert_rejected_target_preserves_source(
            self.database_path,
            lambda target_path: write_preflight_json(report, target_path),
        )

    def test_cli_rejects_an_existing_symbolic_link_to_the_source_database(self):
        target_path = self.root / "source-alias.json"
        target_path.symlink_to(self.database_path)

        self.assert_rejected_target_preserves_source(
            target_path,
            lambda output_path: main([str(self.database_path), str(output_path)]),
        )

    def test_write_rejects_an_existing_hard_link_to_the_source_database(self):
        target_path = self.root / "source-hard-link.json"
        os.link(self.database_path, target_path)
        report = build_preflight_report(self.database_path)

        self.assert_rejected_target_preserves_source(
            target_path,
            lambda output_path: write_preflight_json(report, output_path),
        )

    def test_groups_only_unambiguous_public_reports_by_student_and_city(self):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO school_plan_reports(id,student_id,selected_school_ids) VALUES(6,'PUB001','[1]')"
            )
            connection.execute(
                "INSERT INTO school_plan_reports(id,student_id,selected_school_ids) VALUES(7,'PUB001','[999]')"
            )

        report = build_preflight_report(self.database_path)

        self.assertEqual(
            report.public_scope_collisions,
            (
                PublicScopeCollision(
                    student_id="PUB001",
                    city_id=1,
                    city_name="Melbourne",
                    city_state="VIC",
                    report_ids=(1, 6),
                ),
            ),
        )


if __name__ == "__main__":
    unittest.main()
