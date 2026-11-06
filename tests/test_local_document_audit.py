import unittest
from local_document_lab.evidence import validate_result
from local_document_lab.audit import audit_result


class AuditTests(unittest.TestCase):
    def test_grounded_course_date_is_flagged_when_used_as_welfare_date(self):
        blocks=[{'id':'course','text':'Course Start Date: 13/07/2026','page':1,'bbox':[0,0,1,1]}]
        result=validate_result({'fields':{'welfare_start_date':{'value':'2026-07-13','quote':blocks[0]['text'],'block_id':'course'}},'programs':[]},blocks)
        issues=audit_result('caaw',result,blocks)['issues']
        self.assertIn('welfare_start_date_uses_course_evidence',issues)

    def test_same_institution_for_language_and_main_is_allowed(self):
        claim={'value':'Example College','block_id':'b','quote':'Example College','page':1,'bbox':[0,0,1,1],'status':'supported_needs_review'}
        blocks=[{'id':'b','text':'Example College','page':1,'bbox':[0,0,1,1]}]
        result={'fields':{'language_school':claim,'main_school':claim},'programs':[
            {'kind':'language','fields':{'school':claim},'issues':[]},
            {'kind':'main','fields':{'school':claim},'issues':[]}],'issues':[]}
        self.assertNotIn('language_main_school_conflict',audit_result('confirmation',result,blocks)['issues'])

    def test_main_summary_must_match_a_main_program_when_programs_exist(self):
        a={'value':'A College','block_id':'a','quote':'A College','page':1,'bbox':[0,0,1,1],'status':'supported_needs_review'}
        b={**a,'value':'B College','block_id':'b','quote':'B College'}
        blocks=[{'id':'a','text':'A College','page':1,'bbox':[0,0,1,1]},{'id':'b','text':'B College','page':1,'bbox':[0,0,1,1]}]
        result={'fields':{'main_school':a},'programs':[{'kind':'main','fields':{'school':b},'issues':[]}],'issues':[]}
        self.assertIn('main_school_not_in_main_programs',audit_result('confirmation',result,blocks)['issues'])

    def test_missing_coe_fields_are_reported_as_incomplete(self):
        audit=audit_result('coe',{'fields':{},'programs':[],'issues':[]},[])
        self.assertEqual(set(audit['missing_fields']),{'provider','course','course_start_date','course_end_date'})
        self.assertFalse(audit['candidate_complete'])

    def test_caaw_does_not_require_a_host_school_in_optional_course_program(self):
        blocks=[{'id':'w','text':'Example Department Welfare 06/07/2026 31/12/2027','page':1,'bbox':[0,0,1,1]}]
        def claim(value):return {'value':value,'block_id':'w','quote':blocks[0]['text']}
        result=validate_result({'fields':{'provider':claim('Example Department'),
            'welfare_start_date':claim('2026-07-06'),'welfare_end_date':claim('2027-12-31')},
            'programs':[{'kind':'main'}]},blocks)
        self.assertTrue(audit_result('caaw',result,blocks)['candidate_complete'])

    def test_saved_supported_status_does_not_replace_actual_evidence(self):
        result={'fields':{'provider':{'value':'Fake College','block_id':'b','quote':'Fake College','status':'supported_needs_review'}},'programs':[],'issues':[]}
        audit=audit_result('coe',result,[{'id':'b','text':'Real College','page':1,'bbox':[0,0,1,1]}])
        self.assertIn('invalid_evidence:provider',audit['issues'])

if __name__=='__main__':unittest.main()
