"""Public DTO activity agrees with the confirmed lifecycle slot contract."""
import unittest
from student_case_service import _case_dto

def row(status,slot):
 fields=['created_by','created_at','updated_by','updated_at','started_at','paused_at','completed_at','cancelled_at','failed_at','archived_at','terminal_result']
 return {**dict.fromkeys(fields,None),'id':1,'student_id':'synthetic','case_type':'transfer','sequence_no':1,'status':status,'active_slot':slot}
class StudentCaseActivityContractTests(unittest.TestCase):
 def test_draft_never_reports_active_even_for_historical_slot(self):
  for slot in (0,1):
   with self.subTest(slot=slot):self.assertFalse(_case_dto(row('draft',slot)).is_active)
 def test_formal_activity_requires_both_status_and_slot(self):
  for status in ('preparing','in_progress','waiting','paused'):
   with self.subTest(status=status):
    self.assertTrue(_case_dto(row(status,1)).is_active)
    self.assertFalse(_case_dto(row(status,0)).is_active)
 def test_terminal_record_is_not_active_even_with_inconsistent_slot(self):
  for status in ('completed','cancelled','failed','archived'):
   with self.subTest(status=status):self.assertFalse(_case_dto(row(status,1)).is_active)
