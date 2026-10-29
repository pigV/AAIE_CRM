from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "migrations" / "20260808_016_offer_course_start_date.sql"
FORWARD_CHECK_PATH = ROOT / "migrations" / "checks" / "20260808_016_offer_course_start_date_forward_check.sql"


class OfferCourseStartMigration016Tests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.database_path = Path(self.tempdir.name) / "migration-016.db"
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE students(student_id TEXT PRIMARY KEY);
                CREATE TABLE offers(
                  id INTEGER PRIMARY KEY,
                  student_id TEXT NOT NULL REFERENCES students(student_id),
                  school_name TEXT NOT NULL,
                  offer_date TEXT,
                  created_at TEXT NOT NULL
                );
                INSERT INTO students VALUES('S1');
                INSERT INTO offers VALUES(1,'S1','Final College','2026-08-01','2026-08-01');
                """
            )

    def apply_migration(self):
        self.assertTrue(MIGRATION_PATH.is_file())
        self.assertTrue(FORWARD_CHECK_PATH.is_file())
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))

    def test_adds_optional_validated_course_start_without_backfill(self):
        self.apply_migration()
        with closing(sqlite3.connect(self.database_path)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info('offers')")}
            self.assertIn("course_start_date", columns)
            self.assertIsNone(connection.execute("SELECT course_start_date FROM offers WHERE id=1").fetchone()[0])
            connection.execute("UPDATE offers SET course_start_date='2027-02-01' WHERE id=1")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE offers SET course_start_date='2026-02-30' WHERE id=1")

    def test_forward_check_is_read_only_and_passes(self):
        self.apply_migration()
        sql = FORWARD_CHECK_PATH.read_text(encoding="utf-8")
        self.assertFalse(any(token in sql.upper() for token in ("UPDATE ", "DELETE ", "INSERT ", "ALTER ", "DROP ", "CREATE ")))
        with closing(sqlite3.connect(self.database_path)) as connection:
            statement = sql.strip().rstrip(";")
            self.assertEqual(connection.execute(statement).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
