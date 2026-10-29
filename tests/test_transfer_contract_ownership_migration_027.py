from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

import migration_runner


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "migrations/20260820_027_transfer_contract_ownership.sql"
FORWARD_CHECK_PATH = (
    ROOT / "migrations/checks/20260820_027_transfer_contract_ownership_forward_check.sql"
)


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class TransferContractOwnershipMigration027Tests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(self.sandbox.cleanup)
        self.root = Path(self.sandbox.name)
        self.database_path = self.root / "transfer-027.sqlite3"
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(
                """
                CREATE TABLE users(id INTEGER PRIMARY KEY);
                CREATE TABLE students(student_id TEXT PRIMARY KEY);
                CREATE TABLE schools(id INTEGER PRIMARY KEY);
                CREATE TABLE parent_form_tokens(token TEXT PRIMARY KEY);
                CREATE TABLE contracts(
                  id INTEGER PRIMARY KEY,
                  student_id TEXT REFERENCES students(student_id),
                  contract_type TEXT NOT NULL,
                  contract_status TEXT NOT NULL,
                  deleted_at TEXT,
                  voided_at TEXT,
                  application_cycle_id INTEGER,
                  final_signed_file_path TEXT
                );
                CREATE TABLE student_cases(
                  id INTEGER PRIMARY KEY,
                  student_id TEXT NOT NULL REFERENCES students(student_id),
                  case_type TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'draft',
                  active_slot INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE student_school_enrolments(
                  id INTEGER PRIMARY KEY,
                  student_id TEXT NOT NULL REFERENCES students(student_id),
                  enrolment_role TEXT NOT NULL,
                  status TEXT NOT NULL,
                  source_case_id INTEGER REFERENCES student_cases(id)
                );
                CREATE TABLE application_cycles(
                  id INTEGER PRIMARY KEY,
                  cycle_uid TEXT NOT NULL UNIQUE,
                  student_id TEXT REFERENCES students(student_id),
                  parent_form_token TEXT REFERENCES parent_form_tokens(token),
                  source_student_case_id INTEGER REFERENCES student_cases(id),
                  cycle_type TEXT NOT NULL CHECK(cycle_type IN ('initial','transfer','progression','future')),
                  sequence_no INTEGER NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('active','closed')),
                  outcome TEXT CHECK(outcome IS NULL OR outcome IN (
                    'granted','refused','completed','withdrawn','cancelled')),
                  opened_at TEXT NOT NULL,closed_at TEXT,
                  created_by_user_id INTEGER,created_by_name TEXT,created_at TEXT NOT NULL,
                  updated_by_user_id INTEGER,updated_by_name TEXT,updated_at TEXT NOT NULL,
                  education_stream TEXT NOT NULL DEFAULT 'school_education'
                    CHECK(education_stream IN ('school_education','higher_education')),
                  CHECK((status='active' AND outcome IS NULL AND closed_at IS NULL)
                    OR (status='closed' AND outcome IS NOT NULL AND closed_at IS NOT NULL)),
                  CHECK(student_id IS NOT NULL OR parent_form_token IS NOT NULL),
                  CHECK((cycle_type='initial' AND source_student_case_id IS NULL)
                    OR (cycle_type IN ('transfer','progression') AND source_student_case_id IS NOT NULL)
                    OR cycle_type='future')
                );
                CREATE UNIQUE INDEX uq_application_cycles_active_student_stream
                  ON application_cycles(student_id,education_stream)
                  WHERE status='active' AND student_id IS NOT NULL;
                CREATE UNIQUE INDEX uq_application_cycles_parent_form
                  ON application_cycles(parent_form_token) WHERE parent_form_token IS NOT NULL;
                CREATE UNIQUE INDEX uq_application_cycles_student_case
                  ON application_cycles(source_student_case_id) WHERE source_student_case_id IS NOT NULL;
                CREATE UNIQUE INDEX uq_application_cycles_student_sequence
                  ON application_cycles(student_id,sequence_no) WHERE student_id IS NOT NULL;
                CREATE INDEX idx_application_cycles_status
                  ON application_cycles(status,cycle_type,id);
                CREATE INDEX idx_application_cycles_student_stream
                  ON application_cycles(student_id,education_stream,status,id);
                CREATE TABLE progression_cases(
                  case_id INTEGER PRIMARY KEY REFERENCES student_cases(id),
                  business_type TEXT,source_contract_id INTEGER REFERENCES contracts(id)
                );
                CREATE TABLE transfer_school_applications(
                  id INTEGER NOT NULL,
                  case_id INTEGER NOT NULL,
                  is_final INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY(id),
                  UNIQUE(id,case_id)
                );
                CREATE TABLE transfer_cases(
                  case_id INTEGER PRIMARY KEY REFERENCES student_cases(id) ON DELETE RESTRICT,
                  transfer_origin TEXT NOT NULL DEFAULT 'internal'
                    CHECK(transfer_origin IN ('internal','external')),
                  source_contract_id INTEGER REFERENCES contracts(id) ON DELETE RESTRICT,
                  workflow_state TEXT NOT NULL DEFAULT 'draft'
                    CHECK(workflow_state IN (
                      'draft','preparing','school_search','target_confirmed',
                      'application_submitted','waiting_offer','offer_received',
                      'waiting_payment','payment_satisfied',
                      'waiting_completion_document','ready_to_complete','paused',
                      'completed','cancelled','failed','archived')),
                  current_school_name_snapshot TEXT NOT NULL
                    CHECK(length(trim(current_school_name_snapshot))>0),
                  current_school_id INTEGER REFERENCES schools(id) ON DELETE SET NULL,
                  current_city_key TEXT,
                  current_school_sector TEXT,
                  target_city_key TEXT,
                  target_school_sector TEXT,
                  rule_status TEXT NOT NULL DEFAULT 'pending_confirmation'
                    CHECK(rule_status IN ('pending_confirmation','determined')),
                  same_city_result TEXT,
                  sector_transition TEXT,
                  required_completion_document TEXT,
                  tuition_requirement TEXT,
                  final_application_id INTEGER,
                  rule_calculated_at TEXT,
                  rule_calculated_by INTEGER REFERENCES users(id) ON DELETE RESTRICT,
                  rule_version TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  title TEXT,
                  current_school_enrolment_id INTEGER
                    REFERENCES student_school_enrolments(id) ON DELETE RESTRICT,
                  upcoming_school_enrolment_id INTEGER
                    REFERENCES student_school_enrolments(id) ON DELETE RESTRICT,
                  relationship_status TEXT NOT NULL DEFAULT 'needs_repair'
                    CHECK(relationship_status IN (
                      'needs_repair','current_ready','ready','transitioned','reverted')),
                  CHECK(
                    (transfer_origin='internal' AND source_contract_id IS NULL)
                    OR (transfer_origin='external' AND source_contract_id IS NOT NULL)),
                  CHECK(rule_status='pending_confirmation' OR rule_status='determined'),
                  FOREIGN KEY(final_application_id,case_id)
                    REFERENCES transfer_school_applications(id,case_id)
                    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
                );
                CREATE UNIQUE INDEX uq_transfer_cases_external_contract
                  ON transfer_cases(source_contract_id)
                  WHERE source_contract_id IS NOT NULL;
                CREATE INDEX ix_transfer_cases_workflow
                  ON transfer_cases(workflow_state,updated_at,case_id);
                CREATE INDEX ix_transfer_cases_current_enrolment
                  ON transfer_cases(current_school_enrolment_id);
                CREATE INDEX ix_transfer_cases_upcoming_enrolment
                  ON transfer_cases(upcoming_school_enrolment_id);
                CREATE INDEX ix_transfer_cases_relationship_status
                  ON transfer_cases(relationship_status,case_id);
                CREATE TABLE transfer_case_children(
                  id INTEGER PRIMARY KEY,
                  case_id INTEGER NOT NULL REFERENCES transfer_cases(case_id) ON DELETE RESTRICT
                );

                INSERT INTO users VALUES(1);
                INSERT INTO students VALUES('S1'),('S2');
                INSERT INTO student_cases(id,student_id,case_type) VALUES
                  (1,'S1','transfer'),(2,'S1','transfer'),(3,'S2','transfer'),
                  (4,'S1','transfer'),(5,'S1','transfer'),(6,'S1','transfer'),
                  (7,'S1','transfer'),(8,'S1','transfer'),(20,'S2','progression');
                INSERT INTO parent_form_tokens VALUES('initial-S2');
                INSERT INTO application_cycles(
                  id,cycle_uid,student_id,parent_form_token,source_student_case_id,
                  cycle_type,sequence_no,status,outcome,opened_at,closed_at,
                  created_at,updated_at,education_stream)
                VALUES(30,'cycle-30','S2','initial-S2',NULL,'initial',1,'active',
                       NULL,'2026-08-01',NULL,'2026-08-01','2026-08-01','school_education');
                INSERT INTO progression_cases(case_id,business_type,source_contract_id)
                  VALUES(20,'higher_education',NULL);
                INSERT INTO contracts VALUES
                  (10,'S1','TransferApplicationContract','FullyExecuted',NULL,NULL,NULL,'/final/10.pdf'),
                  (11,'S2','TransferApplicationContract','FullyExecuted',NULL,NULL,NULL,'/final/11.pdf'),
                  (12,'S1','NewInternationalStudentContract','FullyExecuted',NULL,NULL,NULL,'/final/12.pdf'),
                  (13,'S1','LocalStudentApplicationContract','FullyExecuted',NULL,NULL,NULL,'/final/13.pdf'),
                  (14,'S1','TransferApplicationContract','Draft',NULL,NULL,NULL,NULL),
                  (15,'S1','TransferApplicationContract','FullyExecuted','2026-08-20',NULL,NULL,'/final/15.pdf'),
                  (16,'S1','TransferApplicationContract','FullyExecuted',NULL,'2026-08-20',NULL,'/final/16.pdf'),
                  (17,'S1','TransferApplicationContract','FullyExecuted',NULL,NULL,NULL,'/final/17.pdf');
                INSERT INTO transfer_cases(
                  case_id,transfer_origin,source_contract_id,workflow_state,
                  current_school_name_snapshot,rule_status,rule_version,
                  created_at,updated_at,relationship_status)
                VALUES(1,'internal',NULL,'draft','Legacy Current','pending_confirmation',
                       'transfer-rule-v1','2026-08-20','2026-08-20','needs_repair');
                INSERT INTO transfer_case_children VALUES(1,1);
                """
            )
            connection.commit()
        self.before_tables = self._fingerprint_tables()

    def _fingerprint_tables(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            result = {}
            for table in (
                "contracts", "student_cases", "transfer_case_children",
                "application_cycles", "progression_cases",
            ):
                select = (
                    "case_id,business_type,source_contract_id"
                    if table == "progression_cases"
                    else "*"
                )
                rows = connection.execute(
                    f'SELECT {select} FROM "{table}" ORDER BY rowid'
                ).fetchall()
                result[table] = hashlib.sha256(
                    json.dumps(rows, separators=(",", ":")).encode()
                ).hexdigest()
            return result

    def _stage_package(self):
        package = self.root / "package"
        checks = package / "checks"
        checks.mkdir(parents=True, exist_ok=True)
        shutil.copy2(MIGRATION_PATH, package / MIGRATION_PATH.name)
        shutil.copy2(FORWARD_CHECK_PATH, checks / FORWARD_CHECK_PATH.name)
        return package

    def _backup_metadata(self):
        backup = self.root / "pre-027.sqlite3"
        with closing(sqlite3.connect(self.database_path)) as source:
            with closing(sqlite3.connect(backup)) as target:
                source.backup(target)
        manifest = self.root / "pre-027.manifest.json"
        manifest.write_text(
            json.dumps({
                "database_path": str(self.database_path.resolve()),
                "backup_path": str(backup.resolve()),
                "backup_method": "sqlite_online_backup",
                "created_at": "2026-08-20T10:00:00+10:00",
                "source_database_sha256": _sha256(self.database_path),
                "backup_sha256": _sha256(backup),
                "quick_check": "ok",
                "integrity_check": "ok",
            }, sort_keys=True),
            encoding="utf-8",
        )
        return {"backup_path": str(backup), "manifest_path": str(manifest)}

    def apply(self):
        return migration_runner.apply_migrations(
            self.database_path,
            self._stage_package(),
            applied_by="transfer-027-test",
            source_commit="transfer-027-test",
            formal_database_path=None,
            required_backup_metadata=self._backup_metadata(),
        )

    def test_unique_next_identity_and_guarded_rebuild_mode(self):
        migration = next(
            item for item in migration_runner.discover_migrations(ROOT / "migrations")
            if item.version == "20260820_027"
        )
        self.assertEqual("transfer_contract_ownership", migration.name)
        self.assertEqual(migration_runner.SQLITE_FK_OFF_REBUILD_MODE, migration.mode)

    def test_schema_only_preserves_null_history_and_referencing_fk(self):
        self.apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            legacy = connection.execute(
                "SELECT transfer_origin,source_contract_id FROM transfer_cases WHERE case_id=1"
            ).fetchone()
            child_fk = connection.execute(
                "PRAGMA foreign_key_list('transfer_case_children')"
            ).fetchone()
            self.assertEqual(("internal", None), legacy)
            progression_current = connection.execute(
                "SELECT current_school_enrolment_id FROM progression_cases WHERE case_id=20"
            ).fetchone()[0]
            outcomes_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='application_cycles'"
            ).fetchone()[0]
            self.assertIsNone(progression_current)
            self.assertIn("superseded_by_transfer", outcomes_sql)
            self.assertIn("superseded_by_progression", outcomes_sql)
            self.assertEqual("transfer_cases", child_fk[2])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
        self.assertEqual(self.before_tables, self._fingerprint_tables())

    def test_internal_transfer_accepts_only_exact_fully_executed_contract(self):
        self.apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """INSERT INTO transfer_cases(
                     case_id,transfer_origin,source_contract_id,workflow_state,
                     current_school_name_snapshot,rule_status,rule_version,
                     created_at,updated_at,relationship_status)
                   VALUES(2,'internal',10,'draft','Current','pending_confirmation',
                          'transfer-rule-v1','2026-08-20','2026-08-20','needs_repair')"""
            )
            for case_id, contract_id in (
                (3, 10), (4, 11), (5, 12), (5, 13), (5, 14),
                (5, 15), (5, 16),
            ):
                with self.subTest(case_id=case_id, contract_id=contract_id):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            """INSERT INTO transfer_cases(
                                 case_id,transfer_origin,source_contract_id,workflow_state,
                                 current_school_name_snapshot,rule_status,rule_version,
                                 created_at,updated_at,relationship_status)
                               VALUES(?,'internal',?,'draft','Current','pending_confirmation',
                                      'transfer-rule-v1','2026-08-20','2026-08-20','needs_repair')""",
                            (case_id, contract_id),
                        )

    def test_contract_ownership_update_is_guarded_and_idempotent(self):
        self.apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                "UPDATE transfer_cases SET source_contract_id=17 WHERE case_id=1"
            )
            connection.execute(
                "UPDATE transfer_cases SET source_contract_id=17 WHERE case_id=1"
            )
            self.assertEqual(17, connection.execute(
                "SELECT source_contract_id FROM transfer_cases WHERE case_id=1"
            ).fetchone()[0])
            for contract_id in (11, 12, 14, 15, 16, None):
                with self.subTest(contract_id=contract_id):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            "UPDATE transfer_cases SET source_contract_id=? WHERE case_id=1",
                            (contract_id,),
                        )

    def test_first_apply_then_second_noop_and_forward_check(self):
        first = self.apply()
        second = migration_runner.apply_migrations(
            self.database_path,
            self._stage_package(),
            applied_by="transfer-027-test",
            source_commit="transfer-027-test",
            formal_database_path=None,
        )
        self.assertEqual(["20260820_027"], first["applied"])
        self.assertEqual([], second["applied"])
        self.assertEqual([], second["pending"])
        with closing(sqlite3.connect(self.database_path)) as connection:
            sql = FORWARD_CHECK_PATH.read_text(encoding="utf-8").strip().rstrip(";")
            self.assertEqual(1, connection.execute(sql).fetchone()[0])
            self.assertEqual("ok", connection.execute("PRAGMA quick_check").fetchone()[0])
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])

    def test_terminal_transfer_history_does_not_block_later_school_transition(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(
                """
                INSERT INTO student_school_enrolments(
                  id,student_id,enrolment_role,status,source_case_id
                ) VALUES(100,'S1','current','active',1);
                UPDATE student_cases
                   SET status='preparing',active_slot=1
                 WHERE id=1;
                UPDATE transfer_cases
                   SET current_school_enrolment_id=100,
                       relationship_status='current_ready'
                 WHERE case_id=1;
                CREATE TRIGGER student_school_enrolments_transfer_reference_guard
                BEFORE UPDATE OF student_id,enrolment_role,status,source_case_id
                ON student_school_enrolments
                BEGIN
                  SELECT CASE WHEN EXISTS (
                    SELECT 1
                      FROM transfer_cases AS transfer
                      JOIN student_cases AS parent_case
                        ON parent_case.id=transfer.case_id
                     WHERE transfer.current_school_enrolment_id=OLD.id
                       AND (
                         NEW.student_id<>parent_case.student_id
                         OR NEW.enrolment_role<>'current'
                         OR NEW.status<>'active'
                       )
                  ) THEN RAISE(
                    ABORT,
                    'referenced current enrolment cannot invalidate transfer relationship'
                  ) END;
                END;
                """
            )
            connection.commit()

        self.apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "referenced current enrolment cannot invalidate transfer relationship",
            ):
                connection.execute(
                    "UPDATE student_school_enrolments "
                    "SET enrolment_role='historical',status='ended' WHERE id=100"
                )

            connection.execute(
                "UPDATE transfer_cases "
                "SET workflow_state='completed',relationship_status='transitioned' "
                "WHERE case_id=1"
            )
            connection.execute(
                "UPDATE student_cases SET status='completed',active_slot=0 WHERE id=1"
            )
            connection.execute(
                "UPDATE student_school_enrolments "
                "SET enrolment_role='historical',status='ended' WHERE id=100"
            )
            self.assertEqual(
                (100, "historical", "ended"),
                connection.execute(
                    """SELECT transfer.current_school_enrolment_id,
                              enrolment.enrolment_role,enrolment.status
                         FROM transfer_cases AS transfer
                         JOIN student_school_enrolments AS enrolment
                           ON enrolment.id=transfer.current_school_enrolment_id
                        WHERE transfer.case_id=1"""
                ).fetchone(),
            )


if __name__ == "__main__":
    unittest.main()
