import unittest
from tests import test_business_change_school_chain as chain
from dashboard_confirmed_stages import load_dashboard_stages

class WordStageNativeTests(unittest.TestCase):
    def setUp(self):
        self.f=chain.SchoolMaterialChainTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.test_plan_application_offer_payment_coe_and_evidence_withdrawal_use_current_cycle()
    def test_reviewed_stage_is_shared_by_dashboard_and_student_profile_then_expires(self):
        from reviewed_application_stage import confirm_reviewed_stage
        from student_workflow_projection import load_student_workflow_projection_map
        f=self.f
        with f.db.transaction() as c:
            confirm_reviewed_stage(c,student_id=f.sid,cycle_id=f.cycle,stage_key='07_school_response',actor_id=f.actor['id'],reason='Reviewed Word example',stamp=f.stamp)
        with f.db.read_snapshot() as c:
            self.assertEqual('07_school_response',load_dashboard_stages(c,[f.cycle])[f.cycle][0])
            self.assertEqual('07_school_response',load_student_workflow_projection_map(c,[f.sid])[f.sid].stage_key)
        with f.db.transaction() as c:
            c.execute("UPDATE offers SET offer_status='cancelled' WHERE student_id=?",(f.sid,))
        with f.db.read_snapshot() as c:
            from reviewed_application_stage import load_reviewed_stages
            self.assertEqual({},load_reviewed_stages(c,[f.cycle]))
    def test_review_after_old_school_withdrawal_can_confirm_current_tertiary_stage(self):
        from reviewed_application_stage import confirm_reviewed_stage
        f=self.f
        with f.db.transaction() as c:
            c.execute("UPDATE cycle_final_acceptances SET acceptance_status='withdrawn',ended_at=?,ended_by_user_id=? WHERE application_cycle_id=?",(f.stamp,f.actor['id'],f.cycle))
            confirm_reviewed_stage(c,student_id=f.sid,cycle_id=f.cycle,stage_key='10_visa_submission',actor_id=f.actor['id'],reason='Old school withdrawn; current TAFE reviewed',stamp=f.stamp)
        with f.db.read_snapshot() as c:
            self.assertEqual('10_visa_submission',load_dashboard_stages(c,[f.cycle])[f.cycle][0])

    def test_higher_education_exact_cycle_offer_counts_without_school_city_classification(self):
        f=self.f
        from application_cycle import create_student_cycle
        with f.db.transaction() as c:
            cycle=create_student_cycle(c,f.sid,'initial',f.stamp,f.actor['id'],education_stream='higher_education')
            c.execute("INSERT INTO offers(student_id,school_name,offer_status,document_type,offer_file_path,offer_expiry_date,application_cycle_id,classification_status,created_at) VALUES(?,?,'received','Offer',?,'2099-01-01',?,'unresolved',?)",(f.sid,'Test University',str(f.root/'university.pdf'),cycle.id,f.stamp))
        with f.db.read_snapshot() as c:
            self.assertEqual('08_offer_acceptance',load_dashboard_stages(c,[cycle.id])[cycle.id][0])
