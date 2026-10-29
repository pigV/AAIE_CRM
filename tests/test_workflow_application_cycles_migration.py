import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from migration_runner import apply_migrations, verify_migrations


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "migrations" / "20260811_017_workflow_application_cycles.sql"
CHECK_PATH = (
    ROOT
    / "migrations"
    / "checks"
    / "20260811_017_workflow_application_cycles_forward_check.sql"
)
NOW = "2026-08-11T10:00:00+10:00"


def build_legacy_fixture(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=MEMORY")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT);
        CREATE TABLE students(student_id TEXT PRIMARY KEY, current_status TEXT);
        CREATE TABLE parent_form_tokens(
          token TEXT PRIMARY KEY,
          student_id TEXT,
          submitted_at TEXT,
          invalidated_at TEXT
        );
        CREATE TABLE student_cases(
          id INTEGER PRIMARY KEY,
          student_id TEXT NOT NULL REFERENCES students(student_id),
          case_type TEXT NOT NULL
        );
        CREATE TABLE contracts(
          id INTEGER PRIMARY KEY,
          student_id TEXT REFERENCES students(student_id),
          contract_status TEXT,
          signed_at TEXT,
          fully_executed_at TEXT
        );
        CREATE TABLE contract_signing_links(
          id INTEGER PRIMARY KEY,
          contract_id INTEGER NOT NULL REFERENCES contracts(id),
          student_id TEXT REFERENCES students(student_id),
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          signed_at TEXT
        );
        CREATE TABLE school_cities(id INTEGER PRIMARY KEY, city_name TEXT);
        CREATE TABLE school_plan_reports(
          id INTEGER PRIMARY KEY,
          student_id TEXT NOT NULL REFERENCES students(student_id),
          city_id INTEGER REFERENCES school_cities(id),
          plan_sector TEXT,
          status TEXT NOT NULL DEFAULT 'generated',
          report_version INTEGER NOT NULL,
          is_current INTEGER NOT NULL DEFAULT 0,
          superseded_by_report_id INTEGER REFERENCES school_plan_reports(id),
          invalidated_at TEXT,
          invalidated_reason TEXT,
          updated_at TEXT
        );
        CREATE UNIQUE INDEX uq_school_plan_current_public_city
        ON school_plan_reports(student_id, city_id)
        WHERE plan_sector='public' AND is_current=1;
        CREATE TABLE school_plan_report_confirmations(
          id INTEGER PRIMARY KEY,
          report_id INTEGER NOT NULL REFERENCES school_plan_reports(id),
          student_id TEXT NOT NULL REFERENCES students(student_id),
          confirmed_at TEXT NOT NULL
        );
        CREATE TABLE school_plan_report_links(
          id INTEGER PRIMARY KEY,
          report_id INTEGER NOT NULL REFERENCES school_plan_reports(id),
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          confirmed_at TEXT
        );
        CREATE TABLE school_plan_confirmed_schools(
          id INTEGER PRIMARY KEY,
          confirmation_id INTEGER NOT NULL REFERENCES school_plan_report_confirmations(id),
          report_id INTEGER NOT NULL REFERENCES school_plan_reports(id),
          student_id TEXT NOT NULL REFERENCES students(student_id),
          school_id INTEGER,
          school_name_snapshot TEXT NOT NULL,
          city_id INTEGER REFERENCES school_cities(id),
          plan_sector TEXT NOT NULL,
          submission_status TEXT NOT NULL DEFAULT 'not_submitted',
          result_status TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE TABLE offers(
          id INTEGER PRIMARY KEY,
          student_id TEXT NOT NULL REFERENCES students(student_id),
          school_id INTEGER,
          school_name TEXT NOT NULL,
          uploaded_at TEXT,
          offer_status TEXT
        );
        CREATE TABLE coe_oshc(
          id INTEGER PRIMARY KEY,
          student_id TEXT NOT NULL REFERENCES students(student_id),
          school_name TEXT,
          document_kind TEXT
        );
        CREATE TABLE visa_cases(
          id INTEGER PRIMARY KEY,
          student_id TEXT NOT NULL REFERENCES students(student_id),
          actual_lodgement_date TEXT,
          result_status TEXT
        );
        CREATE TABLE student_documents(
          id INTEGER PRIMARY KEY,
          student_id TEXT NOT NULL REFERENCES students(student_id),
          document_type TEXT NOT NULL,
          uploaded_at TEXT NOT NULL,
          is_active INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO users(id, username) VALUES(1, 'auditor');
        INSERT INTO students(student_id, current_status) VALUES('S1', 'New');
        INSERT INTO students(student_id, current_status) VALUES('S2', 'New');
        INSERT INTO parent_form_tokens(token) VALUES('parent-token-1');
        INSERT INTO school_cities(id, city_name) VALUES(1, 'Sydney');
        INSERT INTO school_cities(id, city_name) VALUES(2, 'Melbourne');
        INSERT INTO contracts(id, student_id, contract_status)
        VALUES(10, 'S1', 'Draft');
        INSERT INTO school_plan_reports(
          id, student_id, city_id, plan_sector, status, report_version, is_current, updated_at
        ) VALUES(20, 'S1', 1, 'public', 'active', 1, 1, '2026-08-10T10:00:00+10:00');
        INSERT INTO offers(id, student_id, school_name, offer_status)
        VALUES(30, 'S1', 'Example School', 'Received');
        INSERT INTO coe_oshc(id, student_id, school_name, document_kind)
        VALUES(40, 'S1', 'Example School', 'COE');
        INSERT INTO visa_cases(id, student_id, result_status)
        VALUES(50, 'S1', 'Pending');
        """
    )
    return conn


def apply_workflow_migration(conn: sqlite3.Connection) -> None:
    if not MIGRATION_PATH.is_file():
        raise AssertionError(f"missing migration: {MIGRATION_PATH.name}")
    conn.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


class WorkflowApplicationCyclesMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "legacy.db"
        self.conn = build_legacy_fixture(self.database_path)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_migration_accepts_legacy_school_plan_schema_without_report_version(self):
        self.conn.execute("ALTER TABLE school_plan_reports DROP COLUMN report_version")

        apply_workflow_migration(self.conn)

        self.assertIn(
            "application_cycle_id",
            column_names(self.conn, "school_plan_reports"),
        )

    def insert_cycle(
        self,
        uid: str,
        *,
        student_id: str | None = "S1",
        token: str | None = None,
        status: str = "active",
        outcome: str | None = None,
        closed_at: str | None = None,
        cycle_type: str = "initial",
        sequence_no: int = 1,
        source_student_case_id: int | None = None,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO application_cycles(
              cycle_uid,student_id,parent_form_token,source_student_case_id,cycle_type,sequence_no,
              status,outcome,opened_at,closed_at,created_by_user_id,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                uid,
                student_id,
                token,
                source_student_case_id,
                cycle_type,
                sequence_no,
                status,
                outcome,
                NOW,
                closed_at,
                1,
                NOW,
                NOW,
            ),
        )
        return int(cursor.lastrowid)

    def test_migration_preserves_legacy_rows_and_adds_cycle_contract(self):
        legacy_contract_columns = (
            "id,student_id,contract_status,signed_at,fully_executed_at"
        )
        before = tuple(
            self.conn.execute(
                f"SELECT {legacy_contract_columns} FROM contracts WHERE id=10"
            ).fetchone()
        )

        apply_workflow_migration(self.conn)

        expected_tables = {
            "application_cycles",
            "public_application_groups",
            "public_application_preferences",
            "private_school_applications",
            "offer_received_events",
            "cycle_final_acceptances",
            "cycle_final_acceptance_history",
            "final_school_submission_events",
            "visa_decision_events",
        }
        self.assertTrue(expected_tables.issubset(table_names(self.conn)))
        for table in ("contracts", "school_plan_reports", "offers", "coe_oshc", "visa_cases"):
            self.assertIn("application_cycle_id", column_names(self.conn, table))
        self.assertIn("cycle_is_current", column_names(self.conn, "school_plan_reports"))
        after = tuple(
            self.conn.execute(
                f"SELECT {legacy_contract_columns} FROM contracts WHERE id=10"
            ).fetchone()
        )
        self.assertEqual(before, after)
        self.assertIsNone(
            self.conn.execute(
                "SELECT application_cycle_id FROM contracts WHERE id=10"
            ).fetchone()[0]
        )

    def test_stage_one_cycle_needs_no_student_and_student_has_one_active_cycle(self):
        apply_workflow_migration(self.conn)
        stage_one = self.insert_cycle(
            "cycle-parent-1", student_id=None, token="parent-token-1"
        )
        self.assertIsInstance(stage_one, int)
        self.conn.execute(
            "UPDATE application_cycles SET student_id='S1' WHERE id=?", (stage_one,)
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "student identity is immutable"):
            self.conn.execute(
                "UPDATE application_cycles SET student_id='S2' WHERE id=?", (stage_one,)
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_cycle("cycle-second-active", student_id="S1", sequence_no=2)

    def test_cycle_scoped_current_public_plan_preserves_other_cycles_and_cities(self):
        apply_workflow_migration(self.conn)
        first = self.insert_cycle(
            "cycle-history",
            status="closed",
            outcome="granted",
            closed_at=NOW,
        )
        self.conn.execute(
            "INSERT INTO student_cases(id,student_id,case_type) VALUES(70,'S1','transfer')"
        )
        second = self.insert_cycle(
            "cycle-current",
            cycle_type="transfer",
            sequence_no=2,
            source_student_case_id=70,
        )
        self.conn.execute(
            "UPDATE school_plan_reports SET is_current=0, application_cycle_id=?, cycle_is_current=1 WHERE id=20",
            (first,),
        )
        self.conn.execute(
            """
            INSERT INTO school_plan_reports(
              id,student_id,city_id,plan_sector,report_version,is_current,
              application_cycle_id,cycle_is_current
            ) VALUES(21,'S1',1,'public',1,1,?,1)
            """,
            (second,),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO school_plan_reports(
                  id,student_id,city_id,plan_sector,report_version,is_current,
                  application_cycle_id,cycle_is_current
                ) VALUES(22,'S1',1,'public',2,0,?,1)
                """,
                (second,),
            )
        self.assertEqual(
            2,
            self.conn.execute(
                "SELECT count(*) FROM school_plan_reports WHERE cycle_is_current=1"
            ).fetchone()[0],
        )

    def test_offer_and_visa_events_are_idempotent_and_append_only(self):
        apply_workflow_migration(self.conn)
        cycle_id = self.insert_cycle("cycle-events")
        self.conn.execute(
            """
            INSERT INTO private_school_applications(
              application_cycle_id,student_id,school_name_snapshot,status,
              submitted_at,created_by_user_id,created_at,updated_at
            ) VALUES(?, 'S1', 'Example School', 'submitted', ?, 1, ?, ?)
            """,
            (cycle_id, NOW, NOW, NOW),
        )
        private_id = self.conn.execute(
            "SELECT id FROM private_school_applications"
        ).fetchone()[0]
        self.conn.execute(
            """
            INSERT INTO offer_received_events(
              application_cycle_id,student_id,source_kind,private_application_id,
              school_name_snapshot,external_offer_key,idempotency_key,received_at,
              recorded_by_user_id,created_at
            ) VALUES(?, 'S1', 'private', ?, 'Example School', 'offer-1',
                     'cycle-events:private:offer-1', ?, 1, ?)
            """,
            (cycle_id, private_id, NOW, NOW),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO offer_received_events(
                  application_cycle_id,student_id,source_kind,private_application_id,
                  school_name_snapshot,external_offer_key,idempotency_key,received_at,
                  recorded_by_user_id,created_at
                ) VALUES(?, 'S1', 'private', ?, 'Example School', 'offer-1',
                         'cycle-events:private:offer-1', ?, 1, ?)
                """,
                (cycle_id, private_id, NOW, NOW),
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "Offer events are append-only"):
            self.conn.execute(
                "UPDATE offer_received_events SET received_at=? WHERE id=1", (NOW,)
            )

        self.conn.execute(
            "INSERT INTO student_documents(id,student_id,document_type,uploaded_at) VALUES(60,'S1','VisaGrantLetter',?)",
            (NOW,),
        )
        self.conn.execute(
            "UPDATE visa_cases SET application_cycle_id=? WHERE id=50", (cycle_id,)
        )
        self.conn.execute(
            """
            INSERT INTO visa_decision_events(
              application_cycle_id,student_id,visa_case_id,student_document_id,
              decision_type,confirmed_at,idempotency_key,recorded_by_user_id,created_at
            ) VALUES(?, 'S1', 50, 60, 'grant', ?, 'cycle-events:grant', 1, ?)
            """,
            (cycle_id, NOW, NOW),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO visa_decision_events(
                  application_cycle_id,student_id,visa_case_id,student_document_id,
                  decision_type,confirmed_at,idempotency_key,recorded_by_user_id,created_at
                ) VALUES(?, 'S1', 50, 60, 'refusal', ?, 'cycle-events:refusal', 1, ?)
                """,
                (cycle_id, NOW, NOW),
            )

    def test_final_acceptance_is_unique_per_cycle_and_submission_is_explicit(self):
        apply_workflow_migration(self.conn)
        cycle_id = self.insert_cycle("cycle-acceptance")
        self.conn.execute(
            """
            INSERT INTO private_school_applications(
              application_cycle_id,student_id,school_name_snapshot,status,
              submitted_at,created_by_user_id,created_at,updated_at
            ) VALUES(?, 'S1', 'Example School', 'submitted', ?, 1, ?, ?)
            """,
            (cycle_id, NOW, NOW, NOW),
        )
        private_id = self.conn.execute(
            "SELECT id FROM private_school_applications"
        ).fetchone()[0]
        self.conn.execute(
            """
            INSERT INTO offer_received_events(
              application_cycle_id,student_id,source_kind,private_application_id,
              school_name_snapshot,external_offer_key,idempotency_key,received_at,
              recorded_by_user_id,created_at
            ) VALUES(?, 'S1', 'private', ?, 'Example School', 'offer-a',
                     'cycle-acceptance:offer-a', ?, 1, ?)
            """,
            (cycle_id, private_id, NOW, NOW),
        )
        offer_event_id = self.conn.execute(
            "SELECT id FROM offer_received_events"
        ).fetchone()[0]
        self.conn.execute(
            """
            INSERT INTO cycle_final_acceptances(
              application_cycle_id,student_id,offer_received_event_id,
              school_name_snapshot,signed_offer_completed_at,payment_completed_at,
              selected_at,selected_by_user_id,created_at,updated_at
            ) VALUES(?, 'S1', ?, 'Example School', ?, ?, ?, 1, ?, ?)
            """,
            (cycle_id, offer_event_id, NOW, NOW, NOW, NOW, NOW),
        )
        acceptance_id = self.conn.execute(
            "SELECT id FROM cycle_final_acceptances"
        ).fetchone()[0]
        self.assertEqual(
            0,
            self.conn.execute(
                "SELECT count(*) FROM final_school_submission_events"
            ).fetchone()[0],
        )
        self.conn.execute(
            """
            INSERT INTO final_school_submission_events(
              application_cycle_id,final_acceptance_id,submitted_at,
              submission_channel,submission_reference,idempotency_key,
              submitted_by_user_id,created_at
            ) VALUES(?, ?, ?, 'email', 'mail-1', 'cycle-acceptance:submission', 1, ?)
            """,
            (cycle_id, acceptance_id, NOW, NOW),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO final_school_submission_events(
                  application_cycle_id,final_acceptance_id,submitted_at,
                  submission_channel,idempotency_key,submitted_by_user_id,created_at
                ) VALUES(?, ?, ?, 'email', 'cycle-acceptance:submission-2', 1, ?)
                """,
                (cycle_id, acceptance_id, NOW, NOW),
            )

    def test_existing_migration_runner_applies_and_verifies_package(self):
        self.conn.close()
        migrations_dir = Path(self.temp_dir.name) / "migrations"
        checks_dir = migrations_dir / "checks"
        checks_dir.mkdir(parents=True)
        if not MIGRATION_PATH.is_file() or not CHECK_PATH.is_file():
            self.fail("workflow migration package is incomplete")
        shutil.copy2(MIGRATION_PATH, migrations_dir / MIGRATION_PATH.name)
        shutil.copy2(CHECK_PATH, checks_dir / CHECK_PATH.name)

        result = apply_migrations(
            self.database_path,
            migrations_dir,
            applied_by="workflow-migration-test",
            source_commit="test-source",
            formal_database_path=None,
        )
        verified = verify_migrations(self.database_path, migrations_dir)

        self.assertEqual(["20260811_017"], result["applied"])
        self.assertEqual(["20260811_017"], verified["verified"])
        self.assertEqual([], verified["foreign_key_violations"])
        self.conn = sqlite3.connect(self.database_path)


if __name__ == "__main__":
    unittest.main()
