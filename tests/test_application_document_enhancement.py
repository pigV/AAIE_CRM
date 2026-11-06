"""Synthetic fixtures only; never import CRM or its database."""
import tempfile,unittest,time
from pathlib import Path
from unittest.mock import patch
import application_document_extraction as service

def fixture_temporary_root():
 root=Path(__file__).resolve().parents[1]/"STAGING_DATA/tmp"
 root.mkdir(parents=True,exist_ok=True)
 return root

B=[{"id":"a","page":2,"bbox":[0,0,10,10],"text":"Sample Secondary School Junior Secondary Year 8 19-Jul-2027 17-Dec-2027"}]
def claim(v):return {"value":v,"block_id":"a","quote":B[0]["text"]}
def raw():return {"fields":{"main_school":claim("Sample Secondary School"),"year_level":claim("Year 8")},"programs":[{"kind":"main","school":claim("Sample Secondary School"),"course":claim("Junior Secondary"),"course_start_date":claim("2027-07-19"),"course_end_date":claim("2027-12-17")}]}

class AdapterTests(unittest.TestCase):
 def test_confirmation_maps_sector_and_main_period_without_language_inference(self):
  v=service.review_candidates("confirmation",raw(),B)
  self.assertEqual(v["letter_main_course_name"]["value"],"Sample Secondary School")
  self.assertEqual(v["secondary_main_course_school"]["value"],"Sample Secondary School")
  self.assertNotIn("primary_main_course_school",v)
  self.assertNotIn("letter_language_course_name",v)
  self.assertEqual(v["letter_main_start_date"]["value"],"2027-07-19")
  self.assertEqual(v["letter_main_end_date"]["page"],2)
  self.assertEqual(v["letter_year_level"]["value"],"Year 8")
 def test_offer_preserves_money_and_manual_and_offers_conflicting_candidate(self):
  c=service.review_candidates("offer",raw(),B)
  original={"fields":{"school_name":{"value":"Human choice"},"deposit_amount":{"value":"900.00"},"offer_date":{"value":"2026-11-01"},"language_school_name":{"value":""}}}
  merged=service.merge_review(original,c)
  self.assertEqual(merged["fields"]["school_name"]["value"],"Human choice")
  self.assertEqual(merged["fields"]["school_name"]["local_candidate"]["value"],"Sample Secondary School")
  self.assertEqual(merged["fields"]["deposit_amount"]["value"],"900.00")
  self.assertEqual(merged["fields"]["offer_date"]["value"],"2026-11-01")
  self.assertEqual(original["fields"]["school_name"],{"value":"Human choice"})
 def test_forged_quote_and_course_dates_cannot_fill_welfare(self):
  r=raw();r["fields"]["welfare_start_date"]=claim("2027-07-19")
  r["fields"]["provider"]={"value":"Fake College","quote":"Fake College","block_id":"a"}
  self.assertEqual(service.review_candidates("caaw",r,B),{})
 def test_two_secondary_schools_abstain_from_sector_summary(self):
  r=raw();r["programs"].append({"kind":"main","school":claim("School"),"course":claim("Junior Secondary")})
  v=service.review_candidates("confirmation",r,B)
  self.assertNotIn("secondary_main_course_school",v)
  self.assertNotIn("letter_main_start_date",v)
 def test_type_switch_does_not_expose_other_type_fields(self):
  self.assertEqual(service.review_candidates("oshc",raw(),B),{})

class ReviewBoundaryTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  import sys
  sys.path.insert(0,str(Path(__file__).resolve().parent))
  from test_application_document_extraction import parser_namespace,upload
  cls.ns=parser_namespace();cls.upload=staticmethod(upload)
 def test_existing_review_boundary_adds_local_candidates_without_overwriting(self):
  with patch('application_document_extraction.development_queue') as factory:
   class Ready:
    def submit(self,*args):return {"status":"completed","job_id":"id","fields":{"language_school_name":{"value":"Sample IEC","status":"needs_review","message":"Review","snippet":"Sample IEC","page":3}}}
   factory.return_value=Ready()
   data=self.ns['extraction_review_data'](self.upload(b'%PDF-synthetic','sample.pdf','application/pdf'),'Offer','Offer')
   self.assertEqual(data['fields']['language_school_name']['value'],'Sample IEC')
   self.assertEqual(data['local_extraction']['status'],'completed')
 def test_rendered_source_page_and_conflicting_candidate_survive_html_escaping(self):
  data={"text_found":True,"source":"Local","document_type":"Offer","fields":{"school_name":{"value":"Old","status":"needs_review","message":"Review","snippet":"Old","page":4,"local_candidate":{"value":"New <College>","snippet":"New <College>","page":2}}}}
  rendered=self.ns['extraction_review_html'](0,'Offer',data)
  self.assertIn('data-source-page="4"',rendered)
  self.assertIn('data-local-candidate',rendered)
  self.assertNotIn('New <College>',rendered)

class EndpointTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  import ast
  tree=ast.parse((Path(__file__).resolve().parents[1]/'app.py').read_text())
  node=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='upload_review_local_extraction')
  cls.node=node
 def invoke(self,*,student='7',permission=True,method='GET',kind='Offer'):
  import ast
  from types import SimpleNamespace
  ns={'load_upload_review':lambda token:(Path('.'),{'student_id':'7','module':'Offer','files':[{'index':0}]}),
      'can_review_module_action':lambda *a:permission,'APP_DIR':Path('.'),'STAGING':True,
      'module_document_types':lambda m:['Offer'],
      'upload_from_review':lambda *a:SimpleNamespace(filename='test.pdf',data=b'%PDF-synthetic')}
  exec(compile(ast.Module(body=[self.node],type_ignores=[]),'app.py','exec'),ns)
  fake=SimpleNamespace(user={},query={'document_type':[kind]},json_response=lambda payload,status=200:(status,payload))
  return ns['upload_review_local_extraction'](fake,method,student,'token','0')
 def test_wrong_student_cannot_read_extraction(self):self.assertEqual(self.invoke(student='8')[0],400)
 def test_missing_module_permission_is_denied(self):self.assertEqual(self.invoke(permission=False)[0],403)
 def test_write_method_is_rejected(self):self.assertEqual(self.invoke(method='POST')[0],405)
 def test_document_type_outside_module_does_not_enqueue(self):
  with patch('application_document_extraction.development_queue') as factory:
   result=self.invoke(kind='CAAW')
   self.assertEqual(result[1]['status'],'manual_review');factory.return_value.submit.assert_not_called()

class DirectoryQueueTests(unittest.TestCase):
 def test_request_and_independent_worker_handoff_is_bound_to_source_and_kind(self):
  import json
  with tempfile.TemporaryDirectory(dir=fixture_temporary_root()) as name:
   root=Path(name)
   (root/'heartbeat.json').write_text(json.dumps({'updated_at':time.time()}))
   q=service.DirectoryReviewQueue(root,'test')
   first=q.submit(b'%PDF-synthetic','confirmation')
   self.assertEqual(first['status'],'queued')
   self.assertTrue(service.broker_once(root,'test',lambda *a:{'fields':{'letter_year_level':{'value':'Year 8'}}}))
   done=q.submit(b'%PDF-synthetic','confirmation')
   self.assertEqual(done['status'],'completed')
   self.assertEqual(done['fields']['letter_year_level']['value'],'Year 8')
   self.assertNotEqual(first['job_id'],q.submit(b'%PDF-other','confirmation')['job_id'])
   folder=root/first['job_id'];(folder/'source.pdf').write_bytes(b'%PDF-tampered')
   self.assertEqual(q.submit(b'%PDF-synthetic','confirmation')['status'],'manual_review')
 def test_dead_broker_does_not_leave_ui_waiting_forever(self):
  with tempfile.TemporaryDirectory(dir=fixture_temporary_root()) as name:
   q=service.DirectoryReviewQueue(Path(name),'test')
   self.assertEqual(q.submit(b'%PDF-synthetic','coe')['status'],'manual_review')
 def test_worker_exception_is_a_manual_review_and_does_not_escape(self):
  import json
  with tempfile.TemporaryDirectory(dir=fixture_temporary_root()) as name:
   root=Path(name);(root/'heartbeat.json').write_text(json.dumps({'updated_at':time.time()}))
   q=service.DirectoryReviewQueue(root,'test');q.submit(b'%PDF-synthetic','coe')
   def fail(*a):raise TimeoutError('private data')
   service.broker_once(root,'test',fail)
   result=q.submit(b'%PDF-synthetic','coe')
   self.assertEqual(result['status'],'manual_review');self.assertNotIn('private',str(result))

class SafetyRegressionTests(unittest.TestCase):
 def test_multiple_main_schools_do_not_offer_first_school_as_summary(self):
  r=raw();r['programs'].append({'kind':'main','school':claim('School'),'course':claim('Junior Secondary')})
  self.assertNotIn('letter_main_course_name',service.review_candidates('confirmation',r,B))
 def test_unexpected_queue_failure_preserves_existing_review(self):
  from types import SimpleNamespace
  original={'document_type':'Offer','fields':{'school_name':{'value':'Human choice'}}}
  with patch('application_document_extraction.development_queue') as factory:
   factory.return_value.submit.side_effect=OSError('private path')
   result=service.enhance_review(original,SimpleNamespace(filename='test.pdf',data=b'%PDF-test'),Path('.'),True)
  self.assertEqual(result['fields']['school_name']['value'],'Human choice')
  self.assertEqual(result['local_extraction']['status'],'manual_review')

class ReviewFixTests(unittest.TestCase):
 def test_unsupported_initial_type_still_installs_type_change_controller(self):
  from types import SimpleNamespace
  data={'document_type':'OSHC','fields':{}}
  with patch('application_document_extraction.development_queue') as factory:
   result=service.enhance_review(data,SimpleNamespace(filename='test.pdf',data=b'%PDF-test'),Path('.'),True)
   self.assertIn('local_extraction',result)
   factory.return_value.submit.assert_not_called()
 def test_sector_schools_are_explicit_informational_not_discarded_editable_values(self):
  from types import SimpleNamespace
  data={'document_type':'LetterOfConfirmation','fields':{'primary_main_course_school':{'value':'Sample Primary'},'secondary_main_course_school':{'value':''}}}
  with patch('application_document_extraction.development_queue') as factory:
   factory.return_value.submit.return_value={'status':'queued','fields':{}}
   result=service.enhance_review(data,SimpleNamespace(filename='test.pdf',data=b'%PDF-test'),Path('.'),True)
  self.assertTrue(result['fields']['primary_main_course_school'].get('informational'))
 def test_projection_does_not_expose_reversed_dates_combined_from_partial_claims(self):
  blocks=[{'id':'a','page':1,'text':'Sample IEC 19-Jul-2027 30-Jun-2027','bbox':[]}]
  c=lambda v:{'value':v,'quote':blocks[0]['text'],'block_id':'a'}
  r={'fields':{'language_school':c('Sample IEC'),'language_start_date':c('2027-07-19')},'programs':[{'kind':'language','school':c('Sample IEC'),'course_end_date':c('2027-06-30')}]}
  candidates=service.review_candidates('confirmation',r,blocks)
  self.assertNotIn('letter_language_start_date',candidates)
  self.assertNotIn('letter_language_end_date',candidates)

class InformationalRenderTests(unittest.TestCase):
 def test_sector_reference_has_no_submitted_input_or_false_save_choice(self):
  import sys
  sys.path.insert(0,str(Path(__file__).resolve().parent))
  from test_application_document_extraction import parser_namespace
  ns=parser_namespace()
  data={'text_found':True,'source':'Local','document_type':'LetterOfConfirmation','fields':{'primary_main_course_school':{'value':'Sample Primary','status':'needs_review','message':'Reference','snippet':'Sample Primary','page':2,'informational':True}}}
  rendered=ns['extraction_review_html'](0,'COE + OSHC',data)
  self.assertIn('data-local-informational-field="primary_main_course_school"',rendered)
  self.assertNotIn('name="extract_primary_main_course_school_0"',rendered)
  self.assertIn('不单独保存',rendered)

class LayoutBoundaryTests(unittest.TestCase):
 def test_explicit_language_period_is_applied_at_real_review_candidate_boundary(self):
  blocks=[{'id':'school','text':'Sample College English Language Centre','page':2,'kind':'text','bbox':[]},
          {'id':'generic','text':'Course Dates: 19 July 2027 to 20 December 2029','page':2,'kind':'text','bbox':[]},
          {'id':'specific','text':'Sample College English language program from 19 July 2027 to 17 December 2027','page':2,'kind':'text','bbox':[]}]
  c=lambda value,key:{'value':value,'quote':next(b['text'] for b in blocks if b['id']==key),'block_id':key}
  r={'fields':{'language_school':c('Sample College','school'),'language_end_date':c('2029-12-20','generic')},'programs':[{'kind':'language','school':c('Sample College','school'),'course_end_date':c('2029-12-20','generic')}]}
  candidates=service.review_candidates('confirmation',r,blocks)
  self.assertEqual(candidates['letter_language_end_date']['value'],'2027-12-17')
  self.assertEqual(candidates['letter_language_end_date']['block_id'],'specific')

class ScopedEnablementTests(unittest.TestCase):
 def test_browser_acceptance_enablement_does_not_activate_other_processes(self):
  import json,os
  with tempfile.TemporaryDirectory(dir=fixture_temporary_root()) as name:
   root=Path(name);(root/'local-document-review.json').write_text(json.dumps({'enabled':True,'scope':'browser-test'}))
   with patch.dict(os.environ,{},clear=True):self.assertFalse(service.review_enabled(root))
   with patch.dict(os.environ,{'AAIE_LOCAL_DOCUMENT_REVIEW_SCOPE':'browser-test'},clear=True):self.assertTrue(service.review_enabled(root))
   with patch.dict(os.environ,{'AAIE_LOCAL_DOCUMENT_REVIEW_SCOPE':'other'},clear=True):self.assertFalse(service.review_enabled(root))

class ScopeRequiredTests(unittest.TestCase):
 def test_unscoped_marker_cannot_enable_background_models(self):
  import json,os
  with tempfile.TemporaryDirectory(dir=fixture_temporary_root()) as name:
   root=Path(name);(root/'local-document-review.json').write_text(json.dumps({'enabled':True}))
   with patch.dict(os.environ,{},clear=True):self.assertFalse(service.review_enabled(root))
