from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

import migration_runner
from migration_runner import MigrationForwardCheckError, discover_migrations
from tools.progression_contract_ownership_migration_acceptance import business_snapshot


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT / "migrations" / "20260818_024_progression_contract_ownership.sql"
)
FORWARD_CHECK_PATH = (
    ROOT
    / "migrations"
    / "checks"
    / "20260818_024_progression_contract_ownership_forward_check.sql"
)


class ProgressionContractOwnershipMigration024Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "progression-contract-024.db"
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(
                """
                CREATE TABLE students(student_id TEXT PRIMARY KEY);
                CREATE TABLE student_cases(
                  id INTEGER PRIMARY KEY,
                  student_id TEXT NOT NULL REFERENCES students(student_id),
                  case_type TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'draft',
                  active_slot INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE contracts(
                  id INTEGER PRIMARY KEY,
                  student_id TEXT REFERENCES students(student_id),
                  contract_type TEXT NOT NULL,
                  contract_status TEXT NOT NULL,
                  deleted_at TEXT,
                  voided_at TEXT
                );
                CREATE TABLE progression_cases(
                  case_id INTEGER PRIMARY KEY
                    REFERENCES student_cases(id) ON DELETE RESTRICT,
                  progression_type TEXT NOT NULL,
                  workflow_stage TEXT NOT NULL DEFAULT 'planning',
                  case_outcome TEXT
                );
                CREATE TABLE offers(id INTEGER PRIMARY KEY, student_id TEXT, status TEXT);
                CREATE TABLE coe_oshc(id INTEGER PRIMARY KEY, student_id TEXT, status TEXT);
                CREATE TABLE visas(id INTEGER PRIMARY KEY, student_id TEXT, status TEXT);
                CREATE TABLE school_plan_reports(id INTEGER PRIMARY KEY, student_id TEXT, status TEXT);
                CREATE TABLE application_cycles(id INTEGER PRIMARY KEY, student_id TEXT, status TEXT);
                INSERT INTO students VALUES('S1'),('S2');
                INSERT INTO student_cases(id,student_id,case_type) VALUES
                  (1,'S1','progression'),
                  (2,'S1','progression'),
                  (3,'S1','progression'),
                  (4,'S2','progression'),
                  (5,'S1','progression'),
                  (6,'S1','progression'),
                  (7,'S1','progression');
                INSERT INTO progression_cases(case_id,progression_type)
                  VALUES(1,'bachelor');
                INSERT INTO contracts VALUES
                  (10,'S1','LocalStudentApplicationContract','Draft',NULL,NULL),
                  (11,'S1','LocalStudentApplicationContract','ParentSigned',NULL,NULL),
                  (12,'S1','LocalStudentApplicationContract','FullyExecuted',NULL,NULL),
                  (13,'S1','NewInternationalStudentContract','FullyExecuted',NULL,NULL),
                  (14,'S1','TransferApplicationContract','FullyExecuted',NULL,NULL),
                  (15,'S2','LocalStudentApplicationContract','FullyExecuted',NULL,NULL),
                  (16,'S1','LocalStudentApplicationContract','FullyExecuted','2026-08-18',NULL),
                  (17,'S1','LocalStudentApplicationContract','FullyExecuted',NULL,'2026-08-18');
                INSERT INTO offers VALUES(20,'S1','received');
                INSERT INTO coe_oshc VALUES(30,'S1','confirmed');
                INSERT INTO visas VALUES(40,'S1','granted');
                INSERT INTO school_plan_reports VALUES(50,'S1','confirmed');
                INSERT INTO application_cycles VALUES(60,'S1','active');
                """
            )

    def apply_sql(self):
        self.assertTrue(MIGRATION_PATH.is_file())
        self.assertTrue(FORWARD_CHECK_PATH.is_file())
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))

    def test_package_is_unique_next_identity_after_023(self):
        migrations = discover_migrations(ROOT / "migrations")
        versions = [item.version for item in migrations]
        position = versions.index("20260818_024")
        self.assertEqual("20260816_023", versions[position - 1])
        self.assertEqual("normal", migrations[position].mode)

    def test_acceptance_snapshot_fingerprints_every_protected_business_family(self):
        before = business_snapshot(self.database_path)
        expected = {
            "contracts", "offers", "coe_oshc", "visas",
            "school_plan", "application_cycles",
        }
        self.assertEqual(expected, set(before["business_fingerprints"]))

        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("INSERT INTO offers VALUES(21,'S1','accepted')")
            connection.commit()
        after = business_snapshot(self.database_path)

        self.assertNotEqual(
            before["business_fingerprints"]["offers"],
            after["business_fingerprints"]["offers"],
        )
        for family in expected - {"offers"}:
            self.assertEqual(
                before["business_fingerprints"][family],
                after["business_fingerprints"][family],
            )

    def test_schema_only_adds_nullable_canonical_ownership_without_backfill(self):
        self.apply_sql()
        with closing(sqlite3.connect(self.database_path)) as connection:
            columns = {
                row[1]: row for row in connection.execute(
                    "PRAGMA table_info(progression_cases)"
                )
            }
            legacy = connection.execute(
                "SELECT business_type,source_contract_id FROM progression_cases WHERE case_id=1"
            ).fetchone()
            foreign_keys = connection.execute(
                "PRAGMA foreign_key_list(progression_cases)"
            ).fetchall()
            index_names = {
                row[1] for row in connection.execute(
                    "PRAGMA index_list(progression_cases)"
                )
            }
        self.assertEqual(0, columns["business_type"][3])
        self.assertEqual(0, columns["source_contract_id"][3])
        self.assertEqual((None, None), legacy)
        self.assertTrue(any(row[2] == "contracts" and row[3] == "source_contract_id" for row in foreign_keys))
        self.assertIn("uq_progression_cases_source_contract", index_names)

    def test_he_requires_no_contract_and_school_requires_exact_fully_executed_local(self):
        self.apply_sql()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """INSERT INTO progression_cases(
                     case_id,progression_type,business_type,source_contract_id)
                   VALUES(2,'bachelor','higher_education',NULL)"""
            )
            connection.execute(
                """INSERT INTO progression_cases(
                     case_id,progression_type,business_type,source_contract_id)
                   VALUES(3,'other','school_progression',12)"""
            )
            for case_id, business_type, contract_id in (
                (5, "school_progression", 15),
                (5, "school_progression", 10),
                (5, "school_progression", 11),
                (5, "school_progression", 13),
                (5, "school_progression", 14),
                (5, "school_progression", 16),
                (5, "school_progression", 17),
                (5, "school_progression", None),
                (5, "higher_education", 12),
                (5, None, 12),
            ):
                with self.subTest(case_id=case_id, business_type=business_type, contract_id=contract_id):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            """INSERT INTO progression_cases(
                                 case_id,progression_type,business_type,source_contract_id)
                               VALUES(?,?,?,?)""",
                            (case_id, "other", business_type, contract_id),
                        )

    def test_owned_contract_cannot_be_reused_by_another_progression_case(self):
        self.apply_sql()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """INSERT INTO progression_cases(
                     case_id,progression_type,business_type,source_contract_id)
                   VALUES(3,'other','school_progression',12)"""
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO progression_cases(
                         case_id,progression_type,business_type,source_contract_id)
                       VALUES(5,'other','school_progression',12)"""
                )

    def test_forward_check_is_read_only_and_passes(self):
        self.apply_sql()
        sql = FORWARD_CHECK_PATH.read_text(encoding="utf-8")
        forbidden = ("UPDATE ", "DELETE ", "INSERT ", "ALTER ", "DROP ", "CREATE ")
        self.assertFalse(any(token in sql.upper() for token in forbidden))
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(1, connection.execute(sql.strip().rstrip(";")).fetchone()[0])
            self.assertEqual("ok", connection.execute("PRAGMA quick_check").fetchone()[0])

    def test_runner_rolls_back_schema_and_registry_when_forward_check_fails(self):
        migration_dir = self.root / "bad-migration"
        checks_dir = migration_dir / "checks"
        checks_dir.mkdir(parents=True)
        shutil.copy2(MIGRATION_PATH, migration_dir / MIGRATION_PATH.name)
        (checks_dir / FORWARD_CHECK_PATH.name).write_text("SELECT 0;\n", encoding="utf-8")

        with self.assertRaises(MigrationForwardCheckError):
            migration_runner.apply_migrations(
                self.database_path,
                migration_dir,
                applied_by="progression-024-rollback-test",
                source_commit="progression-024-rollback-test",
                formal_database_path=None,
            )

        with closing(sqlite3.connect(self.database_path)) as connection:
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(progression_cases)"
                )
            }
            registry_exists = connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()[0]
            self.assertNotIn("business_type", columns)
            self.assertNotIn("source_contract_id", columns)
            self.assertEqual(0, registry_exists)
            self.assertEqual(("bachelor",), connection.execute(
                "SELECT progression_type FROM progression_cases WHERE case_id=1"
            ).fetchone())


if __name__ == "__main__":
    unittest.main()
