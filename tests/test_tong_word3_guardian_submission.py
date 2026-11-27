"""Parent submission creates the identified guardian receivable, never a payment."""
import unittest
from tests import test_parent_guardian_intake_fee as support
from tests.test_transfer_intake_handoff import NOW


class GuardianSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = support.ParentGuardianIntakeFeeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.c = self.fixture.c

    def submit(self, **changes):
        self.fixture.submit(confirmed=False, **changes)
        from revenue.parent_guardian_intake import record_submitted_parent_guardian_intake
        return record_submitted_parent_guardian_intake(
            self.c, 'S2', 'ordinary-intake', occurred_at=NOW)

    def test_submitted_parent_choice_creates_named_900_receivable_once(self):
        self.submit()
        self.submit()
        self.assertEqual([('guardian-590', 90000)], self.fixture.fee_rows())
        self.assertEqual([('Parent Father', 'Draft', 991)], [tuple(r) for r in self.c.execute(
            "SELECT applicant_name,status,application_cycle_id FROM visa_cases WHERE student_id='S2'")])
        self.assertEqual(0, self.c.execute('SELECT count(*) FROM revenue_payments').fetchone()[0])

    def test_incomplete_or_conflicting_name_never_creates_receivable(self):
        self.submit(father_passport_last_name='')
        self.submit(father_passport_last_name='Mismatch')
        self.assertEqual([], self.fixture.fee_rows())

    def test_unsubmitted_revoked_or_wrong_student_form_cannot_create_fee(self):
        self.fixture.submit(confirmed=False)
        from revenue.parent_guardian_intake import record_submitted_parent_guardian_intake
        for field,value in [('submitted_at',None),('invalidated_at',NOW)]:
            with self.subTest(field=field):
                self.c.execute(f'UPDATE parent_form_tokens SET {field}=? WHERE token=?',(value,'ordinary-intake'))
                self.assertIsNone(record_submitted_parent_guardian_intake(self.c,'S2','ordinary-intake',occurred_at=NOW))
        self.assertIsNone(record_submitted_parent_guardian_intake(self.c,'S1','ordinary-intake',occurred_at=NOW))
        self.assertEqual([],self.fixture.fee_rows())
    def test_transfer_submission_defers_fee_until_formal_transfer_cycle(self):
        self.fixture.submit(transfer=True,confirmed=False)
        from revenue.parent_guardian_intake import record_submitted_parent_guardian_intake
        result=record_submitted_parent_guardian_intake(self.c,'S2','S2-transfer-intake',occurred_at=NOW)
        self.assertTrue(result['aggregate_uid'].startswith('cycle:'))
        self.assertEqual([],self.fixture.fee_rows())
        transfer=self.fixture.fixture.attempt()
        self.assertEqual('completed',transfer['status'])
        self.assertEqual([90000,240000],[amount for _,amount in self.fixture.fee_rows()])

    def test_optional_finance_error_does_not_undo_parent_submission(self):
        from unittest.mock import patch
        self.fixture.submit(confirmed=False)
        from revenue.parent_guardian_intake import record_submitted_parent_guardian_intake
        with patch('revenue.parent_guardian_intake._record_submission',side_effect=ValueError('policy unavailable')):
            self.assertIsNone(record_submitted_parent_guardian_intake(self.c,'S2','ordinary-intake',occurred_at=NOW))
        self.assertTrue(self.c.execute("SELECT submitted_at FROM parent_form_tokens WHERE token='ordinary-intake'").fetchone()[0])
        self.assertEqual([],self.fixture.fee_rows())


class GuardianSubmissionBoundaryTests(unittest.TestCase):
    def test_missing_finance_module_does_not_fail_submission(self):
        import sqlite3
        from revenue.parent_guardian_intake import record_submitted_parent_guardian_intake
        with sqlite3.connect(':memory:') as connection:
            self.assertIsNone(record_submitted_parent_guardian_intake(connection,'S2','unknown',occurred_at=NOW))
