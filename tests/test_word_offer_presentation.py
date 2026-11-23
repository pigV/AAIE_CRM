import inspect,unittest
import app
from tests import test_business_change_school_chain as chain

class OfferPresentationTests(unittest.TestCase):
    def test_offer_cards_keep_document_acceptance_without_final_school_controls(self):
        f=chain.SchoolMaterialChainTests();f.setUp();self.addCleanup(f.doCleanups)
        f.test_plan_application_offer_payment_coe_and_evidence_withdrawal_use_current_cycle()
        offers=f.db.all('SELECT * FROM offers WHERE student_id=?',(f.sid,))
        f.h._student_lifecycle_record_contexts={}
        html=f.h.offer_panel(f.sid,offers)
        self.assertNotIn('/select-final',html)
        self.assertNotIn('设为最终入读学校',html)
        self.assertNotIn('撤回最终入读选择',html)
        self.assertIn('Offer',html)

    def test_offer_panel_reuses_an_active_request_snapshot(self):
        f=chain.SchoolMaterialChainTests();f.setUp();self.addCleanup(f.doCleanups)
        f.test_plan_application_offer_payment_coe_and_evidence_withdrawal_use_current_cycle()
        with f.db.request_scope():
            with f.db.read_snapshot():
                offers=f.db.all('SELECT * FROM offers WHERE student_id=?',(f.sid,))
                html=f.h.offer_panel(f.sid,offers)
                self.assertIn('CRM 学校匹配',html)
