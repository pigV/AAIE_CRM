import unittest
from tests import test_business_change_school_chain as chain

class CoEEnrolmentControlsTests(unittest.TestCase):
    def test_canonical_only_enrolment_can_be_reviewed_and_withdrawn_without_rewriting_offer(self):
        f=chain.SchoolMaterialChainTests();f.setUp();self.addCleanup(f.doCleanups)
        f.test_plan_application_offer_payment_coe_and_evidence_withdrawal_use_current_cycle()
        a=f.db.one("SELECT * FROM cycle_final_acceptances WHERE application_cycle_id=? AND acceptance_status='active'",(f.cycle,))
        before=[tuple(r) for r in f.db.all('SELECT id,offer_status FROM offers WHERE student_id=?',(f.sid,))]
        html=f.h.coe_enrolment_controls(f.sid)
        self.assertIn('撤回这条入读依据',html)
        f.h.form={'reason':'SYNTHETIC incorrect school selection'}
        f.h.json_response=lambda payload,status=200:(status,payload)
        status,payload=f.h.student_coe_enrolment_withdraw('POST',f.sid,str(a['id']))
        self.assertEqual(200,status,payload);self.assertTrue(payload['ok'])
        self.assertEqual('withdrawn',f.db.one('SELECT acceptance_status FROM cycle_final_acceptances WHERE id=?',(a['id'],))[0])
        self.assertEqual(before,[tuple(r) for r in f.db.all('SELECT id,offer_status FROM offers WHERE student_id=?',(f.sid,))])
