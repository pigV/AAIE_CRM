from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import unittest

from database import Database
import migration_runner
from school_plan_offer_backfill import (
    FORMAL_BACKFILL_CONFIRMATION,
    LEGACY_ACCEPTANCE_STATUS_UNKNOWN,
    apply_formal_school_plan_offer_014,
    backfill_school_plan_offer_014,
)


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "migrations" / "20260806_014_school_plan_offer_lifecycle.sql"
FORWARD_CHECK_PATH = (
    ROOT
    / "migrations"
    / "checks"
    / "20260806_014_school_plan_offer_lifecycle_forward_check.sql"
)


class SchoolPlanOfferMigration014Tests(unittest.TestCase):
    expected_report_columns = {
        "city_id",
        "city_name_snapshot",
        "plan_sector",
        "is_current",
        "superseded_by_report_id",
        "scope_review_status",
    }
    expected_offer_columns = {
        "school_id",
        "city_id",
        "city_name_snapshot",
        "sector_key",
        "classification_status",
    }
    expected_tables = {
        "school_plan_confirmed_schools",
        "school_plan_school_submission_events",
        "student_offer_acceptances",
        "offer_acceptance_history",
        "school_plan_offer_migration_exceptions",
    }

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.database_path = self.root / "legacy-school-plan-offer.db"
        self._initialize_pre_014_database()

    def _initialize_pre_014_database(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE students(student_id TEXT PRIMARY KEY);
                CREATE TABLE school_cities(
                    id INTEGER PRIMARY KEY, city_name TEXT, state TEXT
                );
                CREATE TABLE schools(
                    id INTEGER PRIMARY KEY, school_name TEXT,
                    city_id INTEGER REFERENCES school_cities(id), sector_key TEXT
                );
                CREATE TABLE school_plan_reports(
                    id INTEGER PRIMARY KEY,
                    student_id TEXT NOT NULL REFERENCES students(student_id),
                    selected_school_ids TEXT NOT NULL DEFAULT '[]',
                    report_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT '2026-08-06T00:00:00+10:00',
                    file_path TEXT
                );
                CREATE TABLE school_plan_report_confirmations(
                    id INTEGER PRIMARY KEY,
                    report_id INTEGER NOT NULL REFERENCES school_plan_reports(id),
                    student_id TEXT REFERENCES students(student_id),
                    confirmed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT '2026-08-06T00:00:00+10:00'
                );
                CREATE TABLE school_plan_report_school_rankings(
                    id INTEGER PRIMARY KEY,
                    confirmation_id INTEGER NOT NULL REFERENCES school_plan_report_confirmations(id),
                    report_id INTEGER NOT NULL REFERENCES school_plan_reports(id),
                    school_id INTEGER, school_name TEXT NOT NULL,
                    rank_order INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE offers(
                    id INTEGER PRIMARY KEY,
                    student_id TEXT NOT NULL REFERENCES students(student_id),
                    school_name TEXT NOT NULL DEFAULT '',
                    offer_status TEXT NOT NULL DEFAULT 'received',
                    accepted_date TEXT,
                    accepted_by INTEGER,
                    created_at TEXT NOT NULL DEFAULT '2026-08-06T00:00:00+10:00'
                );
                INSERT INTO students(student_id) VALUES('student-1'),('student-2');
                INSERT INTO school_cities(id,city_name,state) VALUES(1,'Melbourne','VIC');
                INSERT INTO schools(id,school_name,city_id,sector_key)
                    VALUES(1,'Fixture School',1,'public'),(2,'Other School',1,'public');
                INSERT INTO school_plan_reports(id,student_id) VALUES(1,'student-1'),(2,'student-2');
                INSERT INTO school_plan_report_confirmations(id,report_id) VALUES(1,1),(2,2);
                INSERT INTO offers(id,student_id) VALUES(1,'student-1'),(2,'student-2');
                """
            )

    def _stage_migration_package(self):
        package = self.root / "migration-package"
        checks = package / "checks"
        checks.mkdir(parents=True)
        shutil.copy2(MIGRATION_PATH, package / MIGRATION_PATH.name)
        shutil.copy2(FORWARD_CHECK_PATH, checks / FORWARD_CHECK_PATH.name)
        return package

    def _migrate(self):
        self.assertTrue(MIGRATION_PATH.is_file())
        self.assertTrue(FORWARD_CHECK_PATH.is_file())
        return migration_runner.apply_migrations(
            self.database_path,
            self._stage_migration_package(),
            applied_by="migration-014-contract-test",
            source_commit="test",
            formal_database_path=None,
        )

    def _seed_backfill_fixture(self):
        """Add legacy data covering only deterministic migration decisions."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE school_plan_report_viewers(
                    id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL, student_id TEXT NOT NULL
                );
                CREATE TABLE school_plan_report_links(
                    id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL, student_id TEXT NOT NULL
                );
                CREATE TABLE school_plan_report_events(
                    id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL, event_type TEXT NOT NULL
                );
                CREATE TABLE school_plan_report_view_logs(
                    id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL, event_type TEXT NOT NULL
                );
                """
            )
            connection.executemany(
                "INSERT INTO students(student_id) VALUES(?)",
                [("S001",), ("S002",), ("S003",), ("20260035",), ("SIGNED001",)],
            )
            connection.executemany(
                "INSERT INTO schools(id,school_name,city_id,sector_key) VALUES(?,?,?,?)",
                [
                    (10, "Melbourne Public One", 1, "public"),
                    (11, "Melbourne Public Two", 1, "public"),
                    (12, "Melbourne Private One", 1, "private"),
                    (13, "Duplicate Offer School", 1, "public"),
                    (14, "Duplicate Offer School", 1, "public"),
                ],
            )
            connection.executemany(
                """INSERT INTO school_plan_reports(
                       id,student_id,selected_school_ids,report_version,created_at,file_path
                   ) VALUES(?,?,?,?,?,?)""",
                [
                    (10, "S001", "[10]", 1, "2026-01-01T09:00:00+10:00", "reports/10.pdf"),
                    (11, "S001", "[11]", 2, "2026-02-01T09:00:00+10:00", "reports/11.pdf"),
                    (12, "S001", "[12]", 1, "2026-03-01T09:00:00+10:00", "reports/12.pdf"),
                    (13, "S001", "[10,12]", 3, "2026-04-01T09:00:00+10:00", "reports/13.pdf"),
                    (14, "S002", "[]", 1, "2026-05-01T09:00:00+10:00", "reports/14.pdf"),
                    (15, "S002", "[999]", 1, "2026-06-01T09:00:00+10:00", "reports/15.pdf"),
                    (16, "S002", "[10]", 9, "2026-07-01T09:00:00+10:00", "reports/16.pdf"),
                    (17, "S002", "[11]", 9, "2026-08-01T09:00:00+10:00", "reports/17.pdf"),
                    (18, "S003", "[10]", 5, "2026-09-01T09:00:00+10:00", "reports/18.pdf"),
                    (19, "S003", "[11]", 5, "2026-09-01T09:00:00+10:00", "reports/19.pdf"),
                ],
            )
            connection.executemany(
                """INSERT INTO school_plan_report_confirmations(
                       id,report_id,student_id,confirmed_at,created_at
                   ) VALUES(?,?,?,?,?)""",
                [
                    (10, 10, "S001", "2026-01-02T09:00:00+10:00", "2026-01-02T09:00:00+10:00"),
                    (11, 11, "S001", "2026-02-02T09:00:00+10:00", "2026-02-02T09:00:00+10:00"),
                ],
            )
            connection.executemany(
                """INSERT INTO school_plan_report_school_rankings(
                       id,confirmation_id,report_id,school_id,school_name,rank_order,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                [
                    (10, 10, 10, 10, "Melbourne Public One", 1, "2026-01-02T09:00:00+10:00"),
                    (11, 11, 11, 11, "Melbourne Public Two", 1, "2026-02-02T09:00:00+10:00"),
                    (12, 10, 11, 11, "Mismatched confirmation", 2, "2026-02-02T09:00:00+10:00"),
                ],
            )
            connection.executemany(
                "INSERT INTO school_plan_report_viewers(id,report_id,student_id) VALUES(?,?,?)",
                [(10, 10, "S001"), (11, 11, "S001")],
            )
            connection.executemany(
                "INSERT INTO school_plan_report_links(id,report_id,student_id) VALUES(?,?,?)",
                [(10, 10, "S001"), (11, 11, "S001")],
            )
            connection.executemany(
                "INSERT INTO school_plan_report_events(id,report_id,event_type) VALUES(?,?,?)",
                [(10, 10, "wechat_authenticated"), (11, 11, "confirmed")],
            )
            connection.executemany(
                "INSERT INTO school_plan_report_view_logs(id,report_id,event_type) VALUES(?,?,?)",
                [(10, 10, "view"), (11, 11, "download")],
            )
            connection.executemany(
                """INSERT INTO offers(
                       id,student_id,school_name,offer_status,accepted_date,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                [
                    (10, "S001", " melbourne  public two ", "accepted", "2026-02-03T09:00:00+10:00", "2026-02-01T09:00:00+10:00"),
                    (11, "20260035", "Melbourne Public One", "accepted", "2026-02-03T09:00:00+10:00", "2026-02-01T09:00:00+10:00"),
                    (12, "20260035", "Melbourne Public Two", "accepted", "2026-02-04T09:00:00+10:00", "2026-02-01T09:00:00+10:00"),
                    (13, "SIGNED001", "Melbourne Public One", "signed", None, "2026-02-01T09:00:00+10:00"),
                    (14, "S002", "Missing Offer School", "received", None, "2026-02-01T09:00:00+10:00"),
                    (15, "S002", "Duplicate Offer School", "received", None, "2026-02-01T09:00:00+10:00"),
                ],
            )
            connection.commit()

    def _snapshot_hash(self):
        return hashlib.sha256(self.database_path.read_bytes()).hexdigest()

    def _logical_dump(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            return "\n".join(connection.iterdump())

    def test_runner_applies_schema_contract_and_is_idempotent(self):
        self.assertTrue(
            MIGRATION_PATH.is_file(),
            "migration 014 SQL must exist before its contract can pass",
        )
        self.assertTrue(
            FORWARD_CHECK_PATH.is_file(),
            "migration 014 forward check must exist before its contract can pass",
        )
        package = self._stage_migration_package()

        first = migration_runner.apply_migrations(
            self.database_path,
            package,
            applied_by="migration-014-contract-test",
            source_commit="test",
            formal_database_path=None,
        )
        second = migration_runner.apply_migrations(
            self.database_path,
            package,
            applied_by="migration-014-contract-test",
            source_commit="test",
            formal_database_path=None,
        )

        self.assertEqual(first["applied"], ["20260806_014"])
        self.assertEqual(second["applied"], [])
        with closing(sqlite3.connect(self.database_path)) as connection:
            report_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(school_plan_reports)")
            }
            offer_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(offers)")
            }
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(self.expected_report_columns <= report_columns)
            self.assertTrue(self.expected_offer_columns <= offer_columns)
            self.assertTrue(self.expected_tables <= tables)
            self.assertEqual(
                connection.execute(
                    "SELECT is_current,scope_review_status FROM school_plan_reports WHERE id=1"
                ).fetchone(),
                (0, "pending"),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT classification_status FROM offers WHERE id=1"
                ).fetchone(),
                ("unresolved",),
            )
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone(), ("ok",))

    def test_schema_enforces_current_public_and_current_acceptance_contracts(self):
        self._migrate()

        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "UPDATE school_plan_reports SET city_id=1,plan_sector='public',is_current=1 WHERE id=1"
            )
            connection.execute(
                "INSERT INTO school_plan_reports(id,student_id) VALUES(3,'student-1')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE school_plan_reports
                       SET city_id=1,plan_sector='public',is_current=1 WHERE id=3"""
                )
            connection.execute(
                """UPDATE school_plan_reports
                   SET city_id=1,plan_sector='private',is_current=1 WHERE id=3"""
            )
            index_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                ("uq_school_plan_current_public_city",),
            ).fetchone()[0]
            self.assertIn("ON school_plan_reports(student_id, city_id)", index_sql)
            self.assertIn("WHERE plan_sector='public' AND is_current=1", index_sql)

            acceptance_columns = {
                row[1]: row for row in connection.execute(
                    "PRAGMA table_info(student_offer_acceptances)"
                )
            }
            self.assertEqual(acceptance_columns["student_id"][5], 1)
            self.assertEqual(
                set(acceptance_columns),
                {
                    "student_id",
                    "offer_id",
                    "status_before_acceptance",
                    "accepted_at",
                    "accepted_by_user_id",
                    "accepted_by_name",
                    "updated_at",
                },
            )
            connection.execute(
                """INSERT INTO student_offer_acceptances(
                       student_id,offer_id,status_before_acceptance,accepted_at,updated_at
                   ) VALUES('student-1',1,'received','2026-08-06T00:00:00+10:00',
                            '2026-08-06T00:00:00+10:00')"""
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO student_offer_acceptances(
                           student_id,offer_id,status_before_acceptance,accepted_at,updated_at
                       ) VALUES('student-1',1,'draft','2026-08-06T00:01:00+10:00',
                                '2026-08-06T00:01:00+10:00')"""
                )

            history_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(offer_acceptance_history)")
            }
            self.assertTrue(
                {
                    "student_id",
                    "previous_offer_id",
                    "new_offer_id",
                    "previous_offer_restored_status",
                    "new_offer_previous_status",
                    "accepted_by_user_id",
                    "accepted_by_name",
                    "accepted_at",
                    "coe_present",
                    "coe_warning_acknowledged",
                }
                <= history_columns
            )
            triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
            self.assertTrue(
                {
                    "school_plan_school_submission_events_no_update",
                    "school_plan_school_submission_events_no_delete",
                    "offer_acceptance_history_no_update",
                    "offer_acceptance_history_no_delete",
                }
                <= triggers
            )

    def test_ownership_guards_reject_cross_report_and_cross_student_references(self):
        self._migrate()

        with closing(sqlite3.connect(self.database_path)) as connection:
            valid_confirmed_school = (
                1, 1, 1, "student-1", None, "Manual school",
                1, "Melbourne", "public", "ranked", None, 1,
                "not_submitted", None, None, None,
                "2026-08-06T00:00:00+10:00", "2026-08-06T00:00:00+10:00",
            )
            connection.execute(
                """INSERT INTO school_plan_confirmed_schools(
                       id,confirmation_id,report_id,student_id,school_id,
                       school_name_snapshot,city_id,city_name_snapshot,plan_sector,
                       decision_mode,rank_order,is_selected,submission_status,
                       submitted_at,submitted_by_user_id,submitted_by_name,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                valid_confirmed_school,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO school_plan_confirmed_schools(
                           confirmation_id,report_id,student_id,school_name_snapshot,
                           plan_sector,decision_mode,created_at,updated_at
                       ) VALUES(2,1,'student-1','Wrong confirmation','public','ranked',
                                '2026-08-06T00:00:00+10:00','2026-08-06T00:00:00+10:00')"""
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE school_plan_confirmed_schools SET confirmation_id=2 WHERE id=1"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE school_plan_confirmed_schools SET student_id='student-2' WHERE id=1"
                )

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO student_offer_acceptances(
                           student_id,offer_id,status_before_acceptance,accepted_at,updated_at
                       ) VALUES('student-1',2,'received','2026-08-06T00:00:00+10:00',
                                '2026-08-06T00:00:00+10:00')"""
                )
            connection.execute(
                """INSERT INTO student_offer_acceptances(
                       student_id,offer_id,status_before_acceptance,accepted_at,updated_at
                   ) VALUES('student-1',1,'received','2026-08-06T00:00:00+10:00',
                            '2026-08-06T00:00:00+10:00')"""
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE student_offer_acceptances SET offer_id=2 WHERE student_id='student-1'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO offer_acceptance_history(
                           student_id,previous_offer_id,new_offer_id,new_offer_previous_status,
                           accepted_at,coe_present,coe_warning_acknowledged,created_at
                       ) VALUES('student-1',NULL,2,'received','2026-08-06T00:00:00+10:00',0,0,
                                '2026-08-06T00:00:00+10:00')"""
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO offer_acceptance_history(
                           student_id,previous_offer_id,new_offer_id,new_offer_previous_status,
                           accepted_at,coe_present,coe_warning_acknowledged,created_at
                       ) VALUES('student-1',2,1,'received','2026-08-06T00:00:00+10:00',0,0,
                                '2026-08-06T00:00:00+10:00')"""
                )

    def test_submission_status_transition_writes_one_immutable_owned_event(self):
        self._migrate()

        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """INSERT INTO school_plan_confirmed_schools(
                       id,confirmation_id,report_id,student_id,school_name_snapshot,
                       plan_sector,decision_mode,created_at,updated_at
                   ) VALUES(1,1,1,'student-1','Fixture School','public','ranked',
                            '2026-08-06T00:00:00+10:00','2026-08-06T00:00:00+10:00')"""
            )
            connection.execute(
                """UPDATE school_plan_confirmed_schools
                   SET submission_status='submitted',
                       submitted_at='2026-08-06T01:00:00+10:00',
                       submitted_by_name='Staff A',
                       updated_at='2026-08-06T01:00:00+10:00'
                   WHERE id=1"""
            )
            connection.execute(
                """UPDATE school_plan_confirmed_schools
                   SET submitted_by_name='Staff B',updated_at='2026-08-06T01:05:00+10:00'
                   WHERE id=1"""
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE school_plan_confirmed_schools
                       SET submission_status='not_submitted',
                           updated_at='2026-08-06T02:00:00+10:00'
                       WHERE id=1"""
                )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM school_plan_school_submission_events"
                ).fetchone()[0],
                1,
            )
            connection.execute(
                """UPDATE school_plan_confirmed_schools
                   SET submission_status='not_submitted',submitted_at=NULL,
                       submitted_by_user_id=NULL,submitted_by_name=NULL,
                       updated_at='2026-08-06T02:00:00+10:00'
                   WHERE id=1"""
            )
            events = connection.execute(
                """SELECT previous_submission_status,new_submission_status,submitted_at,
                          submitted_by_name,occurred_at
                   FROM school_plan_school_submission_events"""
            ).fetchall()
            self.assertEqual(
                events,
                [
                    ("not_submitted", "submitted", "2026-08-06T01:00:00+10:00", "Staff A", "2026-08-06T01:00:00+10:00"),
                    ("submitted", "not_submitted", None, None, "2026-08-06T02:00:00+10:00"),
                ],
            )
            self.assertEqual(
                connection.execute(
                    """SELECT submission_status,submitted_at,submitted_by_user_id,submitted_by_name
                       FROM school_plan_confirmed_schools WHERE id=1"""
                ).fetchone(),
                ("not_submitted", None, None, None),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO school_plan_school_submission_events(
                           confirmed_school_id,student_id,report_id,previous_submission_status,
                           new_submission_status,occurred_at,created_at
                       ) VALUES(1,'student-2',1,'submitted','not_submitted',
                                '2026-08-06T02:00:00+10:00','2026-08-06T02:00:00+10:00')"""
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE school_plan_school_submission_events SET occurred_at='changed'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM school_plan_school_submission_events")

    def test_parent_identity_guards_preserve_canonical_and_audit_ownership(self):
        self._migrate()

        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("INSERT INTO offers(id,student_id) VALUES(3,'student-1')")
            connection.execute(
                """INSERT INTO student_offer_acceptances(
                       student_id,offer_id,status_before_acceptance,accepted_at,updated_at
                   ) VALUES('student-1',1,'received','2026-08-06T00:00:00+10:00',
                            '2026-08-06T00:00:00+10:00')"""
            )
            connection.execute(
                """INSERT INTO offer_acceptance_history(
                       student_id,previous_offer_id,new_offer_id,new_offer_previous_status,
                       accepted_at,coe_present,coe_warning_acknowledged,created_at
                   ) VALUES('student-1',NULL,3,'received','2026-08-06T00:00:00+10:00',0,0,
                            '2026-08-06T00:00:00+10:00')"""
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE offers SET student_id='student-2' WHERE id=1")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE offers SET student_id='student-2' WHERE id=3")

            connection.execute(
                "INSERT INTO school_plan_reports(id,student_id) VALUES(3,'student-1')"
            )
            connection.execute(
                "INSERT INTO school_plan_report_confirmations(id,report_id) VALUES(3,3)"
            )
            connection.execute(
                """INSERT INTO school_plan_confirmed_schools(
                       id,confirmation_id,report_id,student_id,school_name_snapshot,
                       plan_sector,decision_mode,created_at,updated_at
                   ) VALUES(3,3,3,'student-1','Audit school','public','ranked',
                            '2026-08-06T00:00:00+10:00','2026-08-06T00:00:00+10:00')"""
            )
            connection.execute(
                """UPDATE school_plan_confirmed_schools
                   SET submission_status='submitted',submitted_at='2026-08-06T01:00:00+10:00',
                       updated_at='2026-08-06T01:00:00+10:00'
                   WHERE id=3"""
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE school_plan_reports SET student_id='student-2' WHERE id=3"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE school_plan_report_confirmations SET report_id=2 WHERE id=3"
                )

            connection.execute(
                "INSERT INTO school_plan_reports(id,student_id) VALUES(4,'student-2')"
            )
            connection.execute(
                "INSERT INTO school_plan_report_confirmations(id,report_id) VALUES(4,4)"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE school_plan_confirmed_schools
                       SET confirmation_id=4,report_id=4,student_id='student-2'
                       WHERE id=3"""
                )

            connection.execute(
                "UPDATE school_plan_confirmed_schools SET school_name_snapshot='Renamed school' WHERE id=3"
            )
            connection.execute(
                "UPDATE school_plan_reports SET city_name_snapshot='Melbourne' WHERE id=4"
            )
            self.assertEqual(
                connection.execute(
                    "SELECT school_name_snapshot FROM school_plan_confirmed_schools WHERE id=3"
                ).fetchone()[0],
                "Renamed school",
            )

    def test_retained_history_blocks_parent_deletion_and_null_school_duplicates(self):
        self._migrate()

        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """INSERT INTO school_plan_confirmed_schools(
                       id,confirmation_id,report_id,student_id,school_name_snapshot,
                       plan_sector,decision_mode,created_at,updated_at
                   ) VALUES(1,1,1,'student-1','Manual school','public','ranked',
                            '2026-08-06T00:00:00+10:00','2026-08-06T00:00:00+10:00')"""
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO school_plan_confirmed_schools(
                           confirmation_id,report_id,student_id,school_name_snapshot,
                           plan_sector,decision_mode,created_at,updated_at
                       ) VALUES(1,1,'student-1','Manual school','public','ranked',
                                '2026-08-06T00:01:00+10:00','2026-08-06T00:01:00+10:00')"""
                )
            connection.execute(
                """INSERT INTO school_plan_confirmed_schools(
                       id,confirmation_id,report_id,student_id,school_id,school_name_snapshot,
                       plan_sector,decision_mode,created_at,updated_at
                   ) VALUES(2,1,1,'student-1',1,'Library school','public','ranked',
                            '2026-08-06T00:01:00+10:00','2026-08-06T00:01:00+10:00')"""
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO school_plan_confirmed_schools(
                           confirmation_id,report_id,student_id,school_id,school_name_snapshot,
                           plan_sector,decision_mode,created_at,updated_at
                       ) VALUES(1,1,'student-1',1,'Library school','public','ranked',
                                '2026-08-06T00:02:00+10:00','2026-08-06T00:02:00+10:00')"""
                )
            connection.execute(
                """UPDATE school_plan_confirmed_schools
                   SET submission_status='submitted',updated_at='2026-08-06T01:00:00+10:00'
                   WHERE id=1"""
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM school_plan_reports WHERE id=1")
            connection.execute(
                """INSERT INTO offer_acceptance_history(
                       student_id,previous_offer_id,new_offer_id,new_offer_previous_status,
                       accepted_at,coe_present,coe_warning_acknowledged,created_at
                   ) VALUES('student-1',NULL,1,'received','2026-08-06T01:00:00+10:00',0,0,
                            '2026-08-06T01:00:00+10:00')"""
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM offers WHERE id=1")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE offer_acceptance_history SET accepted_at='changed'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM offer_acceptance_history")

    def test_backfill_dry_run_and_apply_are_deterministic_and_preserve_legacy_data(self):
        self._migrate()
        self._seed_backfill_fixture()
        before_hash = self._snapshot_hash()
        before_counts = {}
        with closing(sqlite3.connect(self.database_path)) as connection:
            for table in (
                "school_plan_reports",
                "school_plan_report_viewers",
                "school_plan_report_links",
                "school_plan_report_events",
                "school_plan_report_view_logs",
                "school_plan_report_confirmations",
                "school_plan_report_school_rankings",
                "offers",
            ):
                before_counts[table] = connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]

        dry_run = backfill_school_plan_offer_014(self.database_path, apply=False)

        self.assertEqual(self._snapshot_hash(), before_hash)
        self.assertEqual(dry_run.ambiguous_rows_changed, 0)

        applied = backfill_school_plan_offer_014(self.database_path, apply=True)

        self.assertEqual(applied, dry_run)
        self.assertEqual(applied.ambiguous_rows_changed, 0)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM school_plan_reports
                       WHERE student_id='S001' AND city_id=1
                         AND plan_sector='public' AND is_current=1"""
                ).fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute(
                    """SELECT id,is_current,superseded_by_report_id
                       FROM school_plan_reports WHERE id IN (10,11) ORDER BY id"""
                ).fetchall(),
                [(10, 0, 11), (11, 1, None)],
            )
            self.assertEqual(
                connection.execute(
                    """SELECT is_current,superseded_by_report_id
                       FROM school_plan_reports WHERE id=12"""
                ).fetchone(),
                (1, None),
            )
            self.assertEqual(
                connection.execute(
                    """SELECT id,city_id,plan_sector,is_current,superseded_by_report_id,
                              scope_review_status
                       FROM school_plan_reports WHERE id IN (13,14,15) ORDER BY id"""
                ).fetchall(),
                [
                    (13, None, None, 0, None, "pending"),
                    (14, None, None, 0, None, "pending"),
                    (15, None, None, 0, None, "pending"),
                ],
            )
            self.assertEqual(
                connection.execute(
                    """SELECT id,is_current,superseded_by_report_id
                       FROM school_plan_reports WHERE id IN (16,17,18,19) ORDER BY id"""
                ).fetchall(),
                [(16, 0, 17), (17, 1, None), (18, 0, 19), (19, 1, None)],
            )
            self.assertEqual(
                connection.execute(
                    """SELECT report_id,school_id,school_name_snapshot,rank_order
                       FROM school_plan_confirmed_schools
                       WHERE report_id IN (10,11) ORDER BY report_id"""
                ).fetchall(),
                [(10, 10, "Melbourne Public One", 1), (11, 11, "Melbourne Public Two", 1)],
            )
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM school_plan_offer_migration_exceptions
                       WHERE entity_type='school_plan_ranking' AND entity_id=12
                         AND exception_type='ranking_ownership_mismatch'"""
                ).fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute(
                    """SELECT offer_id,status_before_acceptance
                       FROM student_offer_acceptances WHERE student_id='S001'"""
                ).fetchone(),
                (10, LEGACY_ACCEPTANCE_STATUS_UNKNOWN),
            )
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM school_plan_offer_migration_exceptions
                       WHERE entity_type='offer' AND entity_id=10
                         AND exception_type='legacy_acceptance_restore_status_unknown'"""
                ).fetchone(),
                (1,),
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT offer_id FROM student_offer_acceptances WHERE student_id='20260035'"
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT offer_id FROM student_offer_acceptances WHERE student_id='SIGNED001'"
                ).fetchone()
            )
            self.assertEqual(
                connection.execute(
                    "SELECT school_id,classification_status FROM offers WHERE id=10"
                ).fetchone(),
                (11, "resolved"),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT school_id,classification_status FROM offers WHERE id=15"
                ).fetchone(),
                (None, "ambiguous"),
            )
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM school_plan_offer_migration_exceptions
                       WHERE entity_type='offer' AND entity_id=11
                         AND exception_type='duplicate_accepted_offer'"""
                ).fetchone(),
                (1,),
            )
            for table, count in before_counts.items():
                self.assertEqual(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    count,
                )

        after_first_apply = self._logical_dump()
        repeated = backfill_school_plan_offer_014(self.database_path, apply=True)
        self.assertEqual(self._logical_dump(), after_first_apply)
        self.assertEqual(repeated.rows_changed, 0)
        self.assertEqual(repeated.exception_rows_written, 0)
        self.assertEqual(repeated.exceptions, dry_run.exceptions)

    def test_backfill_rolls_back_all_changes_when_a_write_fails(self):
        self._migrate()
        self._seed_backfill_fixture()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """CREATE TRIGGER abort_backfill_report_two
                   BEFORE UPDATE OF plan_sector ON school_plan_reports
                   WHEN NEW.id=11
                   BEGIN
                     SELECT RAISE(ABORT, 'forced Task 3 rollback');
                   END"""
            )
        before = self._logical_dump()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced Task 3 rollback"):
            backfill_school_plan_offer_014(self.database_path, apply=True)

        self.assertEqual(self._logical_dump(), before)

    def _write_formal_backfill_manifest(self, *, complete=True):
        safe_point = self.root / "backups" / "safe-box" / "formal-backfill-test"
        safe_point.mkdir(parents=True)
        backup_database = safe_point / "aaie_crm.db"
        shutil.copy2(self.database_path, backup_database)
        manifest_path = safe_point / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "complete": complete,
                    "root": str(self.root),
                    "database": {
                        "backup_path": str(backup_database.relative_to(self.root)),
                        "backup_size": backup_database.stat().st_size,
                        "quick_check": "ok",
                    },
                }
            ),
            encoding="utf-8",
        )
        return manifest_path

    def test_formal_backfill_requires_exact_confirmation_sha_and_complete_safebox(self):
        self._migrate()
        self._seed_backfill_fixture()
        manifest_path = self._write_formal_backfill_manifest(complete=False)
        expected_sha = self._snapshot_hash()

        with self.assertRaisesRegex(ValueError, "formal backfill confirmation"):
            apply_formal_school_plan_offer_014(
                self.database_path,
                confirmation="wrong",
                expected_database_sha256=expected_sha,
                backup_manifest_path=manifest_path,
                formal_database_path=self.database_path,
            )
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            apply_formal_school_plan_offer_014(
                self.database_path,
                confirmation=FORMAL_BACKFILL_CONFIRMATION,
                expected_database_sha256="0" * 64,
                backup_manifest_path=manifest_path,
                formal_database_path=self.database_path,
            )
        with self.assertRaisesRegex(ValueError, "SAFEBOX manifest is incomplete"):
            apply_formal_school_plan_offer_014(
                self.database_path,
                confirmation=FORMAL_BACKFILL_CONFIRMATION,
                expected_database_sha256=expected_sha,
                backup_manifest_path=manifest_path,
                formal_database_path=self.database_path,
            )

        self.assertEqual(self._snapshot_hash(), expected_sha)

    def test_formal_backfill_applies_once_and_is_verified_idempotent(self):
        self._migrate()
        self._seed_backfill_fixture()
        manifest_path = self._write_formal_backfill_manifest()
        preview = backfill_school_plan_offer_014(self.database_path, apply=False)

        applied = apply_formal_school_plan_offer_014(
            self.database_path,
            confirmation=FORMAL_BACKFILL_CONFIRMATION,
            expected_database_sha256=self._snapshot_hash(),
            backup_manifest_path=manifest_path,
            formal_database_path=self.database_path,
        )

        self.assertEqual(applied, preview)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone(), ("ok",))

        repeated = apply_formal_school_plan_offer_014(
            self.database_path,
            confirmation=FORMAL_BACKFILL_CONFIRMATION,
            expected_database_sha256=self._snapshot_hash(),
            backup_manifest_path=manifest_path,
            formal_database_path=self.database_path,
        )
        self.assertEqual(repeated.rows_changed, 0)
        self.assertEqual(repeated.exception_rows_written, 0)

    def test_fresh_database_schema_matches_migration_contract(self):
        fresh_database_path = self.root / "fresh-school-plan-offer.db"
        database = Database(fresh_database_path)
        database.initialize()

        with closing(sqlite3.connect(fresh_database_path)) as connection:
            report_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(school_plan_reports)")
            }
            offer_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(offers)")
            }
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(self.expected_report_columns <= report_columns)
            self.assertTrue(self.expected_offer_columns <= offer_columns)
            self.assertTrue(self.expected_tables <= tables)

    def test_fresh_schema_matches_migrated_014_object_contracts(self):
        self._migrate()
        fresh_database_path = self.root / "fresh-object-contracts.db"
        Database(fresh_database_path).initialize()
        object_names = (
            "school_plan_confirmed_schools",
            "school_plan_school_submission_events",
            "student_offer_acceptances",
            "offer_acceptance_history",
            "school_plan_offer_migration_exceptions",
            "uq_school_plan_current_public_city",
            "uq_school_plan_confirmed_schools_null_school",
            "school_plan_confirmed_schools_ownership_insert",
            "school_plan_confirmed_schools_ownership_update",
            "school_plan_confirmed_schools_not_submitted_evidence_insert",
            "school_plan_confirmed_schools_not_submitted_evidence_update",
            "school_plan_school_submission_events_ownership_insert",
            "school_plan_school_submission_events_ownership_update",
            "school_plan_confirmed_schools_identity_no_update_after_event",
            "school_plan_confirmed_schools_submission_event_after_update",
            "student_offer_acceptances_ownership_insert",
            "student_offer_acceptances_ownership_update",
            "offer_acceptance_history_ownership_insert",
            "offer_acceptance_history_ownership_update",
            "school_plan_school_submission_events_no_update",
            "school_plan_school_submission_events_no_delete",
            "offer_acceptance_history_no_update",
            "offer_acceptance_history_no_delete",
            "offers_student_identity_no_update",
            "school_plan_reports_student_identity_no_update",
            "school_plan_report_confirmations_report_identity_no_update",
        )

        def object_sql(path):
            with closing(sqlite3.connect(path)) as connection:
                rows = connection.execute(
                    """SELECT name,sql FROM sqlite_master
                       WHERE name IN ({}) ORDER BY name""".format(
                        ",".join("?" for _ in object_names)
                    ),
                    object_names,
                ).fetchall()
            return {
                name: re.sub(r"\bIF NOT EXISTS\s+", "", sql)
                for name, sql in rows
            }

        self.assertEqual(object_sql(self.database_path), object_sql(fresh_database_path))


if __name__ == "__main__":
    unittest.main()
