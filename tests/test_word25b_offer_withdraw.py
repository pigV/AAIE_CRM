import unittest
from tests import test_offer_document_acceptance as fixture
import offer_acceptance as service


class UnifiedOfferWithdrawalTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.DocumentWithdrawalTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.c=self.f.connection
        service.assign_offer_school(self.c,101,11,self.f.actor)
        service.set_accepted_offer(self.c,'S001',101,self.f.actor)

    def withdraw(self,ack=True):
        action=getattr(service,'withdraw_offer_acceptance_and_selection',None)
        self.assertTrue(callable(action),'A reasoned unified withdrawal action is required')
        return action(self.c,'S001',101,self.f.actor,reason='Family changed plans',acknowledge_final=ack)

    def test_current_final_document_acceptance_can_be_withdrawn_without_deleting_originals(self):
        before=self.c.execute('SELECT count(*) FROM offers').fetchone()[0]
        history=[tuple(r) for r in self.c.execute('SELECT * FROM offer_acceptance_history ORDER BY id')]
        self.withdraw()
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM student_offer_acceptances').fetchone()[0])
        self.assertEqual('received',self.c.execute('SELECT offer_status FROM offers WHERE id=101').fetchone()[0])
        self.assertEqual(before,self.c.execute('SELECT count(*) FROM offers').fetchone()[0])
        self.assertEqual(history,[tuple(r) for r in self.c.execute('SELECT * FROM offer_acceptance_history ORDER BY id')][:len(history)])
        count=self.c.execute('SELECT count(*) FROM offer_acceptance_history').fetchone()[0]
        self.withdraw()
        self.assertEqual(count,self.c.execute('SELECT count(*) FROM offer_acceptance_history').fetchone()[0])

    def test_current_selection_requires_explicit_acknowledgment_and_rolls_back(self):
        with self.assertRaises(ValueError):self.withdraw(False)
        self.assertEqual(101,self.c.execute('SELECT offer_id FROM student_offer_acceptances').fetchone()[0])
        self.assertEqual('accepted',self.c.execute('SELECT offer_status FROM offers WHERE id=101').fetchone()[0])
