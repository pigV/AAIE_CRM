from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations/20260827_031_transfer_v2_authoritative_lifecycle.sql"
FORWARD_CHECK = (
    ROOT
    / "migrations/checks/20260827_031_transfer_v2_authoritative_lifecycle_forward_check.sql"
)


def _fingerprint(connection, table, columns=None):
    projection = ",".join(f'"{column}"' for column in columns) if columns else "*"
    rows = connection.execute(
        f'SELECT {projection} FROM "{table}" ORDER BY rowid'
    ).fetchall()
    return hashlib.sha256(
        json.dumps(rows, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class TransferV2Migration031Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "transfer-v2-031.sqlite3"
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(
                """
                CREATE TABLE users(id INTEGER PRIMARY KEY);
                CREATE TABLE students(student_id TEXT PRIMARY KEY);
                CREATE TABLE schools(id INTEGER PRIMARY KEY);
                CREATE TABLE contracts(
                  id INTEGER PRIMARY KEY,
                  student_id TEXT NOT NULL REFERENCES students(student_id),
                  contract_type TEXT NOT NULL,
                  contract_status TEXT NOT NULL,
                  deleted_at TEXT,
                  voided_at TEXT,
                  final_signed_file_path TEXT
                );
                CREATE TABLE student_cases(
                  id INTEGER PRIMARY KEY,
                  student_id TEXT NOT NULL REFERENCES students(student_id),
                  case_type TEXT NOT NULL,
                  status TEXT NOT NULL,
                  completed_at TEXT,
                  cancelled_at TEXT
                );
                CREATE TABLE application_cycles(
                  id INTEGER PRIMARY KEY,
                  cycle_uid TEXT NOT NULL UNIQUE,
                  student_id TEXT NOT NULL REFERENCES students(student_id),
                  source_student_case_id INTEGER REFERENCES student_cases(id),
                  cycle_type TEXT NOT NULL,
                  sequence_no INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  outcome TEXT,
                  opened_at TEXT NOT NULL,
                  closed_at TEXT,
                  education_stream TEXT NOT NULL DEFAULT 'school_education'
                );
                CREATE TABLE transfer_cases(
                  case_id INTEGER PRIMARY KEY REFERENCES student_cases(id),
                  transfer_origin TEXT NOT NULL,
                  source_contract_id INTEGER REFERENCES contracts(id),
                  workflow_state TEXT NOT NULL,
                  current_school_name_snapshot TEXT NOT NULL,
                  current_school_id INTEGER REFERENCES schools(id),
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE transfer_school_applications(
                  id INTEGER PRIMARY KEY,
                  case_id INTEGER NOT NULL REFERENCES transfer_cases(case_id),
                  school_id INTEGER REFERENCES schools(id),
                  school_name_snapshot TEXT NOT NULL,
                  application_status TEXT NOT NULL,
                  created_by INTEGER NOT NULL REFERENCES users(id),
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE offer_received_events(
                  id INTEGER PRIMARY KEY,
                  application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id),
                  student_id TEXT NOT NULL REFERENCES students(student_id),
                  external_offer_key TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  received_at TEXT NOT NULL
                );
                CREATE TABLE cycle_final_acceptances(
                  id INTEGER PRIMARY KEY,
                  application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id),
                  student_id TEXT NOT NULL REFERENCES students(student_id),
                  offer_received_event_id INTEGER NOT NULL REFERENCES offer_received_events(id),
                  acceptance_status TEXT NOT NULL DEFAULT 'active'
                );
                CREATE TABLE student_documents(
                  id INTEGER PRIMARY KEY,
                  student_id TEXT NOT NULL REFERENCES students(student_id),
                  document_type TEXT NOT NULL,
                  file_path TEXT NOT NULL,
                  uploaded_at TEXT NOT NULL
                );
                CREATE TABLE coe_oshc(
                  id INTEGER PRIMARY KEY,
                  student_id TEXT NOT NULL REFERENCES students(student_id),
                  document_kind TEXT NOT NULL,
                  file_path TEXT,
                  application_cycle_id INTEGER REFERENCES application_cycles(id)
                );

                INSERT INTO users VALUES(1),(2);
                INSERT INTO students VALUES('S-LEGACY'),('S-V2');
                INSERT INTO schools VALUES(10),(20),(30);
                INSERT INTO contracts VALUES(
                  50,'S-V2','TransferApplicationContract','FullyExecuted',NULL,NULL,'/contracts/50.pdf'
                );
                INSERT INTO student_cases VALUES
                  (1,'S-LEGACY','transfer','preparing',NULL,NULL),
                  (2,'S-V2','transfer','preparing',NULL,NULL);
                INSERT INTO application_cycles VALUES(
                  101,'legacy-cycle','S-LEGACY',1,'transfer',2,'active',NULL,
                  '2026-08-20',NULL,'school_education'
                );
                INSERT INTO transfer_cases VALUES(
                  1,'internal',NULL,'preparing','Legacy School',10,
                  '2026-08-20','2026-08-20'
                );
                INSERT INTO transfer_school_applications VALUES(
                  201,1,20,'Legacy Target','submitted',1,'2026-08-20','2026-08-20'
                );
                INSERT INTO student_documents VALUES(
                  301,'S-LEGACY','Passport','/students/S-LEGACY/passport.pdf','2026-08-20'
                );
                """
            )
            connection.commit()
            tables = (
                    "students",
                    "student_cases",
                    "application_cycles",
                    "transfer_cases",
                    "transfer_school_applications",
                    "student_documents",
            )
            self.legacy_columns = {
                table: [
                    row[1]
                    for row in connection.execute(f'PRAGMA table_info("{table}")')
                ]
                for table in tables
            }
            self.before = {
                table: _fingerprint(connection, table, self.legacy_columns[table])
                for table in tables
            }

    def _apply(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(MIGRATION.read_text(encoding="utf-8"))
            connection.commit()

    def test_migration_files_exist_and_follow_030(self):
        self.assertTrue(MIGRATION.is_file())
        self.assertTrue(FORWARD_CHECK.is_file())
        versions = sorted(path.name for path in (ROOT / "migrations").glob("*.sql"))
        self.assertEqual(
            "20260824_030_lifecycle_module_dispositions.sql",
            versions[versions.index(MIGRATION.name) - 1],
        )

    def test_additive_schema_preserves_legacy_rows_and_creates_no_v2_facts(self):
        self._apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            after = {
                table: _fingerprint(connection, table, self.legacy_columns[table])
                for table in self.before
            }
            self.assertEqual(self.before, after)
            for table in (
                "transfer_v2_origin_events",
                "transfer_target_state_events",
                "transfer_document_requirements",
                "transfer_document_bindings",
                "transfer_document_binding_state_events",
                "transfer_document_adoption_events",
                "transfer_payment_aggregates",
                "transfer_payment_events",
                "transfer_coe_confirmations",
                "transfer_coe_events",
                "transfer_v2_lifecycle_events",
                "transfer_v2_command_receipts",
                "transfer_completion_events",
                "transfer_school_library_requests",
            ):
                self.assertEqual(0, connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0])
            self.assertEqual("ok", connection.execute("PRAGMA quick_check").fetchone()[0])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())

    def test_v2_columns_and_exact_ownership_constraints_exist(self):
        self._apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            transfer_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(transfer_cases)")
            }
            branch_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(transfer_school_applications)"
                )
            }
            offer_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(offer_received_events)")
            }
            acceptance_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(cycle_final_acceptances)")
            }
            self.assertTrue({
                "v2_version",
                "transfer_source_label",
                "current_year_level_snapshot",
                "application_year_level",
                "transfer_year",
                "transfer_term",
                "current_school_state_snapshot",
                "current_school_region_snapshot",
                "current_school_type_snapshot",
            }.issubset(transfer_columns))
            self.assertTrue({
                "branch_uid",
                "application_cycle_id",
                "original_school_id",
                "school_state_snapshot",
                "school_region_snapshot",
                "school_type_snapshot",
                "updated_by",
            }.issubset(branch_columns))
            self.assertIn("transfer_branch_id", offer_columns)
            self.assertIn("transfer_branch_id", acceptance_columns)

    def test_event_tables_are_append_only_and_v2_contract_is_mandatory(self):
        self._apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """INSERT INTO transfer_cases(
                     case_id,transfer_origin,source_contract_id,workflow_state,
                     current_school_name_snapshot,created_at,updated_at,v2_version,
                     transfer_source_label,current_year_level_snapshot,
                     application_year_level,transfer_year,transfer_term)
                   VALUES(2,'internal',50,'preparing','Current School',
                          '2026-08-27','2026-08-27',2,'aaie_existing',
                          'Year 8','Year 9',2027,'Term 1')"""
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE transfer_cases SET source_contract_id=NULL WHERE case_id=2"
                )
            connection.execute(
                """INSERT INTO application_cycles VALUES(
                     102,'v2-cycle','S-V2',2,'transfer',2,'active',NULL,
                     '2026-08-27',NULL,'school_education')"""
            )
            connection.execute(
                """INSERT INTO transfer_school_applications(
                     id,case_id,school_id,school_name_snapshot,application_status,
                     created_by,created_at,updated_at,branch_uid,application_cycle_id,
                     original_school_id,school_state_snapshot,school_region_snapshot,
                     school_type_snapshot,updated_by)
                   VALUES(202,2,30,'Target School','preparing',1,'2026-08-27',
                          '2026-08-27','branch-202',102,30,'VIC','Melbourne','Public',1)"""
            )
            event_id = connection.execute(
                """INSERT INTO transfer_target_state_events(
                     event_uid,application_cycle_id,branch_id,previous_state,new_state,
                     event_type,actor_user_id,occurred_at,created_at)
                   VALUES('branch-state-1',102,202,NULL,'materials_preparation',
                          'created',1,'2026-08-27','2026-08-27')"""
            ).lastrowid
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    "UPDATE transfer_target_state_events SET note='changed' WHERE id=?",
                    (event_id,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    "DELETE FROM transfer_target_state_events WHERE id=?", (event_id,)
                )


if __name__ == "__main__":
    unittest.main()
