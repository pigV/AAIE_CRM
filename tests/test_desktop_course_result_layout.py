import unittest
from html.parser import HTMLParser
from revenue.school_courses import render_course_fields
from visa_document_processing_ui import processing_dates_html

class Nodes(HTMLParser):
    def __init__(self,html):
        super().__init__();self.nodes=[];self.feed(html)
    def handle_starttag(self,tag,attrs):self.nodes.append((tag,dict(attrs)))

class DesktopLayoutTests(unittest.TestCase):
    def test_school_search_keeps_distinct_crm_ids_and_safe_labels(self):
        html=render_course_fields(4,[{'id':9,'school_name':'Same <College>'},{'id':10,'school_name':'Same <College>'}],{'school_id':10})
        nodes=Nodes(html).nodes
        inputs=[a for t,a in nodes if t=='input' and 'data-course-school-search' in a]
        self.assertEqual(2,len(inputs))
        self.assertEqual(2,len({a['aria-controls'] for a in inputs}))
        selects=[a for t,a in nodes if t=='select' and 'data-course-school-value' in a]
        self.assertEqual({'course_language_school_4','course_main_school_4'},{a['name'] for a in selects})
        self.assertIn('Same &lt;College&gt;',html)
        self.assertNotIn('<College>',html)
        self.assertIn('value="10" selected',html)

    def test_compact_dates_keep_one_real_edit_form_behind_disclosure(self):
        summary={'fields':{'grant_sent_on':{'value':'2026-11-13'}},'validation':['Check source <letter>']}
        html=processing_dates_html(74,'csrf',summary,[],compact=True)
        nodes=Nodes(html).nodes
        details=[a for t,a in nodes if t=='details' and 'data-result-date-editor' in a]
        self.assertEqual(1,len(details));self.assertNotIn('open',details[0])
        self.assertEqual(1,html.count('name="action" value="correct-processing-dates"'))
        self.assertEqual(1,html.count('name="grant_sent_on"'))
        self.assertIn('2026-11-13',html)
        self.assertIn('Check source &lt;letter&gt;',html)
        self.assertIn('data-result-date-summary',html)
        full=processing_dates_html(74,'csrf',summary,[],compact=False)
        self.assertNotIn('data-result-date-editor',full)
