import unittest
from html.parser import HTMLParser

class Controls(HTMLParser):
    def __init__(self, text):
        super().__init__(); self.forms=[];self.inputs=[];self.feed(text)
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag=='form':self.forms.append(a)
        if tag in ('input','select','button'):self.inputs.append(a)

class ProcessingUI(unittest.TestCase):
    def test_edit_targets_same_document_and_escapes_display_name(self):
        from visa_document_processing_ui import document_edit_html
        html=document_edit_html(1,'csrf',{'id':7,'archived_at':None,'display_name':'<script>evil</script>','effective_category':'salary','manual_override':True,'auto_category':'deposit'})
        controls=Controls(html)
        self.assertTrue(any(x.get('name')=='document_id' and x.get('value')=='7' for x in controls.inputs))
        self.assertTrue(any(x.get('name')=='category' for x in controls.inputs))
        self.assertNotIn('<script>evil</script>',html)
        self.assertIn('Manual / 人工',html)
        self.assertNotIn('Filename classification preview',html)
        self.assertTrue(all(x['method']=='post' for x in controls.forms))
    def test_missing_metrics_never_render_fake_zero_and_manual_medical_is_separate(self):
        from visa_document_processing_ui import processing_dates_html
        html=processing_dates_html(1,'csrf',{'fields':{},'total_processing_days':None,'medical_completed_to_grant_days':None,'validation':[]},[])
        self.assertIn('Pending',html);self.assertNotIn('0 days',html)
        self.assertIn('Health Exam Date uses only an explicitly completed examination date. Manual corrections are retained.',html)
        controls=Controls(html)
        self.assertTrue(any(x.get('name')=='medical_completed_date' for x in controls.inputs))
    def test_same_day_zero_is_available_and_source_selection_is_explicit(self):
        from visa_document_processing_ui import processing_dates_html
        html=processing_dates_html(1,'csrf',{'fields':{},'total_processing_days':0,'medical_completed_to_grant_days':5,'validation':['Grant before acknowledgement']},[{'id':7,'display_name':'letter.txt','archived_at':None}])
        self.assertIn('0 days',html);self.assertIn('5 days',html)
        self.assertIn('Grant before acknowledgement',html)
        self.assertTrue(any(x.get('name')=='source_grant_sent_on' for x in Controls(html).inputs))

    def test_rule_buttons_show_staff_actions_not_template_expressions(self):
        from visa_document_processing_ui import keyword_rules_html
        html=keyword_rules_html(1,'csrf',[{'id':1,'keyword':'deposit','category':'deposit','priority':100,'active':True}])
        self.assertIn('<button>Save rule</button>',html)
        self.assertIn('<button>Add rule</button>',html)
        self.assertNotIn('if rid else',html)
