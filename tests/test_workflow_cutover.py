import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from dashboard_kpi_cards import load_dashboard_kpis
from tests.test_workflow_application_cycles_migration import (
    apply_workflow_migration,
    build_legacy_fixture,
)


READY = {
    "migration_applied": True,
    "deterministic_backfill_complete": True,
    "current_active_ownership_resolved": True,
    "reconciliation_blockers_cleared": True,
    "blocking_ambiguity_resolved": True,
    "dashboard_parity_verified": True,
    "resolver_parity_verified": True,
    "foreign_key_check_passed": True,
    "quick_check_passed": True,
}


class WorkflowCutoverTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "cutover.db"
        self.conn = build_legacy_fixture(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_by INTEGER,updated_at TEXT NOT NULL)"
        )
        self.conn.execute(
            "CREATE TABLE workflow_status(student_id TEXT PRIMARY KEY,current_stage TEXT)"
        )
        self.conn.execute("ALTER TABLE students ADD COLUMN deleted_at TEXT")
        self.conn.execute("ALTER TABLE offers ADD COLUMN document_type TEXT")
        self.conn.execute("ALTER TABLE offers ADD COLUMN offer_date TEXT")
        self.conn.execute("ALTER TABLE offers ADD COLUMN accepted_date TEXT")
        self.conn.execute("ALTER TABLE offers ADD COLUMN original_filename TEXT")
        self.conn.execute("ALTER TABLE offers ADD COLUMN signed_offer_file_path TEXT")
        self.conn.execute("ALTER TABLE offers ADD COLUMN payment_receipt_file_path TEXT")
        self.conn.execute("ALTER TABLE offers ADD COLUMN deposit_paid_date TEXT")
        self.conn.execute("ALTER TABLE contracts ADD COLUMN deleted_at TEXT")
        self.conn.execute("ALTER TABLE contracts ADD COLUMN voided_at TEXT")
        self.conn.execute("UPDATE contracts SET contract_status='FullyExecuted',fully_executed_at='2026-08-11'")
        self.conn.execute("UPDATE offers SET document_type='Offer',uploaded_at='2026-08-11'")
        self.conn.execute("INSERT INTO workflow_status VALUES('S1','建档')")
        self.conn.execute("INSERT INTO workflow_status VALUES('S2','Visa granted')")
        self.conn.execute(
            "CREATE TABLE visas(id INTEGER PRIMARY KEY,student_id TEXT,visa_status TEXT,visa_grant_date TEXT)"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def _legacy_kpis(self):
        return load_dashboard_kpis(
            self.conn,
            date(2026, 8, 11),
            final_workflow_stage="签证下签或拒签",
            workflow_stage_aliases={"Visa granted": "签证下签或拒签"},
        )

    def test_case_a_legacy_database_defaults_to_legacy_compatible(self):
        from workflow_cutover import WorkflowReadMode, resolve_workflow_read_mode

        self.assertEqual(WorkflowReadMode.LEGACY_COMPATIBLE, resolve_workflow_read_mode(self.conn))
        self.assertEqual(1, self._legacy_kpis().in_progress_students)

    def test_case_b_migrated_empty_schema_does_not_collapse_visible_metrics(self):
        before = self._legacy_kpis()
        apply_workflow_migration(self.conn)

        after = self._legacy_kpis()

        self.assertEqual(before.in_progress_students, after.in_progress_students)
        self.assertEqual(before.signed_this_week, after.signed_this_week)
        self.assertEqual(before.offers_uploaded_this_week, after.offers_uploaded_this_week)

    def test_case_c_and_d_incomplete_evidence_cannot_activate(self):
        from workflow_cutover import CutoverNotReadyError, activate_workflow_v2

        apply_workflow_migration(self.conn)
        for evidence in ({}, {**READY, "reconciliation_blockers_cleared": False}):
            with self.assertRaises(CutoverNotReadyError):
                activate_workflow_v2(self.conn, evidence=evidence, actor="test")

    def test_case_e_f_g_explicit_activation_and_disable_are_reversible(self):
        from workflow_cutover import (
            WorkflowReadMode,
            activate_workflow_v2,
            disable_workflow_v2,
            resolve_workflow_read_mode,
        )

        apply_workflow_migration(self.conn)
        activate_workflow_v2(self.conn, evidence=READY, actor="test")
        self.assertEqual(WorkflowReadMode.CANONICAL_V2, resolve_workflow_read_mode(self.conn))

        disable_workflow_v2(self.conn, actor="test rollback")

        self.assertEqual(WorkflowReadMode.LEGACY_COMPATIBLE, resolve_workflow_read_mode(self.conn))
        self.assertEqual(1, self._legacy_kpis().in_progress_students)

    def test_malformed_state_fails_closed(self):
        from workflow_cutover import WorkflowReadMode, resolve_workflow_read_mode

        apply_workflow_migration(self.conn)
        self.conn.execute(
            "INSERT INTO system_settings VALUES('WORKFLOW_V2_CUTOVER_STATE','not-json',NULL,'2026-08-11')"
        )

        self.assertEqual(WorkflowReadMode.LEGACY_COMPATIBLE, resolve_workflow_read_mode(self.conn))


if __name__ == "__main__":
    unittest.main()
