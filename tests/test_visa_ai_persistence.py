import unittest, sqlite3,json
from tests.test_visa_documents_candidate import SCHEMA
class AIPersistenceTests(unittest.TestCase):
 def setUp(self):
  self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row;self.c.executescript(SCHEMA);self.addCleanup(self.c.close)
  import visa_documents_candidate as s
  self.actor={'actor_user_id':1,'occurred_at':'2026-09-07T12:00:00'}
  self.doc=s.register_document(self.c,1,file_path='/synthetic/a.pdf',original_name='a.pdf',version=1,sha256='a'*64,**self.actor)
  self.result={'result':s.propose_letter('Visa grant notice\nDate of grant: 7 September 2026'),'metadata':{'provider':'openai','model':'test-model','request_hash':'b'*64,'response_hash':'c'*64,'source_evidence':[{'document_id':self.doc['id'],'sha256':'a'*64,'version':1}]}}
 def test_letter_ai_is_proposal_and_retains_metadata(self):
  from visa_ai_persistence import save_ai_letter
  row=save_ai_letter(self.c,1,self.doc['id'],self.result,**self.actor)
  self.assertEqual('openai:test-model',row['extraction_method']);self.assertEqual('Pending',self.c.execute('SELECT result_status FROM visa_cases WHERE id=1').fetchone()[0])
  self.assertIn('request_hash',self.c.execute('SELECT after_json FROM visa_document_audit ORDER BY id DESC LIMIT 1').fetchone()[0])
 def test_drift_or_wrong_document_rejected_before_store(self):
  from visa_ai_persistence import save_ai_letter
  self.result['metadata']['source_evidence'][0]['sha256']='x'*64
  with self.assertRaises(ValueError):save_ai_letter(self.c,1,self.doc['id'],self.result,**self.actor)
  self.assertEqual(0,self.c.execute('SELECT count(*) FROM visa_letter_extractions').fetchone()[0])

 def test_analysis_requires_exact_active_submitted_versions_and_retains_full_result(self):
  from visa_ai_persistence import save_ai_analysis
  import visa_documents_candidate as service
  envelope={'result':{'Summary':'Summary','Risk':'Risk','Missing':'Missing','Action Required':'Review','full_analysis':'Full result'},'metadata':self.result['metadata']}
  with self.assertRaises(ValueError):save_ai_analysis(self.c,1,envelope,**self.actor)
  service.mark_submitted(self.c,1,self.doc['id'],**self.actor)
  save_ai_analysis(self.c,1,envelope,**self.actor)
  self.assertIn('Full result',self.c.execute("SELECT after_json FROM visa_document_audit WHERE action='ai_analysis_completed'").fetchone()[0])
  service.set_document_archived(self.c,1,self.doc['id'],archived=True,**self.actor)
  with self.assertRaises(ValueError):save_ai_analysis(self.c,1,envelope,**self.actor)
