import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from application_cycle import create_student_cycle
from dashboard_workflow_metrics import load_dashboard_workflow_metrics
from tests.test_workflow_application_cycles_migration import (
    NOW,
    apply_workflow_migration,
    build_legacy_fixture,
)
from workflow_projection import load_current_workflow_projection
from workflow_reconciliation import (
    reconcile_structural_integrity,
    reconcile_workflow,
    reconciliation_to_json,
    reconciliation_to_markdown,
)


def codes(issues):
    return {issue.code for issue in issues}


class WorkflowReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "reconciliation.db"
        self.conn = build_legacy_fixture(self.path)
        self.conn.row_factory = sqlite3.Row
        apply_workflow_migration(self.conn)
        self.conn.execute(
            "CREATE TABLE workflow_status(student_id TEXT PRIMARY KEY,current_stage TEXT)"
        )

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_detects_application_coe_visa_and_current_stage_mismatches(self):
        cycle = create_student_cycle(
            self.conn, "S1", "initial", NOW, 1, cycle_uid="reconcile-facts"
        )
        self.conn.execute(
            "INSERT INTO workflow_status VALUES('S1','02_contract_creation')"
        )
        self.conn.execute(
            """
            INSERT INTO private_school_applications(
              application_cycle_id,student_id,school_name_snapshot,status,
              submitted_at,created_by_user_id,created_at,updated_at
            ) VALUES(?,'S1','Private A','submitted',?,1,?,?)
            """,
            (cycle.id, NOW, NOW, NOW),
        )
        self.conn.execute(
            "UPDATE coe_oshc SET application_cycle_id=? WHERE id=40", (cycle.id,)
        )
        self.conn.execute(
            "UPDATE visa_cases SET application_cycle_id=?,actual_lodgement_date=? WHERE id=50",
            (cycle.id, NOW),
        )

        issue_codes = codes(reconcile_workflow(self.conn))

        self.assertIn("CURRENT_STAGE_FACT_MISMATCH", issue_codes)
        self.assertIn("APPLICATION_SUBMITTED_STAGE_MISMATCH", issue_codes)
        self.assertIn("COE_STAGE_MISMATCH", issue_codes)
        self.assertIn("VISA_SUBMITTED_STAGE_MISMATCH", issue_codes)

    def test_detects_decision_on_active_cycle_and_submission_evidence_conflict(self):
        cycle = create_student_cycle(
            self.conn, "S1", "initial", NOW, 1, cycle_uid="reconcile-decision"
        )
        self.conn.execute(
            "INSERT INTO workflow_status VALUES('S1','08_offer_acceptance')"
        )
        self.conn.execute(
            "UPDATE visa_cases SET application_cycle_id=? WHERE id=50", (cycle.id,)
        )
        self.conn.execute(
            """
            INSERT INTO student_documents(id,student_id,document_type,uploaded_at,is_active)
            VALUES(81,'S1','VisaGrantLetter',?,1)
            """,
            (NOW,),
        )
        self.conn.execute(
            """
            INSERT INTO visa_decision_events(
              application_cycle_id,student_id,visa_case_id,student_document_id,
              decision_type,confirmed_at,idempotency_key,created_at
            ) VALUES(?,'S1',50,81,'grant',?,'reconcile-grant',?)
            """,
            (cycle.id, NOW, NOW),
        )

        issue_codes = codes(reconcile_workflow(self.conn))

        self.assertIn("VISA_DECISION_ACTIVE_CYCLE", issue_codes)

    def test_structural_scan_detects_all_duplicate_classes(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(
                """
                CREATE TABLE cycle_final_acceptances(id INTEGER,application_cycle_id INTEGER);
                INSERT INTO cycle_final_acceptances VALUES(1,7),(2,7);
                CREATE TABLE school_plan_reports(
                  id INTEGER,application_cycle_id INTEGER,city_id INTEGER,
                  plan_sector TEXT,cycle_is_current INTEGER
                );
                INSERT INTO school_plan_reports VALUES
                  (1,7,3,'public',1),(2,7,3,'public',1);
                CREATE TABLE offer_received_events(
                  id INTEGER,application_cycle_id INTEGER,source_kind TEXT,
                  public_application_group_id INTEGER,private_application_id INTEGER,
                  school_name_snapshot TEXT,received_at TEXT
                );
                INSERT INTO offer_received_events VALUES
                  (1,7,'private',NULL,9,'A','2026-08-05T10:00:00+10:00'),
                  (2,7,'private',NULL,9,'A','2026-08-05T10:00:00+10:00');
                CREATE TABLE visa_decision_events(
                  id INTEGER,application_cycle_id INTEGER,decision_type TEXT
                );
                INSERT INTO visa_decision_events VALUES(1,7,'grant'),(2,7,'grant');
                CREATE TABLE contracts(id INTEGER,student_id TEXT,application_cycle_id INTEGER);
                INSERT INTO contracts VALUES(5,'S1',NULL);
                """
            )

            issue_codes = codes(reconcile_structural_integrity(conn))

            self.assertIn("MULTIPLE_FINAL_ACCEPTED_SCHOOLS", issue_codes)
            self.assertIn("MULTIPLE_CURRENT_PUBLIC_PLANS", issue_codes)
            self.assertIn("DUPLICATE_OFFER_EVENT", issue_codes)
            self.assertIn("DUPLICATE_GRANT_EVENT", issue_codes)
            self.assertIn("UNOWNED_LEGACY_CYCLE_RECORD", issue_codes)
        finally:
            conn.close()

    def test_detects_duplicate_projection_and_dashboard_event_mismatch(self):
        create_student_cycle(
            self.conn, "S1", "initial", NOW, 1, cycle_uid="reconcile-projection"
        )
        projection = load_current_workflow_projection(self.conn)
        metrics = load_dashboard_workflow_metrics(self.conn, today=date(2026, 8, 7))
        observed = {
            "active_workflow_count": metrics.active_workflow_count,
            "week_offer_count": metrics.week_offer_count + 1,
            "week_grant_count": metrics.week_grant_count,
            "month_grant_count": metrics.month_grant_count,
        }

        issue_codes = codes(
            reconcile_workflow(
                self.conn,
                observed_projection=projection + projection,
                observed_metrics=observed,
                today=date(2026, 8, 7),
            )
        )

        self.assertIn("DUPLICATE_CYCLE_PROJECTION", issue_codes)
        self.assertIn("DASHBOARD_METRICS_SOURCE_EVENT_MISMATCH", issue_codes)

    def test_reports_are_stably_ordered_and_read_only(self):
        first = reconcile_workflow(self.conn)
        second = reconcile_workflow(self.conn)

        self.assertEqual(reconciliation_to_json(first), reconciliation_to_json(second))
        self.assertIn("Workflow Reconciliation Report", reconciliation_to_markdown(first))


if __name__ == "__main__":
    unittest.main()
