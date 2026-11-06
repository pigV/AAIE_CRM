import unittest
from local_document_lab.evidence import validate_result
from local_document_lab.project import project_program_fields

BLOCKS=[{'id':'lang','text':'IEC Marrickville 03-Feb-2027 02-Jul-2027','page':1,'bbox':[0,0,1,1]},
        {'id':'main','text':'Dulwich High School of Visual Arts and Design','page':1,'bbox':[1,1,2,2]},
        {'id':'other','text':'Other College','page':1,'bbox':[2,2,3,3]}]


def claim(value,block):
    return {'value':value,'block_id':block,'quote':next(b['text'] for b in BLOCKS if b['id']==block)}


class ProjectionTests(unittest.TestCase):
    def program(self):
        return {'kind':'language','school':claim('IEC Marrickville','lang'),
                'course_start_date':claim('2027-02-03','lang'),'course_end_date':claim('2027-07-02','lang')}

    def test_unique_grounded_language_program_populates_missing_summary(self):
        result=validate_result({'fields':{},'programs':[self.program(),
            {'kind':'main','school':claim('Dulwich High School of Visual Arts and Design','main')}]},BLOCKS)
        projected=project_program_fields(result)
        self.assertEqual(projected['fields']['language_school']['value'],'IEC Marrickville')
        self.assertEqual(projected['fields']['language_start_date']['value'],'2027-02-03')
        self.assertEqual(projected['fields']['language_end_date']['value'],'2027-07-02')
        self.assertEqual(projected['fields']['main_school']['value'],'Dulwich High School of Visual Arts and Design')
        self.assertEqual(projected['fields']['language_start_date']['block_id'],'lang')

    def test_multiple_language_programs_are_not_silently_collapsed(self):
        result=validate_result({'fields':{},'programs':[self.program(),self.program()]},BLOCKS)
        projected=project_program_fields(result)
        self.assertIn('multiple_language_programs',projected['issues'])
        self.assertFalse(projected['fields'].get('language_start_date',{}).get('value'))

    def test_conflicting_summary_and_program_are_flagged(self):
        result=validate_result({'fields':{'language_school':claim('Other College','other')},'programs':[self.program()]},BLOCKS)
        projected=project_program_fields(result)
        self.assertIsNone(projected['fields']['language_school']['value'])
        self.assertIn('language_school_conflict',projected['issues'])

    def test_language_summary_with_only_main_programs_is_flagged(self):
        program=self.program();program['kind']='main'
        result=validate_result({'fields':{'language_school':claim('IEC Marrickville','lang')},'programs':[program]},BLOCKS)
        self.assertIn('language_school_without_language_program',project_program_fields(result)['issues'])

if __name__=='__main__':unittest.main()
