"""Explicit table role/value associations, independent of model omissions."""
import unittest
from application_document_extraction import review_candidates


def table(rows, key='t', page=1):
    text='\n'.join('| '+' | '.join(row)+' |' for row in rows)
    return {'id':key,'kind':'table','text':text,'page':page,
            'bbox':{'l':0,'t':0,'r':500,'b':500,'coord_origin':'TOPLEFT'}}


def claim(value, source):
    return {'value':value,'block_id':source['id'],'quote':source['text']}


def offer(language='Not applicable'):
    return table([('School Name and Year Level','Course','Course Dates'),
                  (f'English Language Centre: {language} School: Sample College Address: Test Road Year Level: 10',
                   'Secondary (Year 7 - 12)','27 January 2027 to 21 December 2029')])


def confirmation(key='t', start='27 January, 2027', end='21 December, 2029', page=1):
    return table([('Enrolment Details','Enrolment Details'),
                  ('English language centre','Sample College'),
                  ('English language program start date','13 July, 2026'),
                  ('English language program end date','18 December, 2026'),
                  ('Host school','Sample College'),('Host school start date',start),('Host school end date',end)],key,page)


class TableRoleTests(unittest.TestCase):
    def test_explicit_not_applicable_cannot_be_a_language_school_or_program(self):
        source=offer()
        name=claim('English Language Centre',source)
        raw={'fields':{'main_school':claim('Sample College',source),'language_school':name},
             'programs':[{'kind':'language','school':name}]}
        result=review_candidates('offer',raw,[source])
        self.assertNotIn('language_school_name',result)
        self.assertEqual(result['school_name']['value'],'Sample College')

    def test_blank_language_row_cannot_borrow_host_school(self):
        source=table([('English language centre',''),('Host school','Sample College'),
                      ('Host school start date','27 January 2027'),('Host school end date','21 December 2029')])
        name=claim('Sample College',source)
        result=review_candidates('confirmation',{'fields':{'language_school':name},
                            'programs':[{'kind':'language','school':name}]},[source])
        self.assertNotIn('letter_language_course_name',result)
        self.assertEqual(result['letter_main_course_name']['value'],'Sample College')

    def test_same_school_separate_host_dates_recovered_when_model_omits_main(self):
        source=confirmation();name=claim('Sample College',source)
        raw={'fields':{'main_school':name,'language_school':name},'programs':[{
            'kind':'language','school':name,'course_start_date':claim('2026-07-13',source),
            'course_end_date':claim('2026-12-18',source)}]}
        result=review_candidates('confirmation',raw,[source])
        self.assertEqual(result['letter_main_start_date']['value'],'2027-01-27')
        self.assertEqual(result['letter_main_end_date']['value'],'2029-12-21')
        self.assertEqual(result['letter_language_start_date']['value'],'2026-07-13')
        self.assertIn('Host school start date',result['letter_main_start_date']['snippet'])

    def test_explicit_course_column_can_recover_correct_value_with_wrong_model_block(self):
        source=offer();raw={'fields':{'main_school':claim('Sample College',source),
            'course_start_date':{'value':'2027-01-27','block_id':'nonexistent-header','quote':'27 January 2027'}},'programs':[]}
        result=review_candidates('offer',raw,[source])
        self.assertEqual(result['course_start_date']['value'],'2027-01-27')
        self.assertEqual(result['course_start_date']['block_id'],source['id'])

    def test_payment_date_column_cannot_supply_course_start(self):
        source=table([('School Name','Payment Dates'),('School: Sample College','27 January 2027 to 21 December 2029')])
        self.assertNotIn('course_start_date',review_candidates('offer',{'fields':{},'programs':[]},[source]))

    def test_competing_host_intervals_are_not_chosen_by_order(self):
        one=confirmation();two=confirmation('other','28 January, 2027',page=2)
        result=review_candidates('confirmation',{'fields':{},'programs':[]},[one,two])
        self.assertNotIn('letter_main_start_date',result)

    def test_cross_page_label_and_value_are_not_joined(self):
        one=table([('Host school','Sample College'),('Host school start date','')])
        two=table([('Unlabelled value','27 January 2027'),('Host school end date','21 December 2029')],'other',2)
        result=review_candidates('confirmation',{'fields':{},'programs':[]},[one,two])
        self.assertNotIn('letter_main_start_date',result)

    def test_reversed_host_period_is_not_repaired_by_sorting(self):
        source=confirmation(start='27 January, 2030',end='21 December, 2029')
        result=review_candidates('confirmation',{'fields':{},'programs':[]},[source])
        self.assertNotIn('letter_main_start_date',result)

    def test_role_heading_without_an_institution_is_not_a_school_name(self):
        source=offer('English Language Centre')
        raw={'fields':{'language_school':claim('English Language Centre',source)},'programs':[]}
        self.assertNotIn('language_school_name',review_candidates('offer',raw,[source]))

class CompetingRoleTests(unittest.TestCase):
    def test_repeated_host_start_clears_model_language_dates_in_main_program(self):
        source=confirmation()
        source['text']+='\n| Host school start date | 28 January, 2027 |'
        name=claim('Sample College',source)
        raw={'fields':{'main_school':name},'programs':[{'kind':'main','school':name,
            'course_start_date':claim('2026-07-13',source),'course_end_date':claim('2026-12-18',source)}]}
        result=review_candidates('confirmation',raw,[source])
        self.assertNotIn('letter_main_start_date',result)
        self.assertNotIn('letter_main_end_date',result)

    def test_multiple_model_main_programs_cannot_merge_language_and_host_dates(self):
        source=confirmation();name=claim('Sample College',source)
        raw={'fields':{'main_school':name},'programs':[
            {'kind':'main','school':name,'course_start_date':claim('2026-07-13',source),'course_end_date':claim('2026-12-18',source)},
            {'kind':'main','school':name,'course_start_date':claim('2027-01-27',source),'course_end_date':claim('2029-12-21',source)}]}
        result=review_candidates('confirmation',raw,[source])
        self.assertNotIn('letter_main_start_date',result)

    def test_course_column_explicit_language_period_does_not_authorize_main_date(self):
        source=offer('Language Academy')
        source['text']=source['text'].replace('Secondary (Year 7 - 12)','Intensive English Language Program')
        result=review_candidates('offer',{'fields':{},'programs':[]},[source])
        self.assertNotIn('course_start_date',result)

    def test_unknown_course_role_does_not_authorize_main_date(self):
        source=offer();source['text']=source['text'].replace('Secondary (Year 7 - 12)','Unspecified program')
        self.assertNotIn('course_start_date',review_candidates('offer',{'fields':{},'programs':[]},[source]))

    def test_reversed_offer_interval_cannot_be_fixed_by_sorting(self):
        source=offer();source['text']=source['text'].replace('27 January 2027 to 21 December 2029','21 December 2029 to 27 January 2027')
        self.assertNotIn('course_start_date',review_candidates('offer',{'fields':{},'programs':[]},[source]))

class IncompleteSchoolClaimTests(unittest.TestCase):
    def test_empty_model_school_claim_uses_explicit_host_interval(self):
        source=confirmation()
        raw={'fields':{},'programs':[{'kind':'main','school':{'value':None,'block_id':None,'quote':None},
             'course_start_date':claim('2026-07-13',source),'course_end_date':claim('2026-12-18',source)}]}
        result=review_candidates('confirmation',raw,[source])
        self.assertEqual(result['letter_main_start_date']['value'],'2027-01-27')
        self.assertEqual(result['letter_main_end_date']['value'],'2029-12-21')

    def test_another_valid_school_cannot_supply_host_dates(self):
        source=confirmation();other=table([('School','Other College'),('Dates','27 January 2028 to 21 December 2028')],'other',2)
        raw={'fields':{},'programs':[{'kind':'main','school':claim('Other College',other),
             'course_start_date':claim('2028-01-27',other),'course_end_date':claim('2028-12-21',other)}]}
        result=review_candidates('confirmation',raw,[source,other])
        self.assertNotIn('letter_main_start_date',result)
        self.assertNotIn('letter_main_end_date',result)

def vertical_programs():
    rows=[('High School Graduate Program','High School Graduate Program','Cost')]
    for level,course,start,end in [('ISEC','Secondary Years 07-10 (Graduate-ISEC)','20 July 2026','11 December 2026'),
                                 ('Year 9 - Year 10','Secondary Years 07-10 (Graduate)','27 January 2027','15 December 2028'),
                                 ('Year 11 - Year 12','Secondary Years 11-12 (Graduate)','29 January 2029','13 December 2030')]:
        rows += [('Year Level:',level,''),('School:','Sample International High School',''),
                 ('Start Date:',start,''),('End Date:',end,''),('Course Name:',course,'')]
    return table(rows)

class VerticalProgramTests(unittest.TestCase):
    def test_vertical_school_value_is_not_an_empty_inline_school_field(self):
        source=vertical_programs();name=claim('Sample International High School',source)
        result=review_candidates('offer',{'fields':{'main_school':name},'programs':[]},[source])
        self.assertEqual(result['school_name']['value'],'Sample International High School')
        self.assertEqual(result['language_school_name']['value'],'Sample International High School')

    def test_explicit_isec_and_two_main_stages_keep_distinct_dates(self):
        source=vertical_programs()
        result=review_candidates('confirmation',{'fields':{},'programs':[]},[source])
        self.assertEqual(result['letter_language_start_date']['value'],'2026-07-20')
        self.assertEqual(result['letter_language_end_date']['value'],'2026-12-11')
        self.assertEqual(result['letter_main_start_date']['value'],'2027-01-27')
        self.assertEqual(result['letter_main_end_date']['value'],'2030-12-13')

    def test_source_programs_do_not_override_another_valid_school_program(self):
        source=vertical_programs();other=table([('School','Other College'),('Dates','27 January 2028 to 21 December 2028')],'other',2)
        raw={'fields':{},'programs':[{'kind':'main','school':claim('Other College',other),
             'course_start_date':claim('2028-01-27',other),'course_end_date':claim('2028-12-21',other)}]}
        result=review_candidates('confirmation',raw,[source,other])
        self.assertNotIn('letter_main_start_date',result)

class PersistentConflictTests(unittest.TestCase):
    def test_valid_conflicting_summary_cannot_be_revived_by_vertical_recovery(self):
        source=vertical_programs();other=table([('School','Other College'),('Dates','27 January 2028 to 21 December 2028')],'other',2)
        raw={'fields':{'main_school':claim('Other College',other)},'programs':[{'kind':'main','school':claim('Other College',other),
             'course_start_date':claim('2028-01-27',other),'course_end_date':claim('2028-12-21',other)}]}
        result=review_candidates('confirmation',raw,[source,other])
        self.assertNotIn('letter_main_course_name',result)
        self.assertNotIn('letter_main_start_date',result)

    def test_explicit_absence_cannot_be_revived_by_vertical_language_program(self):
        source=vertical_programs();absence=table([('English language centre','Not applicable')],'absence',2)
        result=review_candidates('confirmation',{'fields':{},'programs':[]},[source,absence])
        self.assertNotIn('letter_language_course_name',result)
        self.assertNotIn('letter_language_start_date',result)

    def test_generic_model_label_does_not_block_a_named_language_school(self):
        source=offer('Language Academy');generic=claim('English Language Centre',source)
        result=review_candidates('offer',{'fields':{'language_school':generic},'programs':[{'kind':'language','school':generic}]},[source])
        self.assertEqual(result['language_school_name']['value'],'Language Academy')

class BlockedMainDateTests(unittest.TestCase):
    def test_conflicting_offer_school_also_blocks_course_date(self):
        source=offer();other=table([('School','Other College'),('Course Start Date','27 January 2028')],'other',2)
        raw={'fields':{'main_school':claim('Other College',other),'course_start_date':claim('2028-01-27',other)},'programs':[]}
        self.assertNotIn('course_start_date',review_candidates('offer',raw,[source,other]))

class SummaryDateConflictTests(unittest.TestCase):
    def test_summary_only_competing_language_dates_are_not_overwritten(self):
        source=vertical_programs();other=table([('School','Sample International High School'),('English program dates','27 January 2028 to 21 December 2028')],'other',2)
        raw={'fields':{'language_school':claim('Sample International High School',other),
             'language_start_date':claim('2028-01-27',other),'language_end_date':claim('2028-12-21',other)},'programs':[]}
        result=review_candidates('confirmation',raw,[source,other])
        self.assertNotIn('letter_language_start_date',result)
        self.assertNotIn('letter_language_end_date',result)

    def test_summary_only_competing_course_dates_are_not_overwritten(self):
        source=vertical_programs();other=table([('School','Sample International High School'),('Course dates','27 January 2028 to 21 December 2028')],'other',2)
        raw={'fields':{'main_school':claim('Sample International High School',other),
             'course_start_date':claim('2028-01-27',other),'course_end_date':claim('2028-12-21',other)},'programs':[]}
        result=review_candidates('confirmation',raw,[source,other])
        self.assertNotIn('letter_main_start_date',result)
