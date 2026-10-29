from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "migrations" / "20260807_015_application_stage_distribution.sql"
FORWARD_CHECK_PATH = (
    ROOT
    / "migrations"
    / "checks"
    / "20260807_015_application_stage_distribution_forward_check.sql"
)


class ApplicationStageDistributionMigration015Tests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.database_path = Path(self.tempdir.name) / "migration-015.db"
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys=ON;
                CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE students(student_id TEXT PRIMARY KEY);
                CREATE TABLE school_plan_reports(
                    id INTEGER PRIMARY KEY,
                    student_id TEXT NOT NULL REFERENCES students(student_id)
                );
                CREATE TABLE school_plan_report_confirmations(
                    id INTEGER PRIMARY KEY,
                    report_id INTEGER NOT NULL REFERENCES school_plan_reports(id)
                );
                CREATE TABLE offers(
                    id INTEGER PRIMARY KEY,
                    student_id TEXT NOT NULL REFERENCES students(student_id)
                );
                CREATE TABLE school_plan_confirmed_schools(
                    id INTEGER PRIMARY KEY,
                    confirmation_id INTEGER NOT NULL REFERENCES school_plan_report_confirmations(id),
                    report_id INTEGER NOT NULL REFERENCES school_plan_reports(id),
                    student_id TEXT NOT NULL REFERENCES students(student_id),
                    submission_status TEXT NOT NULL DEFAULT 'not_submitted',
                    submitted_at TEXT,
                    submitted_by_user_id INTEGER REFERENCES users(id),
                    submitted_by_name TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE visa_cases(
                    id INTEGER PRIMARY KEY,
                    student_id TEXT NOT NULL REFERENCES students(student_id),
                    actual_lodgement_date TEXT
                );
                INSERT INTO users VALUES(1, 'Alice');
                INSERT INTO students VALUES('S1'), ('S2');
                INSERT INTO school_plan_reports VALUES(10, 'S1');
                INSERT INTO school_plan_report_confirmations VALUES(20, 10);
                INSERT INTO offers VALUES(30, 'S1'), (31, 'S2');
                INSERT INTO school_plan_confirmed_schools(
                    id, confirmation_id, report_id, student_id, submission_status,
                    submitted_at, submitted_by_user_id, submitted_by_name, updated_at
                ) VALUES(40, 20, 10, 'S1', 'submitted',
                         '2026-08-07T09:00:00+10:00', 1, 'Alice',
                         '2026-08-07T09:00:00+10:00');
                INSERT INTO visa_cases VALUES(50, 'S1', NULL);
                """
            )

    def _apply(self):
        self.assertTrue(MIGRATION_PATH.is_file())
        self.assertTrue(FORWARD_CHECK_PATH.is_file())
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))
            connection.commit()

    def test_forward_migration_adds_result_and_visa_evidence(self):
        self._apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            school_columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(school_plan_confirmed_schools)"
                )
            }
            visa_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(visa_cases)")
            }
            self.assertTrue(
                {
                    "result_status", "result_at", "result_by_user_id",
                    "result_by_name", "result_note", "result_offer_id",
                }.issubset(school_columns)
            )
            self.assertTrue(
                {"lodged_by_user_id", "lodged_by_name", "lodgement_recorded_at"}
                .issubset(visa_columns)
            )
            self.assertIsNotNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='school_plan_school_result_events'"
                ).fetchone()
            )

    def test_result_guards_and_append_only_event_history(self):
        self._apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE school_plan_confirmed_schools "
                    "SET result_status='rejected', result_at='2026-08-07', "
                    "result_by_user_id=1, result_by_name='Alice', result_note='' "
                    "WHERE id=40"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE school_plan_confirmed_schools "
                    "SET result_status='offer_received', result_at='2026-08-07', "
                    "result_by_user_id=1, result_by_name='Alice', result_offer_id=31 "
                    "WHERE id=40"
                )
            connection.execute(
                "UPDATE school_plan_confirmed_schools "
                "SET result_status='offer_received', result_at='2026-08-07', "
                "result_by_user_id=1, result_by_name='Alice', result_offer_id=30, "
                "updated_at='2026-08-07T10:00:00+10:00' WHERE id=40"
            )
            event = connection.execute(
                "SELECT previous_result_status,new_result_status,result_offer_id "
                "FROM school_plan_school_result_events WHERE confirmed_school_id=40"
            ).fetchone()
            self.assertEqual(event, ("pending", "offer_received", 30))
            event_id = connection.execute(
                "SELECT id FROM school_plan_school_result_events"
            ).fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE school_plan_school_result_events SET result_note='changed' "
                    "WHERE id=?",
                    (event_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM school_plan_school_result_events WHERE id=?",
                    (event_id,),
                )

    def test_forward_check_is_read_only_and_reports_no_fixture_errors(self):
        self._apply()
        sql = FORWARD_CHECK_PATH.read_text(encoding="utf-8")
        forbidden = ("UPDATE ", "DELETE ", "INSERT ", "ALTER ", "DROP ", "CREATE ")
        self.assertFalse(any(token in sql.upper() for token in forbidden))
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.executescript(sql)
            self.assertIsNotNone(rows)


if __name__ == "__main__":
    unittest.main()
