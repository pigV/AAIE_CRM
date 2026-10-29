import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from application_cycle import create_student_cycle
from dashboard_workflow_metrics import load_dashboard_workflow_metrics
from tests import test_student_case_phase2_transfer as transfer_tests
from workflow_visa_events import record_first_valid_visa_decision


NOW = "2026-08-16T10:00:00+10:00"


class DashboardCanonicalVisaLinkageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "canonical-visa.db"
        helper = transfer_tests.TransferPhase2MigrationTests()
        helper.connect = transfer_tests.TransferPhase2MigrationTests.connect
        helper._create_legacy_fixture(self.database_path)
        transfer_tests.apply_all_test_migrations_with_rebuild_backup(
            self.database_path, self.root, label="canonical-visa-linkage", through="20260909_038"
        )
        self.conn = sqlite3.connect(self.database_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        def add_column_if_missing(table, name, definition):
            columns = {
                str(row[1])
                for row in self.conn.execute(f"PRAGMA table_info('{table}')")
            }
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                )

        for table, name, definition in (
            ("visas", "visa_status", "TEXT"),
            ("visas", "visa_grant_date", "TEXT"),
            ("student_documents", "document_type", "TEXT"),
            ("student_documents", "uploaded_at", "TEXT"),
            ("student_documents", "is_active", "INTEGER NOT NULL DEFAULT 1"),
            ("contracts", "contract_status", "TEXT"),
            ("contracts", "fully_executed_at", "TEXT"),
            ("visa_cases", "status", "TEXT DEFAULT 'Draft'"),
            ("visa_cases", "decision_status", "TEXT DEFAULT 'Pending'"),
            ("visa_cases", "result_status", "TEXT DEFAULT 'Pending'"),
            ("visa_cases", "grant_date", "TEXT"),
            ("visa_cases", "result_date", "TEXT"),
            ("visa_cases", "created_at", "TEXT"),
            ("visa_cases", "updated_at", "TEXT"),
        ):
            add_column_if_missing(table, name, definition)
        self.cycle = create_student_cycle(
            self.conn,
            "student-1",
            "initial",
            NOW,
            1,
            cycle_uid="canonical-visa-cycle",
        )
        self.visa_case_id = int(self.conn.execute(
            """INSERT INTO visa_cases(visa_case_name,student_id,actual_lodgement_date,application_cycle_id,created_at,updated_at)
               VALUES('Canonical Visa Fixture','student-1','2026-07-20',?,'2026-08-16','2026-08-16')""",
            (self.cycle.id,),
        ).lastrowid)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_normal_granted_visa_record_creates_canonical_decision_with_business_date(self):
        visa_record_id = int(self.conn.execute(
            """INSERT INTO visas(student_id,visa_status,visa_grant_date)
               VALUES('student-1','Granted','2026-08-12')"""
        ).lastrowid)

        result = record_first_valid_visa_decision(
            self.conn,
            self.cycle.id,
            visa_case_id=self.visa_case_id,
            visa_record_id=visa_record_id,
            student_document_id=None,
            decision_type="grant",
            decision_date="2026-08-12",
            confirmed_at=NOW,
            actor_user_id=1,
        )

        self.assertTrue(result.created)
        self.assertEqual("2026-08-12", result.event.decision_date)
        self.assertEqual(visa_record_id, result.event.visa_record_id)
        self.assertEqual("visa_record", result.event.decision_source)
        self.assertIsNone(result.event.student_document_id)
        visa_case = self.conn.execute(
            """SELECT status,decision_status,result_status,grant_date,result_date
                 FROM visa_cases WHERE id=?""",
            (self.visa_case_id,),
        ).fetchone()
        self.assertEqual(
            ("Granted", "Granted", "Granted", "2026-08-12", "2026-08-12"),
            tuple(visa_case),
        )

    def test_dashboard_uses_granted_date_not_record_or_evidence_timestamp(self):
        visa_record_id = int(self.conn.execute(
            """INSERT INTO visas(student_id,visa_status,visa_grant_date)
               VALUES('student-1','Granted','2026-08-05')"""
        ).lastrowid)
        record_first_valid_visa_decision(
            self.conn,
            self.cycle.id,
            visa_case_id=self.visa_case_id,
            visa_record_id=visa_record_id,
            student_document_id=None,
            decision_type="grant",
            decision_date="2026-08-05",
            confirmed_at="2026-07-30T10:00:00+10:00",
            actor_user_id=1,
        )

        metrics = load_dashboard_workflow_metrics(
            self.conn, today=date(2026, 8, 7)
        )

        self.assertEqual(1, metrics.week_grant_count)
        self.assertEqual(1, metrics.month_grant_count)

    def test_refused_decision_never_enters_granted_kpi(self):
        visa_record_id = int(self.conn.execute(
            """INSERT INTO visas(student_id,visa_status,visa_grant_date)
               VALUES('student-1','Refused','2026-08-05')"""
        ).lastrowid)
        record_first_valid_visa_decision(
            self.conn,
            self.cycle.id,
            visa_case_id=self.visa_case_id,
            visa_record_id=visa_record_id,
            student_document_id=None,
            decision_type="refusal",
            decision_date="2026-08-05",
            confirmed_at=NOW,
            actor_user_id=1,
        )

        metrics = load_dashboard_workflow_metrics(
            self.conn, today=date(2026, 8, 7)
        )

        self.assertEqual(0, metrics.week_grant_count)
        self.assertEqual(0, metrics.month_grant_count)


if __name__ == "__main__":
    unittest.main()
