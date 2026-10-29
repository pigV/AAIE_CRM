from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import migration_runner


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations/20260904_035_transfer_v2_final_schema.sql"
FORWARD_CHECK = ROOT / "migrations/checks/20260904_035_transfer_v2_final_schema_forward_check.sql"
ROLLBACK = ROOT / "migrations/rollback/20260904_035_transfer_v2_final_schema_rollback.sql"
CUTOVER = "2026-09-04T12:00:00+10:00"


def fingerprint(connection, table):
    rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class TransferV2Migration035Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "transfer-v2-035.sqlite3"
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(
                """
                CREATE TABLE users(id INTEGER PRIMARY KEY);
                CREATE TABLE students(student_id TEXT PRIMARY KEY);
                CREATE TABLE schools(id INTEGER PRIMARY KEY, school_name TEXT NOT NULL);
                CREATE TABLE parent_form_tokens(
                  token TEXT PRIMARY KEY,
                  consultant_id INTEGER REFERENCES users(id),
                  student_hint TEXT,
                  expires_at TEXT NOT NULL,
                  submitted_at TEXT,
                  invalidated_at TEXT,
                  student_id TEXT REFERENCES students(student_id),
                  created_by INTEGER NOT NULL REFERENCES users(id),
                  created_at TEXT NOT NULL
                );
                CREATE TABLE student_cases(
                  id INTEGER PRIMARY KEY,
                  student_id TEXT NOT NULL REFERENCES students(student_id),
                  case_type TEXT NOT NULL
                );
                CREATE TABLE application_cycles(
                  id INTEGER PRIMARY KEY,
                  cycle_uid TEXT NOT NULL UNIQUE,
                  student_id TEXT REFERENCES students(student_id),
                  parent_form_token TEXT REFERENCES parent_form_tokens(token),
                  source_student_case_id INTEGER REFERENCES student_cases(id),
                  cycle_type TEXT NOT NULL,
                  sequence_no INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  outcome TEXT,
                  opened_at TEXT NOT NULL,
                  closed_at TEXT,
                  created_at TEXT NOT NULL,
                  education_stream TEXT NOT NULL DEFAULT 'school_education'
                );
                CREATE TABLE contracts(
                  id INTEGER PRIMARY KEY,
                  student_id TEXT REFERENCES students(student_id),
                  contract_type TEXT NOT NULL,
                  contract_status TEXT NOT NULL,
                  application_cycle_id INTEGER REFERENCES application_cycles(id),
                  deleted_at TEXT,
                  voided_at TEXT
                );
                CREATE TABLE transfer_cases(
                  case_id INTEGER PRIMARY KEY REFERENCES student_cases(id),
                  v2_version INTEGER,
                  final_application_id INTEGER
                );
                CREATE TABLE transfer_school_applications(
                  id INTEGER PRIMARY KEY,
                  case_id INTEGER NOT NULL REFERENCES transfer_cases(case_id),
                  school_id INTEGER REFERENCES schools(id),
                  school_name_snapshot TEXT NOT NULL,
                  city_key TEXT,
                  school_sector TEXT,
                  preference_order INTEGER,
                  application_status TEXT NOT NULL,
                  notes TEXT,
                  is_final INTEGER NOT NULL DEFAULT 0,
                  created_by INTEGER NOT NULL REFERENCES users(id),
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  branch_uid TEXT,
                  application_cycle_id INTEGER REFERENCES application_cycles(id),
                  original_school_id INTEGER REFERENCES schools(id),
                  school_state_snapshot TEXT,
                  school_region_snapshot TEXT,
                  school_type_snapshot TEXT,
                  updated_by INTEGER REFERENCES users(id)
                );
                CREATE TRIGGER transfer_school_applications_no_delete
                BEFORE DELETE ON transfer_school_applications BEGIN
                  SELECT RAISE(ABORT,'transfer applications are retained permanently');
                END;
                CREATE TABLE transfer_target_state_events(
                  id INTEGER PRIMARY KEY,
                  event_uid TEXT NOT NULL UNIQUE,
                  application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id),
                  branch_id INTEGER NOT NULL REFERENCES transfer_school_applications(id),
                  previous_state TEXT CHECK(previous_state IS NULL OR previous_state IN (
                    'materials_preparation','application_in_progress','offer','accepted','paid',
                    'coe','completed','rejected','withdrawn','closed_after_transfer_completion'
                  )),
                  new_state TEXT NOT NULL CHECK(new_state IN (
                    'materials_preparation','application_in_progress','offer','accepted','paid',
                    'coe','completed','rejected','withdrawn','closed_after_transfer_completion'
                  )),
                  event_type TEXT NOT NULL,
                  missing_checklist_json TEXT,
                  note TEXT,
                  actor_user_id INTEGER NOT NULL REFERENCES users(id),
                  occurred_at TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX idx_transfer_target_state_current
                  ON transfer_target_state_events(branch_id,occurred_at DESC,id DESC);
                CREATE TRIGGER transfer_target_state_events_no_update
                BEFORE UPDATE ON transfer_target_state_events BEGIN
                  SELECT RAISE(ABORT,'Transfer target state events are append-only');
                END;
                CREATE TRIGGER transfer_target_state_events_no_delete
                BEFORE DELETE ON transfer_target_state_events BEGIN
                  SELECT RAISE(ABORT,'Transfer target state events are append-only');
                END;
                CREATE TABLE offers(id INTEGER PRIMARY KEY, student_id TEXT NOT NULL);
                CREATE TABLE offer_received_events(
                  id INTEGER PRIMARY KEY,
                  application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id),
                  transfer_branch_id INTEGER REFERENCES transfer_school_applications(id)
                );
                CREATE TABLE offer_received_event_state_events(
                  id INTEGER PRIMARY KEY,
                  offer_received_event_id INTEGER NOT NULL REFERENCES offer_received_events(id),
                  state TEXT NOT NULL
                );
                CREATE TABLE cycle_final_acceptances(
                  id INTEGER PRIMARY KEY,
                  application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id),
                  transfer_branch_id INTEGER REFERENCES transfer_school_applications(id),
                  payment_completed_at TEXT
                );
                CREATE TABLE payments(
                  id INTEGER PRIMARY KEY,
                  student_id TEXT NOT NULL REFERENCES students(student_id),
                  payment_category TEXT NOT NULL,
                  amount REAL,
                  paid_date TEXT,
                  receipt_file_path TEXT,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE case_record_links(
                  id INTEGER PRIMARY KEY,
                  case_id INTEGER NOT NULL REFERENCES transfer_cases(case_id),
                  transfer_application_id INTEGER REFERENCES transfer_school_applications(id),
                  record_type TEXT NOT NULL,
                  record_id INTEGER NOT NULL,
                  validity_status TEXT NOT NULL,
                  created_by INTEGER NOT NULL REFERENCES users(id),
                  created_at TEXT NOT NULL
                );
                CREATE TABLE coe_oshc(
                  id INTEGER PRIMARY KEY,
                  student_id TEXT NOT NULL REFERENCES students(student_id),
                  application_cycle_id INTEGER REFERENCES application_cycles(id)
                );
                CREATE TABLE private_school_applications(
                  id INTEGER PRIMARY KEY,
                  application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id),
                  status TEXT
                );
                CREATE TABLE public_application_groups(
                  id INTEGER PRIMARY KEY,
                  application_cycle_id INTEGER NOT NULL REFERENCES application_cycles(id),
                  status TEXT
                );
                CREATE TABLE schema_migrations(
                  version TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  checksum_sha256 TEXT NOT NULL,
                  applied_at TEXT NOT NULL,
                  execution_ms INTEGER NOT NULL,
                  applied_by TEXT NOT NULL,
                  source_commit TEXT NOT NULL
                );

                INSERT INTO users VALUES(1),(2);
                INSERT INTO students VALUES('S-LEGACY'),('S-NEW');
                INSERT INTO schools VALUES(10,'Legacy School'),(20,'Target School');
                INSERT INTO parent_form_tokens VALUES(
                  'legacy-null',1,'Legacy','2026-10-01',NULL,NULL,NULL,1,'2026-08-01'
                );
                INSERT INTO student_cases VALUES(1,'S-LEGACY','transfer');
                INSERT INTO application_cycles VALUES(
                  1,'legacy-cycle','S-LEGACY',NULL,1,'transfer',1,'active',NULL,
                  '2026-08-01',NULL,'2026-08-01','school_education'
                );
                INSERT INTO transfer_cases VALUES(1,2,NULL);
                INSERT INTO transfer_school_applications VALUES(
                  1,1,20,'Target School','melbourne','private',1,'submitted',NULL,0,1,
                  '2026-08-01','2026-08-01','legacy-branch',1,20,'VIC','Melbourne',
                  'Independent',1
                );
                INSERT INTO transfer_target_state_events VALUES(
                  1,'legacy-state',1,1,NULL,'application_in_progress','legacy',NULL,NULL,1,
                  '2026-08-01','2026-08-01'
                );
                INSERT INTO schema_migrations VALUES(
                  '20260901_034','revenue_client_payment_receipt',
                  'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                  '2026-09-01T00:00:00+10:00',1,'test','base'
                );
                """
            )
            connection.commit()
            self.before = {
                table: fingerprint(connection, table)
                for table in (
                    "parent_form_tokens",
                    "transfer_school_applications",
                    "transfer_target_state_events",
                    "payments",
                )
            }

    def apply(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.executescript(MIGRATION.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO schema_migrations VALUES(?,?,?,?,?,?,?)",
                (
                    "20260904_035",
                    "transfer_v2_final_schema",
                    "b" * 64,
                    CUTOVER,
                    1,
                    "test",
                    "candidate",
                ),
            )
            connection.commit()
            connection.execute("PRAGMA foreign_keys=ON")

    def connect(self):
        connection = sqlite3.connect(self.database_path)
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def test_package_uses_next_migration_slot(self):
        self.assertTrue(MIGRATION.is_file())
        self.assertTrue(FORWARD_CHECK.is_file())
        self.assertTrue(ROLLBACK.is_file())
        versions = sorted(path.name for path in (ROOT / "migrations").glob("*.sql"))
        self.assertEqual("20260901_034_revenue_client_payment_receipt.sql", versions[versions.index(MIGRATION.name) - 1])

    def test_migration_runner_allows_the_approved_rebuild_mode(self):
        migration = next(
            item
            for item in migration_runner.discover_migrations(ROOT / "migrations")
            if item.version == "20260904_035"
        )
        self.assertEqual("sqlite_fk_off_rebuild", migration.mode)

    def test_preserves_all_legacy_rows_and_adds_only_approved_schema(self):
        self.apply()
        connection = self.connect()
        self.assertEqual("ok", connection.execute("PRAGMA quick_check").fetchone()[0])
        self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
        self.assertEqual("legacy-null", connection.execute(
            "SELECT token FROM parent_form_tokens WHERE intake_type IS NULL"
        ).fetchone()[0])
        self.assertEqual("application_in_progress", connection.execute(
            "SELECT new_state FROM transfer_target_state_events WHERE id=1"
        ).fetchone()[0])
        self.assertEqual(0, connection.execute(
            "SELECT COUNT(*) FROM parent_form_intake_type_change_events"
        ).fetchone()[0])
        self.assertEqual(0, connection.execute(
            "SELECT COUNT(*) FROM transfer_payment_confirmations"
        ).fetchone()[0])
        branch = connection.execute(
            "SELECT removed_at,removed_by FROM transfer_school_applications WHERE id=1"
        ).fetchone()
        self.assertEqual((None, None), tuple(branch))

    def test_new_parent_tokens_require_type_and_historical_null_cannot_change(self):
        self.apply()
        connection = self.connect()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "intake type is required"):
            connection.execute(
                "INSERT INTO parent_form_tokens(token,expires_at,created_by,created_at) VALUES('missing','2026-10-01',1,'2026-09-05')"
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "correction event"):
            connection.execute(
                "UPDATE parent_form_tokens SET intake_type='new_student_transfer' WHERE token='legacy-null'"
            )

    def test_safe_non_null_parent_correction_is_atomic_and_append_only(self):
        self.apply()
        connection = self.connect()
        connection.execute(
            "INSERT INTO parent_form_tokens(token,expires_at,created_by,created_at,intake_type) VALUES('safe','2026-10-01',1,'2026-09-05','ordinary_new_application')"
        )
        connection.execute(
            "INSERT INTO parent_form_intake_type_change_events(parent_form_token,previous_intake_type,new_intake_type,changed_by,changed_at) VALUES('safe','ordinary_new_application','new_student_transfer',2,'2026-09-05T10:00:00+10:00')"
        )
        self.assertEqual("new_student_transfer", connection.execute(
            "SELECT intake_type FROM parent_form_tokens WHERE token='safe'"
        ).fetchone()[0])
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            connection.execute("DELETE FROM parent_form_intake_type_change_events")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            connection.execute(
                "UPDATE parent_form_intake_type_change_events SET changed_at='changed'"
            )

    def test_parent_correction_gate_blocks_formal_cycle_and_active_transfer(self):
        self.apply()
        connection = self.connect()
        connection.execute(
            "INSERT INTO parent_form_tokens(token,expires_at,student_id,created_by,created_at,intake_type) VALUES('blocked','2026-10-01','S-NEW',1,'2026-09-05','ordinary_new_application')"
        )
        connection.execute(
            "INSERT INTO application_cycles VALUES(2,'formal-cycle','S-NEW','blocked',NULL,'initial',1,'active',NULL,'2026-09-05',NULL,'2026-09-05','school_education')"
        )
        connection.execute(
            "INSERT INTO offer_received_events VALUES(2,2,NULL)"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "correction gate"):
            connection.execute(
                "INSERT INTO parent_form_intake_type_change_events(parent_form_token,previous_intake_type,new_intake_type,changed_by,changed_at) VALUES('blocked','ordinary_new_application','new_student_transfer',2,'2026-09-05')"
            )

    def test_target_removal_readd_and_submitted_lock(self):
        self.apply()
        connection = self.connect()
        connection.execute(
            "INSERT INTO student_cases VALUES(2,'S-NEW','transfer')"
        )
        connection.execute(
            "INSERT INTO application_cycles VALUES(2,'new-cycle','S-NEW',NULL,2,'transfer',1,'active',NULL,'2026-09-05',NULL,'2026-09-05','school_education')"
        )
        connection.execute("INSERT INTO transfer_cases VALUES(2,2,NULL)")
        branch_values = (2, 2, 20, "Target School", "melbourne", "private", 1, "preparing", None, 0, 1, "2026-09-05", "2026-09-05", "draft-1", 2, 20, "VIC", "Melbourne", "Independent", 1)
        connection.execute(
            "INSERT INTO transfer_school_applications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
            branch_values,
        )
        connection.execute(
            "UPDATE transfer_school_applications SET removed_at='2026-09-05',removed_by=1 WHERE id=2"
        )
        readd = list(branch_values)
        readd[0] = 3
        readd[13] = "draft-2"
        connection.execute(
            "INSERT INTO transfer_school_applications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
            tuple(readd),
        )
        connection.execute(
            "INSERT INTO transfer_target_state_events(event_uid,application_cycle_id,branch_id,previous_state,new_state,event_type,actor_user_id,occurred_at,created_at) VALUES('submitted',2,3,NULL,'application_submitted','manual_status_change',1,'2026-09-05','2026-09-05')"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "submitted target"):
            connection.execute(
                "UPDATE transfer_school_applications SET removed_at='2026-09-06',removed_by=1 WHERE id=3"
            )

    def test_payment_confirmation_requires_authoritative_complete_payment_link(self):
        self.apply()
        connection = self.connect()
        connection.execute("INSERT INTO student_cases VALUES(2,'S-NEW','transfer')")
        connection.execute(
            "INSERT INTO application_cycles VALUES(2,'new-cycle','S-NEW',NULL,2,'transfer',1,'active',NULL,'2026-09-05',NULL,'2026-09-05','school_education')"
        )
        connection.execute("INSERT INTO transfer_cases VALUES(2,2,NULL)")
        connection.execute(
            "INSERT INTO transfer_school_applications VALUES(2,2,20,'Target School','melbourne','private',1,'preparing',NULL,0,1,'2026-09-05','2026-09-05','branch-2',2,20,'VIC','Melbourne','Independent',1,NULL,NULL)"
        )
        connection.execute(
            "INSERT INTO payments VALUES(1,'S-NEW','Offer Deposit',1000,'2026-09-05','/receipt.pdf','2026-09-05')"
        )
        connection.execute(
            "INSERT INTO case_record_links VALUES(1,2,2,'payment',1,'current',1,'2026-09-05')"
        )
        connection.execute(
            "INSERT INTO transfer_payment_confirmations(payment_link_id,confirmed_by,confirmed_at) VALUES(1,1,'2026-09-05')"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            connection.execute("DELETE FROM transfer_payment_confirmations")


if __name__ == "__main__":
    unittest.main()
