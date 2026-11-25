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
        self.assertEqual('合同准备中，待发起签署',student.label)
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

    def test_inactive_contract_link_is_counted_once_and_matches_student_label(self):
        self.db.execute("UPDATE contracts SET contract_status='Sent',fully_executed_at=NULL,final_signed_file_path=NULL,signed_at=NULL WHERE student_id=?",(self.sid,))
        with self.db.read_snapshot() as connection:
            student=load_application_stage_result_map(connection,[self.sid])[self.sid]
            members=[row for row in load_application_stage_distribution(connection,cycles_only=True) if row.cycle_id==self.cycle]
        self.assertEqual('contract_link_inactive',student.key)
        self.assertEqual('链接失效，待重发',student.label)
        self.assertEqual(1,len(members))
        self.assertEqual('contract_link_inactive',members[0].stage.key)
        self.assertEqual('链接失效，待重发',members[0].stage.label)

    def test_expired_revoked_and_replaced_links_keep_one_current_cycle(self):
        self.db.execute("UPDATE contracts SET contract_status='Sent',fully_executed_at=NULL,final_signed_file_path=NULL,signed_at=NULL WHERE student_id=?",(self.sid,))
        cid=self.db.one('SELECT id FROM contracts WHERE student_id=?',(self.sid,))[0]
        self.db.execute("""INSERT INTO contract_signing_links(contract_id,student_id,token_hash,token_last_4,signing_url,expires_at,created_at)
            VALUES(?,?,'synthetic-expired','test','http://synthetic.invalid','2000-01-01',?)""",(cid,self.sid,self.f.stamp))
        for status,expiry,invalidated,expected in (
            ('active','2000-01-01',None,'contract_link_inactive'),
            ('revoked','2099-01-01',self.f.stamp,'contract_link_inactive'),
            ('active','2099-01-01',None,'03_contract_signature'),
        ):
            with self.subTest(status=status,expiry=expiry):
                self.db.execute('UPDATE contract_signing_links SET status=?,expires_at=?,invalidated_at=? WHERE contract_id=?',(status,expiry,invalidated,cid))
                with self.db.read_snapshot() as connection:
                    student=load_application_stage_result_map(connection,[self.sid])[self.sid]
                    members=[r for r in load_application_stage_distribution(connection,cycles_only=True) if r.cycle_id==self.cycle]
                self.assertEqual(expected,student.key)
                self.assertEqual([expected],[r.stage.key for r in members])
        self.db.execute("UPDATE contract_signing_links SET expires_at='2000-01-01' WHERE contract_id=?",(cid,))
        self.db.execute("UPDATE contracts SET contract_status='ParentSigned',signed_at=? WHERE id=?",(self.f.stamp,cid))
        with self.db.read_snapshot() as connection:
            members=[r for r in load_application_stage_distribution(connection,cycles_only=True) if r.cycle_id==self.cycle]
        self.assertEqual(['waiting_school_plan'],[r.stage.key for r in members])
