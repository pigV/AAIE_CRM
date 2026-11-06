import unittest
from local_document_lab.parse import layout_page_end
class PagePlanTests(unittest.TestCase):
 def legal(self):return 'Terms and conditions apply to all applicants. Fees are payable according to the published policy. '*12
 def test_text_only_legal_tail_uses_fast_text_without_dropping_it(self):
  self.assertEqual(layout_page_end(['Cover details','Intro','Course Details']+[self.legal()]*10),3)
 def test_later_explicit_course_page_is_always_in_layout_prefix(self):
  pages=['Cover','Intro','Other',self.legal(),'Course Details\nSchool: Sample College\nStart Date: 19/07/2027',self.legal()]
  self.assertEqual(layout_page_end(pages),5)
 def test_scanned_or_garbled_tail_keeps_layout_and_ocr(self):
  self.assertEqual(layout_page_end(['Cover','Intro','Details',self.legal(),'']),5)
 def test_later_date_facts_keep_layout_even_without_a_heading(self):
  self.assertEqual(layout_page_end(['Cover','Intro','Details',self.legal(),'Sample school program from 19 July 2027 to 17 December 2027. '*3]),5)
