"""Evidence checks for the standalone local lab, without importing CRM."""
import unittest
from local_document_lab.evidence import ground_claim, validate_result

BLOCKS = [{'id':'p1-b1','text':'English Language Centre: IEC Marrickville', 'page':1,
           'bbox':[10,20,200,40]},
          {'id':'p1-b2','text':'Course Start Date: 03-Feb-2027', 'page':1, 'bbox':[10,50,200,70]}]


class EvidenceTests(unittest.TestCase):
    def claim(self, value='IEC Marrickville', quote='English Language Centre: IEC Marrickville', block='p1-b1'):
        return {'value':value,'block_id':block,'quote':quote}

    def test_grounded_school_has_actual_source_page_and_box(self):
        result=ground_claim('language_school',self.claim(),BLOCKS)
        self.assertEqual(result['value'],'IEC Marrickville')
        self.assertEqual(result['page'],1)
        self.assertEqual(result['bbox'],[10,20,200,40])
        self.assertEqual(result['status'],'supported_needs_review')

    def test_fabricated_quote_is_rejected(self):
        self.assertIsNone(ground_claim('language_school',self.claim(quote='School: Invented College'),BLOCKS)['value'])

    def test_value_not_present_in_evidence_is_rejected(self):
        self.assertIsNone(ground_claim('language_school',self.claim(value='Other College'),BLOCKS)['value'])

    def test_unknown_source_block_is_rejected(self):
        self.assertIsNone(ground_claim('language_school',self.claim(block='p9-b8'),BLOCKS)['value'])

    def test_date_normalization_requires_matching_source_date(self):
        claim={'value':'2027-02-03','quote':'Course Start Date: 03-Feb-2027','block_id':'p1-b2'}
        self.assertEqual(ground_claim('course_start_date',claim,BLOCKS)['value'],'2027-02-03')
        claim['value']='2027-03-02'
        self.assertIsNone(ground_claim('course_start_date',claim,BLOCKS)['value'])

    def test_empty_field_abstains_and_malformed_response_is_not_accepted(self):
        self.assertIsNone(ground_claim('language_school',None,BLOCKS)['value'])
        with self.assertRaises(ValueError):validate_result({'fields':[]},BLOCKS)

    def test_reversed_dates_are_flagged(self):
        blocks=[{'id':'dates','text':'Start 21/07/2026 End 18/12/2028','page':1,'bbox':[]}]
        claim=lambda value:{'value':value,'quote':blocks[0]['text'],'block_id':'dates'}
        result=validate_result({'fields':{'course_start_date':claim('2028-12-18'),
                    'course_end_date':claim('2026-07-21')},'programs':[]},blocks)
        self.assertIn('course_dates_reversed',result['issues'])
        self.assertIsNone(result['fields']['course_start_date']['value'])

if __name__=='__main__':unittest.main()
