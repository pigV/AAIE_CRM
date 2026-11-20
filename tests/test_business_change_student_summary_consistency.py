import unittest

from tests import test_business_change_school_chain as fixture
from student_detail_summary_cards import _load_school_plan_card_summary
from student_application_stage_distribution import load_application_stage_distribution,load_application_stage_result_map


class StudentSummaryConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.SchoolMaterialChainTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db;self.sid=self.f.sid;self.cycle=self.f.cycle

    def test_draft_contract_has_one_preparation_stage_in_student_and_dashboard(self):
        self.db.execute("UPDATE contracts SET contract_status='Draft',fully_executed_at=NULL,final_signed_file_path=NULL WHERE student_id=?",(self.sid,))
        with self.db.read_snapshot() as connection:
            student=load_application_stage_result_map(connection,[self.sid])[self.sid]
            members=[row for row in load_application_stage_distribution(connection,cycles_only=True) if row.cycle_id==self.cycle]
        self.assertEqual('contract_preparation',student.key)
        self.assertEqual(1,len(members))
        self.assertEqual('contract_preparation',members[0].stage.key)

    def test_native_private_submission_is_counted_after_final_school_selection(self):
        self.f.test_plan_application_offer_payment_coe_and_evidence_withdrawal_use_current_cycle()
        with self.db.read_snapshot() as connection:summary=_load_school_plan_card_summary(connection,self.sid)
        self.assertTrue(summary.is_final)
        self.assertEqual(1,summary.submitted_count)

    def test_signed_contract_waiting_for_a_plan_keeps_one_visible_active_cycle(self):
        with self.db.read_snapshot() as connection:
            members=[row for row in load_application_stage_distribution(connection,cycles_only=True) if row.cycle_id==self.cycle]
        self.assertEqual(1,len(members))
        self.assertEqual('waiting_school_plan',members[0].stage.key)

    def test_retracted_canonical_submission_overrides_stale_legacy_submitted_flag(self):
        from workflow_application_service import retract_private_application_submission
        self.f.test_plan_application_offer_payment_coe_and_evidence_withdrawal_use_current_cycle()
        application=self.db.one('SELECT id,confirmed_school_id FROM private_school_applications WHERE application_cycle_id=?',(self.cycle,))
        self.db.execute("UPDATE school_plan_confirmed_schools SET submission_status='submitted',submitted_at=? WHERE id=?",(self.f.stamp,application['confirmed_school_id']))
        with self.db.transaction() as connection:retract_private_application_submission(connection,application['id'],occurred_at=self.f.stamp)
        with self.db.read_snapshot() as connection:summary=_load_school_plan_card_summary(connection,self.sid)
        self.assertEqual(0,summary.submitted_count)
