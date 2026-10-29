import sqlite3
import tempfile
import unittest
from pathlib import Path

from migration_runner import discover_migrations, split_sql_statements


ROOT = Path(__file__).resolve().parents[1]


def build_pre_020(path: Path, *, bound_draft: bool = False) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE users(id INTEGER PRIMARY KEY);
            CREATE TABLE students(student_id TEXT PRIMARY KEY);
            CREATE TABLE student_cases(
              id INTEGER PRIMARY KEY,
              student_id TEXT NOT NULL REFERENCES students(student_id),
              case_type TEXT NOT NULL CHECK(case_type IN ('transfer','progression')),
              sequence_no INTEGER NOT NULL CHECK(sequence_no > 0),
              status TEXT NOT NULL,
              terminal_result TEXT,
              active_slot INTEGER NOT NULL CHECK(active_slot IN (0,1)),
              started_at TEXT, paused_at TEXT, completed_at TEXT,
              cancelled_at TEXT, failed_at TEXT, archived_at TEXT,
              created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
              updated_by INTEGER NOT NULL REFERENCES users(id), updated_at TEXT NOT NULL,
              UNIQUE(student_id,case_type,sequence_no),
              CHECK((status IN ('draft','preparing','in_progress','waiting','paused') AND active_slot=1 AND terminal_result IS NULL)
                 OR (status IN ('completed','cancelled','failed') AND active_slot=0 AND terminal_result=status)
                 OR (status='archived' AND active_slot=0 AND terminal_result IN ('completed','cancelled','failed')))
            );
            CREATE UNIQUE INDEX uq_student_cases_one_active_per_student ON student_cases(student_id) WHERE active_slot=1;
            CREATE INDEX ix_student_cases_student_type_sequence ON student_cases(student_id,case_type,sequence_no DESC);
            CREATE INDEX ix_student_cases_student_status ON student_cases(student_id,status);
            CREATE TRIGGER student_cases_no_delete BEFORE DELETE ON student_cases BEGIN SELECT RAISE(ABORT,'student cases are retained permanently'); END;
            CREATE TRIGGER student_cases_archive_from_terminal_only BEFORE UPDATE OF status ON student_cases
              WHEN NEW.status='archived' AND OLD.status NOT IN ('completed','cancelled','failed')
              BEGIN SELECT RAISE(ABORT,'active student case cannot be archived'); END;
            CREATE TABLE application_cycles(
              id INTEGER PRIMARY KEY, student_id TEXT REFERENCES students(student_id),
              source_student_case_id INTEGER REFERENCES student_cases(id), cycle_type TEXT NOT NULL,
              status TEXT NOT NULL
            );
            CREATE TRIGGER application_cycles_case_guard
            BEFORE INSERT ON application_cycles
            WHEN NEW.source_student_case_id IS NOT NULL AND NOT EXISTS(
              SELECT 1 FROM student_cases WHERE id=NEW.source_student_case_id
            )
            BEGIN SELECT RAISE(ABORT,'case missing'); END;
            CREATE TABLE case_notes(id INTEGER PRIMARY KEY, case_id INTEGER NOT NULL REFERENCES student_cases(id));
            INSERT INTO users VALUES(1);
            INSERT INTO students VALUES('S1');
            INSERT INTO student_cases VALUES(1,'S1','transfer',1,'draft',NULL,1,NULL,NULL,NULL,NULL,NULL,NULL,1,'now',1,'now');
            INSERT INTO case_notes VALUES(1,1);
            """
        )
        if bound_draft:
            connection.execute("INSERT INTO application_cycles VALUES(1,'S1',1,'transfer','active')")


def migration_sql() -> str:
    path = ROOT / "migrations" / "20260813_020_student_lifecycle_draft_semantics.sql"
    return path.read_text(encoding="utf-8")


def execute_migration(path: Path) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA legacy_alter_table=ON")
        connection.execute("BEGIN IMMEDIATE")
        for statement in split_sql_statements(migration_sql()):
            connection.execute(statement)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


class StudentLifecycleMigration020Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "crm.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_package_is_discoverable_as_guarded_rebuild(self):
        migration = next(
            item for item in discover_migrations(ROOT / "migrations")
            if item.version == "20260813_020"
        )
        self.assertEqual("20260813_020", migration.version)
        self.assertEqual("sqlite_fk_off_rebuild", migration.mode)

    def test_upgrade_preserves_rows_and_allows_multiple_drafts(self):
        build_pre_020(self.path)
        execute_migration(self.path)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(("draft", 0), connection.execute("SELECT status,active_slot FROM student_cases WHERE id=1").fetchone())
            connection.execute("INSERT INTO student_cases VALUES(2,'S1','transfer',2,'draft',NULL,0,NULL,NULL,NULL,NULL,NULL,NULL,1,'now',1,'now')")
            connection.execute("INSERT INTO student_cases VALUES(3,'S1','progression',1,'draft',NULL,0,NULL,NULL,NULL,NULL,NULL,NULL,1,'now',1,'now')")
            self.assertEqual(3, connection.execute("SELECT count(*) FROM student_cases").fetchone()[0])
            self.assertEqual(1, connection.execute("SELECT count(*) FROM case_notes").fetchone()[0])
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='application_cycles_case_guard'"
            ).fetchone())
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual("ok", connection.execute("PRAGMA quick_check").fetchone()[0])

    def test_bound_active_draft_is_reconciliation_blocker_and_rolls_back(self):
        build_pre_020(self.path, bound_draft=True)
        with self.assertRaises(sqlite3.IntegrityError):
            execute_migration(self.path)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(("draft", 1), connection.execute("SELECT status,active_slot FROM student_cases WHERE id=1").fetchone())
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='student_lifecycle_idempotency_receipts'").fetchone())

    def test_legacy_cycle_is_not_automatically_reowned_by_draft(self):
        build_pre_020(self.path)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """INSERT INTO application_cycles(
                       id,student_id,source_student_case_id,cycle_type,status
                   ) VALUES(7,'S1',NULL,'initial','closed')"""
            )

        execute_migration(self.path)

        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                (None, "initial", "closed"),
                connection.execute(
                    """SELECT source_student_case_id,cycle_type,status
                         FROM application_cycles WHERE id=7"""
                ).fetchone(),
            )


if __name__ == "__main__":
    unittest.main()
