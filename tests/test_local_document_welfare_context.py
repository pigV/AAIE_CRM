"""Synthetic split CAAW headers: evidence context cannot be guessed from a title."""
import unittest
from application_document_extraction import review_candidates

def b(key,text,y=500,page=1,kind='text',x=80):
 return {'id':key,'text':text,'kind':kind,'page':page,'bbox':{'l':x,'r':x+110,'t':y,'b':y-10,'coord_origin':'BOTTOMLEFT'}}
def raw(block,field='welfare_start_date',value='2027-07-19'):
 return {'fields':{field:{'value':value,'quote':block['text'],'block_id':block['id']}},'programs':[]}
class WelfareContextTests(unittest.TestCase):
 def test_split_header_keeps_start_date_and_both_evidence_blocks(self):
  header=b('h','Welfare arrangements',520,kind='section_header');date=b('d','Start Date: 19-Jul-2027')
  c=review_candidates('caaw',raw(date),[header,date])['caaw_welfare_start_date']
  self.assertEqual(c['value'],'2027-07-19');self.assertEqual(c['context_block_id'],'h')
  self.assertIn(header['text'],c['snippet']);self.assertIn(date['text'],c['snippet']);self.assertEqual(c['quote'],date['text'])
 def test_split_header_keeps_end_date(self):
  h=b('h','Welfare arrangements',520,kind='section_header');d=b('d','End Date: 17-Dec-2027',x=330)
  self.assertIn('caaw_welfare_end_date',review_candidates('caaw',raw(d,'welfare_end_date','2027-12-17'),[h,d]))
 def test_course_date_under_welfare_title_is_not_welfare_date(self):
  h=b('h','Confirmation of appropriate accommodation and welfare',760,kind='section_header');d=b('d','Course Start Date: 19-Jul-2027')
  self.assertEqual(review_candidates('caaw',raw(d),[h,d]),{})
 def test_nearer_course_heading_blocks_welfare_context(self):
  h=b('h','Welfare arrangements',540,kind='section_header');course=b('c','Course details',520,kind='section_header');d=b('d','Start Date: 19-Jul-2027')
  self.assertEqual(review_candidates('caaw',raw(d),[h,course,d]),{})
 def test_wrong_role_cross_page_and_page_precision_abstain(self):
  h=b('h','Welfare arrangements',520,kind='section_header');d=b('d','End Date: 19-Jul-2027')
  self.assertEqual(review_candidates('caaw',raw(d),[h,d]),{})
  d=b('d','Start Date: 19-Jul-2027',page=2)
  self.assertEqual(review_candidates('caaw',raw(d),[h,d]),{})
  d=b('d','Start Date: 19-Jul-2027');d['bbox_precision']='page'
  self.assertEqual(review_candidates('caaw',raw(d),[h,d]),{})
 def test_two_dates_in_cell_and_duplicate_headers_abstain(self):
  h=b('h','Welfare arrangements',520,kind='section_header');d=b('d','Start Date: 19-Jul-2027, End Date: 17-Dec-2027')
  self.assertEqual(review_candidates('caaw',raw(d),[h,d]),{})
  d=b('d','Start Date: 19-Jul-2027');h2=dict(h,id='h2')
  self.assertEqual(review_candidates('caaw',raw(d),[h,h2,d]),{})

class WelfareColumnSafetyTests(unittest.TestCase):
 def test_overlapping_course_heading_blocks_distant_left_welfare_heading(self):
  h=b('h','Welfare arrangements',520,kind='section_header',x=80)
  c=b('c','Course details',520,kind='section_header',x=360)
  d=b('d','Start Date: 19-Jul-2027',x=330)
  self.assertEqual(review_candidates('caaw',raw(d),[h,c,d]),{})
