import unittest
from student_lifecycle.transfer_v2_projection import _result


class TransferAcceptedNextActionTests(unittest.TestCase):
    def test_paid_offer_before_acceptance_directs_staff_to_select_school(self):
        row = dict(cycle_id=1,cycle_uid="test",student_id="S",case_id=2,cycle_status="active",cycle_outcome=None)
        pending = _result(row, stage_key="accepted_and_paid", blocker="选择当前接受学校", payment_id=3)
        self.assertIsNone(pending.current_acceptance_id)
        self.assertEqual("选择当前接受学校", pending.next_action)
        accepted = _result(row, stage_key="coe_update", blocker="等待COE")
        self.assertNotEqual(pending.next_action, accepted.next_action)
