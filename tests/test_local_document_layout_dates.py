"""Synthetic layouts: dates must be bound to their explicit role and source position."""
import unittest
from local_document_lab.layout_dates import resolve_language_dates
from local_document_lab.evidence import validate_result
from local_document_lab.project import project_program_fields

def b(key,text,x=10,y=400,page=1):
 return {'id':key,'text':text,'kind':'text','page':page,'bbox':{'l':x,'r':x+80,'t':y,'b':y-8,'coord_origin':'BOTTOMLEFT'}}
def claim(value,block):return {'value':value,'quote':block['text'],'block_id':block['id']}
class LayoutDateTests(unittest.TestCase):
 def test_explicit_named_language_period_resolves_generic_course_end_conflict(self):
  school=b('school','English Language Centre: Example College')
  paragraph=b('period','Example College English Language Program runs from 19 July 2027 to 17 December 2027, subject to assessment.')
  generic=b('overall','Course Dates: 19 July 2027 to 20 December 2029')
  blocks=[school,paragraph,generic]
  raw={'fields':{'language_school':claim('Example College',school),'language_start_date':claim('2027-07-19',generic),'language_end_date':claim('2029-12-20',generic)},'programs':[{'kind':'language','school':claim('Example College',school),'course_end_date':claim('2029-12-20',generic)}]}
  result=project_program_fields(validate_result(resolve_language_dates(raw,blocks),blocks))
  self.assertEqual(result['fields']['language_end_date']['value'],'2027-12-17')
  self.assertEqual(result['fields']['language_end_date']['block_id'],'period')
  self.assertEqual(raw['fields']['language_end_date']['value'],'2029-12-20')
 def test_same_row_date_labels_use_right_hand_values_not_next_text_order(self):
  school=b('anchor','High School Preparation at Sample IEC Start Date',10,400)
  address=b('address','Address line',120,420)
  start=b('start','19-Jul-2027',120,400)
  end_label=b('endlabel','Finish Date',220,400)
  end=b('end','17-Dec-2027',320,400)
  other=b('other','20-Dec-2029',120,200)
  blocks=[school,address,start,end_label,end,other]
  raw={'fields':{'language_school':claim('Sample IEC',school)},'programs':[]}
  result=project_program_fields(validate_result(resolve_language_dates(raw,blocks),blocks))
  self.assertEqual(result['fields']['language_start_date']['value'],'2027-07-19')
  self.assertEqual(result['fields']['language_end_date']['value'],'2027-12-17')
  self.assertEqual(result['programs'][0]['fields']['course_end_date']['block_id'],'end')
 def test_ambiguous_dates_same_position_abstain(self):
  school=b('anchor','High School Preparation at Sample IEC Start Date',10,400)
  blocks=[school,b('start','19-Jul-2027',120,400),b('duplicate','20-Jul-2027',120,400),b('endlabel','Finish Date',220,400),b('end','17-Dec-2027',320,400)]
  raw={'fields':{'language_school':claim('Sample IEC',school)},'programs':[]}
  self.assertNotIn('language_start_date',resolve_language_dates(raw,blocks)['fields'])
 def test_main_course_date_and_other_school_do_not_become_language_dates(self):
  school=b('anchor','Sample IEC',10,400)
  blocks=[school,b('main','Main school course runs from 19 July 2027 to 20 December 2029'),b('other','Other College English Language Program runs from 19 July 2027 to 17 December 2027')]
  raw={'fields':{'language_school':claim('Sample IEC',school)},'programs':[]}
  self.assertNotIn('language_start_date',resolve_language_dates(raw,blocks)['fields'])
 def test_two_explicit_language_periods_and_cross_page_rows_abstain(self):
  school=b('anchor','High School Preparation at Sample IEC Start Date',10,400)
  blocks=[school,b('start','19-Jul-2027',120,400,page=2),b('endlabel','Finish Date',220,400),b('end','17-Dec-2027',320,400),b('p1','Sample IEC English Language Program from 19 July 2027 to 17 December 2027'),b('p2','Sample IEC English Language Program from 20 July 2027 to 18 December 2027')]
  raw={'fields':{'language_school':claim('Sample IEC',school)},'programs':[]}
  self.assertNotIn('language_start_date',resolve_language_dates(raw,blocks)['fields'])

class ResolverSafetyTests(unittest.TestCase):
 def raw(self,school,period=None):
  result={'fields':{'language_school':claim('Sample College',school)},'programs':[{'kind':'language','school':claim('Sample College',school)}]}
  if period:
   for target,v in [('language_start_date','2027-07-19'),('language_end_date','2027-12-17')]:result['fields'][target]=claim(v,period)
   result['programs'][0].update(course_start_date=claim('2027-07-19',period),course_end_date=claim('2027-12-17',period))
  return result
 def test_mixed_main_course_sentence_cannot_supply_language_period(self):
  school=b('school','Sample College English Language Centre')
  main=b('main','Sample College offers English language preparation; its main secondary program runs from 19 July 2027 to 20 December 2029.')
  result=resolve_language_dates(self.raw(school),[school,main])
  self.assertNotIn('language_start_date',result['fields'])
 def test_other_institution_containing_the_name_is_not_the_same_school(self):
  school=b('school','Sample College English Language Centre')
  other=b('other','Other Sample College English language program from 19 July 2027 to 17 December 2027.')
  self.assertNotIn('language_start_date',resolve_language_dates(self.raw(school),[school,other])['fields'])
 def test_competing_explicit_periods_clear_model_selected_dates(self):
  school=b('school','Sample College English Language Centre');one=b('one','Sample College English language program from 19 July 2027 to 17 December 2027.');two=b('two','Sample College English language program from 20 July 2027 to 18 December 2027.')
  result=project_program_fields(validate_result(resolve_language_dates(self.raw(school,one),[school,one,two]),[school,one,two]))
  self.assertFalse(result['fields'].get('language_start_date',{}).get('value'))
  self.assertFalse(result['programs'][0]['fields'].get('course_end_date',{}).get('value'))
 def test_duplicate_source_periods_are_not_collapsed_by_equal_dates(self):
  school=b('school','Sample College English Language Centre');one=b('one','Sample College English language program from 19 July 2027 to 17 December 2027.');two=b('two',one['text'],page=2)
  self.assertNotIn('language_start_date',resolve_language_dates(self.raw(school),[school,one,two])['fields'])
 def test_cross_page_value_is_rejected_without_competing_prose(self):
  school=b('anchor','High School Preparation at Sample IEC Start Date',10,400)
  blocks=[school,b('start','19-Jul-2027',120,400,page=2),b('label','Finish Date',220,400),b('end','17-Dec-2027',320,400)]
  raw={'fields':{'language_school':claim('Sample IEC',school)},'programs':[]}
  self.assertNotIn('language_start_date',resolve_language_dates(raw,blocks)['fields'])

class ExplicitLanguageClauseTests(unittest.TestCase):
 def test_named_language_studies_clause_after_secondary_context_is_not_main_period(self):
  school=b('s','English Language Centre: Example Secondary College')
  period=b('p','Admission to Example Secondary College is subject to completing English language studies at Example Secondary College located at 12 Example Road from 19 July 2027 to 17 December 2027.')
  generic=b('g','Course Dates: 19 July 2027 to 20 December 2029')
  raw={'fields':{'language_school':claim('Example Secondary College',school),'language_start_date':claim('2027-07-19',period),'language_end_date':claim('2027-12-17',period)},'programs':[{'kind':'language','school':claim('Example Secondary College',school),'course_start_date':claim('2027-07-19',generic),'course_end_date':claim('2029-12-20',generic)}]}
  result=project_program_fields(validate_result(resolve_language_dates(raw,[school,period,generic]),[school,period,generic]))
  self.assertEqual(result['fields']['language_end_date']['value'],'2027-12-17')
  self.assertEqual(result['programs'][0]['fields']['course_end_date']['block_id'],'p')
 def test_explicit_language_then_main_program_range_is_rejected(self):
  school=b('s','English Language Centre: Example Secondary College')
  period=b('p','English language studies at Example Secondary College prepare you for the main secondary program from 19 July 2027 to 20 December 2029.')
  raw={'fields':{'language_school':claim('Example Secondary College',school)},'programs':[]}
  self.assertNotIn('language_end_date',resolve_language_dates(raw,[school,period])['fields'])
 def test_explicit_language_role_does_not_match_other_school_suffix(self):
  school=b('s','English Language Centre: Example College')
  period=b('p','English language studies at Example College International from 19 July 2027 to 17 December 2027.')
  raw={'fields':{'language_school':claim('Example College',school)},'programs':[]}
  self.assertNotIn('language_end_date',resolve_language_dates(raw,[school,period])['fields'])

class LanguageTransitionSafetyTests(unittest.TestCase):
 def test_following_diploma_program_does_not_supply_language_dates(self):
  school=b('s','English Language Centre: Example College')
  period=b('p','Admission to Example Secondary College requires English language studies at Example College, followed by a diploma program from 19 July 2027 to 17 December 2029.')
  raw={'fields':{'language_school':claim('Example College',school)},'programs':[]}
  self.assertNotIn('language_end_date',resolve_language_dates(raw,[school,period])['fields'])
 def test_following_program_after_address_does_not_supply_language_dates(self):
  school=b('s','English Language Centre: Example College')
  period=b('p','Admission to Example Secondary College requires English language studies at Example College located at 12 Example Road, followed by training from 19 July 2027 to 17 December 2029.')
  raw={'fields':{'language_school':claim('Example College',school)},'programs':[]}
  self.assertNotIn('language_end_date',resolve_language_dates(raw,[school,period])['fields'])
