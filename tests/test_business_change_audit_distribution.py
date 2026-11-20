from types import SimpleNamespace
import unittest
from tools.business_change_audit import distribution_preview


def row(cycle,student,key,label,kind='student'):
    return SimpleNamespace(cycle_id=cycle,student_id=student,student_name=student or '未登记工作流',
        entity_kind=kind,stage=SimpleNamespace(key=key,label=label))


class DistributionAuditTests(unittest.TestCase):
    def test_cycle_counts_and_member_lists_share_the_same_units(self):
        rows=(row(7,'S1','11_visa_result','等待签证结果'),row(8,'S1','11_visa_result','等待签证结果'),
              row(9,None,'01_parent_form','等待家长填写表单','workflow_case'),row(10,'S2',None,''))
        result=distribution_preview(None,loader=lambda *args,**kwargs:rows)
        self.assertEqual(3,result['total'])
        self.assertEqual(2,result['counts']['11_visa_result'])
        self.assertEqual([7,8],[member['cycle_id'] for member in result['members']['11_visa_result']])
        self.assertEqual('workflow_case',result['members']['01_parent_form'][0]['entity_kind'])
        for key,count in result['counts'].items():self.assertEqual(count,len(result['members'][key]))

    def test_duplicate_cycle_cannot_be_reported_as_two_students(self):
        rows=(row(7,'S1','11_visa_result','等待签证结果'),row(7,'S1','10_visa_submission','等待递交 Visa'))
        with self.assertRaises(ValueError):distribution_preview(None,loader=lambda *args,**kwargs:rows)
