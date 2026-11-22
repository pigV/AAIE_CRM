"""Parent submission opens an audited case, without creating financial items."""
import json
import sqlite3
import unittest

from application_cycle import record_application_lifecycle_event, CycleConflictError
from revenue.cycle_integration import ensure_revenue_case_for_cycle
from tests.revenue_test_support import RevenueDatabaseFixture


NOW = "2026-11-22T02:00:00Z"


class ParentRevenueCaseTests(RevenueDatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.c = self.connect()
        for column in ("parent_form_token TEXT", "source_student_case_id INTEGER",
                       "sequence_no INTEGER DEFAULT 1", "outcome TEXT", "opened_at TEXT",
                       "closed_at TEXT", "created_by_user_id INTEGER", "created_by_name TEXT",
                       "updated_by_user_id INTEGER", "updated_by_name TEXT", "updated_at TEXT"):
            self.c.execute("ALTER TABLE application_cycles ADD COLUMN " + column)
        self.c.execute("CREATE TABLE parent_form_tokens(token TEXT PRIMARY KEY,student_id TEXT,created_by INTEGER,submitted_at TEXT,invalidated_at TEXT)")
        self.c.execute("INSERT INTO parent_form_tokens VALUES('parent-link','S-REVENUE',2,?,NULL)", (NOW,))
        self.c.execute("UPDATE application_cycles SET parent_form_token='parent-link',created_at=?,opened_at=?,updated_at=? WHERE id=100", (NOW, NOW, NOW))
        self.c.execute("UPDATE revenue_module_state SET status='active',cutover_at='2026-08-29T00:00:00Z'")
        self.c.commit()

    def submit(self, **kwargs):
        return record_application_lifecycle_event(
            self.c, "PARENT_FORM_SUBMITTED", occurred_at=NOW, actor_user_id=None,
            student_id=kwargs.get("student_id", "S-REVENUE"), parent_form_token="parent-link")

    def test_parent_submission_and_replay_open_one_case_with_system_provenance(self):
        self.submit()
        self.submit()
        cases = self.c.execute("SELECT * FROM revenue_commercial_cases").fetchall()
        self.assertEqual(1, len(cases))
        self.assertEqual(2, cases[0]["opened_by"])
        audit = self.c.execute("SELECT * FROM revenue_audit_events WHERE action='commercial_case_opened'").fetchall()
        self.assertEqual(1, len(audit))
        source = json.loads(audit[0]["after_json"])["integration_source"]
        self.assertEqual("system", source["actor_kind"])
        self.assertEqual("parent_form.submitted", source["event"])
        self.assertEqual("parent_form_tokens.created_by", source["operator_authority"])
        self.assertNotIn("parent-link", json.dumps(source))
        for table in ("revenue_fee_items", "revenue_commission_lines", "revenue_invoices", "revenue_payments"):
            self.assertEqual(0, self.c.execute("SELECT COUNT(*) FROM " + table).fetchone()[0])

    def test_new_parent_submission_creates_cycle_and_case(self):
        self.c.execute("INSERT INTO students VALUES('S-NEW')")
        self.c.execute("INSERT INTO parent_form_tokens VALUES('new-link','S-NEW',2,?,NULL)", (NOW,))
        cycle = record_application_lifecycle_event(self.c, "PARENT_FORM_SUBMITTED",
            occurred_at=NOW, actor_user_id=None, student_id="S-NEW", parent_form_token="new-link")
        self.assertEqual(1, self.c.execute("SELECT COUNT(*) FROM revenue_commercial_cases WHERE application_cycle_id=?", (cycle.id,)).fetchone()[0])

    def test_authenticated_student_creation_retry_repairs_missing_case(self):
        record_application_lifecycle_event(self.c, "STUDENT_CREATED", occurred_at=NOW,
            actor_user_id=2, student_id="S-REVENUE")
        self.assertEqual(1, self.c.execute("SELECT COUNT(*) FROM revenue_commercial_cases WHERE application_cycle_id=100").fetchone()[0])

    def test_missing_issuer_is_not_replaced_by_an_arbitrary_admin(self):
        self.c.execute("UPDATE parent_form_tokens SET created_by=NULL")
        self.submit()
        self.assertEqual(0, self.c.execute("SELECT COUNT(*) FROM revenue_commercial_cases").fetchone()[0])

    def test_nonexistent_issuer_is_not_used(self):
        self.c.execute("UPDATE parent_form_tokens SET created_by=999")
        self.submit()
        self.assertEqual(0, self.c.execute("SELECT COUNT(*) FROM revenue_commercial_cases").fetchone()[0])

    def test_parent_link_bound_to_another_student_is_rejected(self):
        self.c.execute("UPDATE parent_form_tokens SET student_id='OTHER'")
        with self.assertRaises(CycleConflictError):
            self.submit()
        self.assertEqual(0, self.c.execute("SELECT COUNT(*) FROM revenue_commercial_cases").fetchone()[0])

    def test_invalidated_parent_link_does_not_open_case(self):
        self.c.execute("UPDATE parent_form_tokens SET invalidated_at=?", (NOW,))
        self.submit()
        self.assertEqual(0, self.c.execute("SELECT COUNT(*) FROM revenue_commercial_cases").fetchone()[0])

    def test_historical_parent_cycle_is_not_backfilled_by_a_retry(self):
        self.c.execute("UPDATE application_cycles SET created_at='2026-08-01',updated_at=?", (NOW,))
        self.submit()
        self.assertEqual(0, self.c.execute("SELECT COUNT(*) FROM revenue_commercial_cases").fetchone()[0])

    def test_closed_cycle_is_not_opened_by_the_automatic_bridge(self):
        self.c.execute("UPDATE application_cycles SET status='closed'")
        result = ensure_revenue_case_for_cycle(self.c, 100, 2, occurred_at=NOW)
        self.assertIsNone(result)
        self.assertEqual(0, self.c.execute("SELECT COUNT(*) FROM revenue_commercial_cases").fetchone()[0])

    def test_failure_propagates_and_caller_rolls_back_the_new_cycle(self):
        self.c.execute("CREATE TRIGGER reject_case BEFORE INSERT ON revenue_commercial_cases BEGIN SELECT RAISE(ABORT,'case write rejected'); END")
        self.c.execute("INSERT INTO students VALUES('S-NEW')")
        self.c.execute("INSERT INTO parent_form_tokens VALUES('new-link','S-NEW',2,?,NULL)", (NOW,))
        self.c.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "case write rejected"):
            with self.c:
                record_application_lifecycle_event(self.c, "PARENT_FORM_SUBMITTED", occurred_at=NOW,
                    actor_user_id=None, student_id="S-NEW", parent_form_token="new-link")
        self.assertEqual(0, self.c.execute("SELECT COUNT(*) FROM application_cycles WHERE student_id='S-NEW'").fetchone()[0])
