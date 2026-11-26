"""Parent choice must create one draft-backed receivable, with no payment."""
import json
import unittest

from tests import test_transfer_intake_financial as support
from tests.test_transfer_intake_handoff import NOW


class ParentGuardianIntakeFeeTests(unittest.TestCase):
    def setUp(self):
        fixture = support.TransferIntakeFinancialTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.c = fixture.c
        self.c.execute("ALTER TABLE parent_form_tokens ADD COLUMN created_by INTEGER")
        self.c.executescript("""
            CREATE TABLE parents(
              student_id TEXT, parent_type TEXT, passport_first_name TEXT, passport_last_name TEXT
            );
            INSERT INTO parents VALUES('S2','father','Parent','Father');
            INSERT INTO parents VALUES('S2','mother','Parent','Mother');
            ALTER TABLE contracts ADD COLUMN contract_date TEXT;
            CREATE TABLE visa_cases(
              id INTEGER PRIMARY KEY, student_id TEXT, application_cycle_id INTEGER,
              archived_at TEXT, visa_type TEXT, visa_subclass TEXT, visa_case_name TEXT,
              main_applicant TEXT, applicant_name TEXT, relationship_to_student TEXT,
              applicant_count INTEGER, decision_status TEXT, case_type TEXT, application_type TEXT,
              status TEXT, result_status TEXT, created_by INTEGER, created_at TEXT, updated_at TEXT
            );
        """)
        self.c.execute("UPDATE parent_form_tokens SET created_by=1 WHERE token='S2-transfer-intake'")
        self.form = dict(fixture.f.form, requires_parent_accompaniment='是',
                         accompanying_person_relationship='父亲',
                         father_passport_first_name='Parent', father_passport_last_name='Father')
        self.fixture = fixture

    def submit(self, transfer=False, **changes):
        from revenue.parent_guardian_intake import record_parent_guardian_intake

        data = dict(self.form, **changes)
        with self.c:
            token = 'S2-transfer-intake' if transfer else 'ordinary-intake'
            if not transfer:
                self.c.execute("""
                    INSERT OR IGNORE INTO parent_form_tokens(
                      token, student_id, intake_type, submitted_at, created_at, created_by
                    ) VALUES (?, 'S2', 'ordinary_new_application', ?, ?, 1)
                """, (token, NOW, NOW))
                self.c.execute("UPDATE application_cycles SET parent_form_token=? WHERE id=991", (token,))
            occurred_at = self.c.execute(
                "SELECT submitted_at FROM parent_form_tokens WHERE token=?", (token,),
            ).fetchone()[0]
            self.c.execute("UPDATE students SET form_data_json=? WHERE student_id='S2'", (json.dumps(data),))
            return record_parent_guardian_intake(self.c, 'S2', token, occurred_at=occurred_at)

    def fee_rows(self):
        return [tuple(row) for row in self.c.execute("""
            SELECT milestone_code, net_cents FROM revenue_fee_items WHERE student_id='S2'
            ORDER BY net_cents
        """)]

    def test_actual_parent_replay_creates_one_draft_and_receivable(self):
        from revenue.parent_guardian_intake import automatic_guardian_draft

        self.submit()
        self.submit()
        self.assertEqual([('guardian-590', 90000)], self.fee_rows())
        rows = self.c.execute("SELECT applicant_name,status,application_cycle_id FROM visa_cases WHERE student_id='S2'").fetchall()
        self.assertEqual([('Parent Father', 'Draft', 991)], [tuple(row) for row in rows])
        self.assertEqual(0, self.c.execute("SELECT count(*) FROM revenue_payments").fetchone()[0])
        case_id = self.c.execute("SELECT id FROM visa_cases WHERE student_id='S2'").fetchone()[0]
        self.assertEqual(case_id, automatic_guardian_draft(self.c, 'S2', 991, 'Father Parent'))
        self.c.execute("UPDATE visa_cases SET status='Lodged' WHERE id=?", (case_id,))
        self.assertIsNone(automatic_guardian_draft(self.c, 'S2', 991, 'Father Parent'))

    def test_unknown_or_mismatched_parent_does_not_create_fee(self):
        self.submit(accompanying_person_relationship='其他')
        self.submit(father_passport_last_name='Mismatch')
        self.submit(requires_parent_accompaniment='否')
        self.assertEqual([], self.fee_rows())
        self.assertFalse(self.c.execute("SELECT 1 FROM visa_cases WHERE student_id='S2'").fetchone())

    def test_transfer_intake_fee_waits_for_transfer_cycle_without_blocking_handoff(self):
        self.submit(transfer=True)
        self.assertEqual([], self.fee_rows())
        result = self.fixture.attempt()
        self.assertEqual('completed', result['status'], result)
        self.assertEqual(result['cycle_id'], self.fixture.attempt()['cycle_id'])
        self.assertEqual([90000, 240000], [amount for _, amount in self.fee_rows()])
        self.assertEqual(result['cycle_id'], self.c.execute("SELECT application_cycle_id FROM visa_cases WHERE student_id='S2'").fetchone()[0])

    def test_optional_visa_failure_does_not_rollback_signed_transfer_fee(self):
        from unittest.mock import patch

        self.submit(transfer=True)
        with patch('revenue.parent_guardian_intake.process_revenue_visa_change',
                   side_effect=ValueError('fixture policy conflict')):
            result = self.fixture.attempt()
        self.assertEqual('completed', result['status'], result)
        self.assertEqual([240000], [amount for _, amount in self.fee_rows()])
        self.assertFalse(self.c.execute("SELECT 1 FROM visa_cases WHERE student_id='S2'").fetchone())
        self.assertEqual(result['cycle_id'], self.fixture.attempt()['cycle_id'])
        self.assertEqual([90000, 240000], [amount for _, amount in self.fee_rows()])
