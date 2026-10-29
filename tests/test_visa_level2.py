import hashlib, json, sqlite3, unittest
from pathlib import Path
from tests.test_visa_documents_candidate import SCHEMA, STAMP
import visa_documents_candidate as documents
import visa_processing_dates as dates

class VisaLevel2Tests(unittest.TestCase):
 def setUp(self):
  self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row;self.c.executescript(SCHEMA)
  self.c.executescript((Path(__file__).parents[1]/'migrations/20260909_038_visa_processing_dates.sql').read_text())
  self.c.executescript((Path(__file__).parents[1]/'migrations/20260914_039_visa_result_status_authority.sql').read_text())
  self.actor=dict(actor_user_id=1,occurred_at=STAMP)
 def tearDown(self):self.c.close()
 def doc(self,case=1,name='a.txt',data=b'a',category=None):
  return documents.register_document(self.c,case,file_path='/synthetic/'+name,original_name=name,version=1,sha256=hashlib.sha256(data).hexdigest(),category=category,**self.actor)
 def test_hash_renaming_case_and_deleted_reupload(self):
  d=self.doc()
  with self.assertRaisesRegex(ValueError,'File already exists'):self.doc(name='renamed.txt')
  self.doc(case=2)
  documents.set_document_archived(self.c,1,d['id'],archived=True,**self.actor)
  self.assertFalse(self.doc(name='again.txt')['archived_at'])
 def test_single_active_letters(self):
  for category in ['grant_letter','immi_acknowledgment']:
   with self.subTest(category=category):
    self.c.execute('UPDATE visa_case_documents SET archived_at=?',(STAMP,))
    d=self.doc(name=category+'.txt',data=category.encode(),category=category)
    with self.assertRaisesRegex(ValueError,'already has this document'):self.doc(name='second'+category+'.txt',data=('second'+category).encode(),category=category)
    documents.set_document_archived(self.c,1,d['id'],archived=True,**self.actor)
    self.doc(name='new'+category+'.txt',data=('new'+category).encode(),category=category)
 def test_status_without_dates_and_delete_fallback(self):
  from visa_document_lifecycle import case_projection
  ack=self.doc(name='ack.txt',category='immi_acknowledgment')
  self.assertEqual('审理中',case_projection(self.c,1)['workflow_status'])
  grant=self.doc(name='grant.txt',data=b'g',category='grant_letter')
  self.assertEqual(('Granted','已完成'),tuple(case_projection(self.c,1)[k] for k in ['result_status','workflow_status']))
  documents.set_document_archived(self.c,1,grant['id'],archived=True,**self.actor)
  self.assertEqual('审理中',case_projection(self.c,1)['workflow_status'])
  documents.set_document_archived(self.c,1,ack['id'],archived=True,**self.actor)
  self.assertEqual('未开始',case_projection(self.c,1)['workflow_status'])
 def test_completed_health_only_and_letter_dates(self):
  text='Health Examination\nDate: 1 September 2026\nGenerated 2 September 2026\nAppointment: 3 September 2026\nExamination completed: 4 September 2026'
  p=dates.parse_processing_dates(text,today='2026-09-08')
  self.assertEqual('2026-09-04',p['fields']['health_exam_date']['value'])
  for title,key in [('Acknowledgement of application','acknowledge_sent_on'),('IMMI Grant Notification','grant_sent_on')]:
   p=dates.parse_processing_dates(title+'\nDate: 5 September 2026'+('\nDate of Grant: 4 September 2026' if key=='grant_sent_on' else ''),today='2026-09-08')
   self.assertEqual('2026-09-05',p['fields'][key]['value'])
 def test_manual_priority_delete_and_clear(self):
  d=self.doc(category='grant_letter')
  dates.save_processing_extraction(self.c,1,d['id'],'IMMI Grant Notification\nSent On 5 September 2026',**self.actor)
  dates.correct_processing_dates(self.c,1,{'grant_sent_on':'2026-09-06'},source_document_ids={'grant_sent_on':d['id']},**self.actor)
  documents.set_document_archived(self.c,1,d['id'],archived=True,**self.actor)
  self.assertEqual('2026-09-06',dates.processing_summary(self.c,1)['fields']['grant_sent_on']['value'])
  dates.correct_processing_dates(self.c,1,{'grant_sent_on':''},**self.actor)
  self.assertEqual('',dates.processing_summary(self.c,1)['fields']['grant_sent_on']['value'])
 def test_existing_read_reconciliation_grant_date_no_writes(self):
  from visa_document_lifecycle import case_projection
  d=self.doc(category='grant_letter');before=self.c.total_changes
  p=case_projection(self.c,1,read_text=lambda row:'IMMI Grant Notification\nSent On 6 September 2026\nDate of Grant 5 September 2026')
  self.assertEqual('2026-09-05',p['grant_date']);self.assertEqual('2026-09-06',p['processing']['fields']['grant_sent_on']['value'])
  self.assertEqual(before,self.c.total_changes)
 def test_delete_control_has_no_archive_label(self):
  from visa_document_processing_ui import document_edit_html
  html=document_edit_html(1,'csrf',dict(id=3,archived_at=None))
  self.assertIn('Delete',html);self.assertNotIn('Archive /',html)
 def test_extraction_cannot_bypass_single_active_grant(self):
  first=self.doc(category='grant_letter');second=self.doc(name='unclassified.pdf',data=b'scan')
  with self.assertRaisesRegex(ValueError,'already has this document'):
   documents.save_extraction(self.c,1,second['id'],text='Visa Grant Notification',**self.actor)
 def test_staff_grant_date_requires_active_grant_and_preserves_history(self):
  from visa_document_lifecycle import case_projection
  self.c.execute("UPDATE visa_cases SET student_id='S1',application_cycle_id=1 WHERE id=1")
  self.c.execute("INSERT INTO visas(id,student_id,visa_grant_date) VALUES(1,'S1','2026-09-03')")
  self.c.execute("INSERT INTO visa_decision_events(id,student_id,visa_case_id,application_cycle_id,visa_record_id,decision_type,decision_date) VALUES(1,'S1',1,1,1,'grant','2026-09-03')")
  d=self.doc(category='grant_letter');before=self.c.total_changes
  self.assertEqual('2026-09-03',case_projection(self.c,1,read_text=lambda d:'')['grant_date'])
  self.assertEqual(before,self.c.total_changes)
  documents.set_document_archived(self.c,1,d['id'],archived=True,**self.actor)
  self.assertEqual('',case_projection(self.c,1)['grant_date'])
  self.assertEqual('2026-09-03',self.c.execute('SELECT visa_grant_date FROM visas WHERE id=1').fetchone()[0])
  documents._audit(self.c,1,None,'grant_date_corrected',{}, {'value':''},1,STAMP)
  self.assertEqual('',case_projection(self.c,1)['grant_date'])
 def test_inactive_source_cannot_retain_auto_date(self):
  from visa_document_lifecycle import case_projection
  self.c.execute("UPDATE visa_cases SET student_id='S1' WHERE id=1")
  self.c.execute("INSERT INTO student_documents VALUES(1,'S1','/synthetic/a.txt','a.txt',1,'VisaGrant',?,1,'active')",(STAMP,))
  d=documents.register_document(self.c,1,student_document_id=1,file_path='/synthetic/a.txt',original_name='a.txt',version=1,sha256='a'*64,category='grant_letter',**self.actor)
  dates.save_processing_extraction(self.c,1,d['id'],'IMMI Grant Notification\nSent On 5 September 2026',**self.actor)
  self.assertEqual('2026-09-05',case_projection(self.c,1)['processing']['fields']['grant_sent_on']['value'])
  self.c.execute('UPDATE student_documents SET is_active=0 WHERE id=1')
  p=case_projection(self.c,1)
  self.assertEqual('',p['processing']['fields']['grant_sent_on']['value']);self.assertEqual('Pending',p['result_status'])
 def test_no_digest_does_not_match_unhashed_legacy_link(self):
  from visa_document_lifecycle import check_upload
  self.c.execute("CREATE TABLE visa_case_document_links(visa_case_id INTEGER,student_document_id INTEGER,status TEXT)")
  self.c.execute("UPDATE visa_cases SET student_id='S1' WHERE id=1")
  self.c.execute("INSERT INTO student_documents VALUES(1,'S1','/synthetic/legacy.txt','legacy.txt',1,'Other',?,1,'active')",(STAMP,))
  self.c.execute("INSERT INTO visa_case_document_links VALUES(1,1,'Linked')")
  check_upload(self.c,1,None,'grant_letter')
 def test_other_deleted_proposal_cannot_erase_manual_canonical_date(self):
  from visa_document_lifecycle import case_projection
  self.c.execute("UPDATE visa_cases SET student_id='S1',application_cycle_id=1 WHERE id=1")
  self.c.execute("INSERT INTO visas(id,student_id,visa_grant_date) VALUES(1,'S1','2026-09-03')")
  self.c.execute("INSERT INTO visa_decision_events(id,student_id,visa_case_id,application_cycle_id,visa_record_id,student_document_id,decision_type,decision_date) VALUES(1,'S1',1,1,1,999,'grant','2026-09-03')")
  d=self.doc(category='grant_letter')
  documents.save_extraction(self.c,1,d['id'],text='IMMI Grant Notification\nDate of Grant: 3 September 2026',**self.actor)
  documents.set_document_archived(self.c,1,d['id'],archived=True,**self.actor)
  self.assertEqual('',case_projection(self.c,1)['grant_date'])
  self.assertEqual('2026-09-03',self.c.execute('SELECT visa_grant_date FROM visas WHERE id=1').fetchone()[0])
 def test_explicit_manual_other_overrides_retained_grant_proposal(self):
  from visa_document_lifecycle import case_projection
  from visa_document_metadata import edit_document_metadata
  self.c.executescript((Path(__file__).parents[1]/'migrations/20260909_037_visa_document_metadata.sql').read_text())
  d=self.doc(category='grant_letter');documents.save_extraction(self.c,1,d['id'],text='IMMI Grant Notification\nDate of Grant: 3 September 2026',**self.actor)
  edit_document_metadata(self.c,1,d['id'],category='other',**self.actor)
  p=case_projection(self.c,1,read_text=lambda d:'IMMI Grant Notification\nDate of Grant: 3 September 2026')
  self.assertEqual(('Pending','未开始',''),tuple(p[k] for k in ['result_status','workflow_status','grant_date']))
  self.doc(name='replacement.txt',data=b'realgrant',category='grant_letter')
 def test_reclassification_cannot_create_second_active_grant(self):
  from visa_document_metadata import classify_document
  self.c.executescript((Path(__file__).parents[1]/'migrations/20260909_037_visa_document_metadata.sql').read_text())
  self.doc(name='first.txt',category='grant_letter')
  second=self.doc(name='unknown.txt',data=b'second')
  self.c.execute("UPDATE visa_case_documents SET original_name='IMMI Grant Notification.txt' WHERE id=?",(second['id'],))
  with self.assertRaisesRegex(ValueError,'already has this document'):classify_document(self.c,1,second['id'],**self.actor)
 def test_deleting_unregistered_historical_duplicate_bypasses_upload_eligibility(self):
  from visa_document_lifecycle import active_documents
  self.c.execute("UPDATE visa_cases SET student_id='S1' WHERE id=1")
  self.doc(name='registered.txt',data=b'historical duplicate',category='grant_letter')
  self.c.execute("INSERT INTO student_documents VALUES(1,'S1','/synthetic/legacy.txt','legacy.txt',1,'VisaGrantLetter',?,1,'active')",(STAMP,))
  deleted=documents.register_document(self.c,1,student_document_id=1,file_path='/synthetic/legacy.txt',original_name='legacy.txt',version=1,sha256=hashlib.sha256(b'historical duplicate').hexdigest(),for_deletion=True,**self.actor)
  self.assertTrue(deleted['archived_at']);self.assertEqual(1,len(active_documents(self.c,1)))
  self.assertEqual(['document_registered','document_archived'],[r[0] for r in self.c.execute('select action from visa_document_audit where document_id=?',(deleted['id'],))])
 def test_refusal_is_not_grant_or_ack_workflow_authority(self):
  from visa_document_lifecycle import case_projection
  d=self.doc(category='refusal_letter')
  p=case_projection(self.c,1);self.assertEqual(('Pending','未开始'),tuple(p[k] for k in ['result_status','workflow_status']))
  documents.set_document_archived(self.c,1,d['id'],archived=True,**self.actor)
  self.assertEqual('Pending',case_projection(self.c,1)['result_status'])
