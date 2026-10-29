import sqlite3, unittest
from pathlib import Path
import visa_processing_dates as s

class ProcessingDatesTests(unittest.TestCase):
 def setUp(self):
  self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
  self.c.executescript('CREATE TABLE users(id INTEGER PRIMARY KEY); INSERT INTO users VALUES(1); CREATE TABLE visa_cases(id INTEGER PRIMARY KEY); INSERT INTO visa_cases VALUES(1); CREATE TABLE visa_case_documents(id INTEGER PRIMARY KEY,visa_case_id INTEGER,archived_at TEXT); INSERT INTO visa_case_documents VALUES(1,1,NULL),(2,1,NULL);')
  self.c.executescript((Path(__file__).resolve().parents[1]/'migrations/20260909_038_visa_processing_dates.sql').read_text())
 def tearDown(self):self.c.close()
 def save(self,doc,text):return s.save_processing_extraction(self.c,1,doc,text,actor_user_id=1,occurred_at='2026-09-08T12:00:00',today='2026-09-08')
 def summary(self):return s.processing_summary(self.c,1,today='2026-09-08')
 def correct(self,v,**kw):return s.correct_processing_dates(self.c,1,v,actor_user_id=1,occurred_at='2026-09-08T12:00:00',today='2026-09-08',**kw)
 def test_sent_on_metrics_and_grant_independent(self):
  self.save(1,'Acknowledgement of application\nsent on 1 September 2026')
  p=self.save(2,'IMMI Grant Notification\nsent on 8 September 2026\nDate of Grant 7 September 2026\nLength of stay 3 years')
  self.assertEqual(7,self.summary()['total_processing_days']);self.assertNotIn('decision_date',p)
 def test_health_rejects_letter_generated_and_appointment_dates(self):
  for text in ['sent on 2 September 2026\nGenerated 1 September 2026','Generated 1 September 2026','Appointment: 2 September 2026','sent on 31 February 2026']:
   p=s.parse_processing_dates('Health Examination\n'+text,today='2026-09-08');self.assertEqual('',p['fields']['health_exam_date']['value']);self.assertEqual('not_detected',p['fields']['health_exam_date']['status'])
 def test_health_completion_dates_validate_and_fail_closed(self):
  for text,value,status in [('Examination completed on: 2 September 2026','2026-09-02','detected'),('Examination completed: 31 February 2026','','invalid'),('Examination completed: 9 September 2026','','invalid'),('Examination completed: 1 September 2026 or 2 September 2026','','ambiguous')]:
   p=s.parse_processing_dates('Health Examination\n'+text,today='2026-09-08');self.assertEqual(value,p['fields']['health_exam_date']['value']);self.assertEqual(status,p['fields']['health_exam_date']['status'])
 def test_multiple_dates_fail_closed(self):
  p=s.parse_processing_dates('IMMI Grant Notification\nsent on 1 September 2026\nsent on 2 September 2026',today='2026-09-08');self.assertEqual('ambiguous',p['fields']['grant_sent_on']['status'])
 def test_manual_medical_and_audit_override(self):
  self.correct({'medical_completed_date':'2026-09-02'});self.correct({'medical_completed_date':'2026-09-03'})
  self.save(1,'Health Examination\nsent on 1 September 2026');self.save(2,'IMMI Grant Notification\nsent on 8 September 2026')
  self.assertEqual(5,self.summary()['medical_completed_to_grant_days'])
  self.assertIn('2026-09-02',self.c.execute('SELECT before_json FROM visa_processing_date_revisions ORDER BY id DESC LIMIT 1').fetchone()[0])
 def test_reextract_manual_and_archived(self):
  self.save(1,'IMMI Grant Notification\nsent on 8 September 2026');self.correct({'grant_sent_on':'2026-09-07'},source_document_ids={'grant_sent_on':1})
  self.save(1,'IMMI Grant Notification\nsent on 6 September 2026');self.assertEqual('2026-09-07',self.summary()['fields']['grant_sent_on']['value'])
  self.c.execute("UPDATE visa_case_documents SET archived_at='now' WHERE id=1");self.assertEqual('2026-09-07',self.summary()['fields']['grant_sent_on']['value'])
 def test_competing_documents_and_manual_selection(self):
  for d in (1,2):self.save(d,'IMMI Grant Notification\nsent on 8 September 2026')
  self.assertEqual('ambiguous',self.summary()['fields']['grant_sent_on']['status'])
  self.correct({'grant_sent_on':'2026-09-08'},source_document_ids={'grant_sent_on':2});self.assertEqual('2026-09-08',self.summary()['fields']['grant_sent_on']['value'])
 def test_reverse_rejected_same_day_zero_missing_pending(self):
  self.assertIsNone(self.summary()['total_processing_days'])
  with self.assertRaises(ValueError):self.correct({'acknowledge_sent_on':'2026-09-08','grant_sent_on':'2026-09-07'})
  self.correct({'acknowledge_sent_on':'2026-09-08','grant_sent_on':'2026-09-08'});self.assertEqual(0,self.summary()['total_processing_days'])
 def test_future_and_foreign_source_rejected(self):
  for v,kw in [({'medical_completed_date':'2026-09-09'},{}),({'grant_sent_on':'2026-09-08'},{'source_document_ids':{'grant_sent_on':99}})]:
   with self.assertRaises(ValueError):self.correct(v,**kw)

 def test_two_dates_under_one_label_are_ambiguous(self):
  p=s.parse_processing_dates('IMMI Grant Notification\nsent on 1 September 2026 or 2 September 2026',today='2026-09-08')
  self.assertEqual('ambiguous',p['fields']['grant_sent_on']['status'])
 def test_evidence_immutable_and_previous_current_audited(self):
  self.save(1,'Acknowledgement of application\nsent on 1 September 2026')
  self.correct({'acknowledge_sent_on':'2026-09-02'})
  self.correct({'acknowledge_sent_on':'2026-09-03'})
  self.assertIn('2026-09-02',self.c.execute('SELECT before_json FROM visa_processing_date_revisions ORDER BY id DESC LIMIT 1').fetchone()[0])
  for table in ('visa_processing_date_extractions','visa_processing_date_revisions'):
   with self.assertRaises(sqlite3.IntegrityError):self.c.execute('DELETE FROM '+table)
 def test_reverse_auto_proposal_is_pending_not_zero(self):
  self.save(1,'Acknowledgement of application\nsent on 8 September 2026')
  self.save(2,'IMMI Grant Notification\nsent on 7 September 2026')
  self.assertIsNone(self.summary()['total_processing_days']);self.assertTrue(self.summary()['validation'])
 def test_manual_blank_remains_and_medical_never_inferred(self):
  self.save(1,'Health Examination\nsent on 8 September 2026')
  self.assertEqual('',self.summary()['fields']['medical_completed_date']['value'])
  self.correct({'health_exam_date':''});self.save(1,'Health Examination\nExamination completed: 7 September 2026')
  self.assertEqual('detected',self.summary()['fields']['health_exam_date']['status']);self.assertEqual('2026-09-07',self.summary()['fields']['health_exam_date']['value'])
 def test_duplicate_same_date_is_valid_and_next_line(self):
  p=s.parse_processing_dates('Acknowledge Letter\nSent On:\n1 September 2026\nsent on 1 September 2026',today='2026-09-08')
  self.assertEqual('2026-09-01',p['fields']['acknowledge_sent_on']['value'])
 def test_blank_sent_on_does_not_consume_generated_next_line(self):
  p=s.parse_processing_dates('Health Examination\nSent on:\nGenerated: 1 September 2026',today='2026-09-08')
  self.assertEqual('',p['fields']['health_exam_date']['value'])
  self.assertEqual('not_detected',p['fields']['health_exam_date']['status'])
  self.assertEqual('2026-09-01',p['candidates']['generated'][0]['value'])
 def test_same_line_labels_are_scoped(self):
  p=s.parse_processing_dates('Health Examination\nSent on: 2 September 2026 Generated: 1 September 2026',today='2026-09-08')
  self.assertEqual('',p['fields']['health_exam_date']['value'])
 def test_incidental_health_insurance_does_not_change_grant_kind(self):
  p=s.parse_processing_dates('IMMI Grant Notification\nSent on: 8 September 2026\nDate of Grant: 7 September 2026\nMaintain health insurance during your stay',today='2026-09-08')
  self.assertEqual('2026-09-08',p['fields']['grant_sent_on']['value'])
  self.assertEqual('not_detected',p['fields']['health_exam_date']['status'])
 def test_next_line_other_date_label_is_not_sent_on(self):
  p=s.parse_processing_dates('IMMI Grant Notification\nSent on:\nDate of Grant: 7 September 2026',today='2026-09-08')
  self.assertEqual('invalid',p['fields']['grant_sent_on']['status'])
 def test_genuinely_competing_document_titles_remain_ambiguous(self):
  p=s.parse_processing_dates('IMMI Grant Notification\nHealth Examination Request\nSent on 8 September 2026',today='2026-09-08')
  self.assertEqual('ambiguous',p['fields']['grant_sent_on']['status'])
 def test_wrapped_date_line_stops_at_other_label(self):
  p=s.parse_processing_dates('Health Examination\nSent on:\n2 September 2026 Generated: 1 September 2026',today='2026-09-08')
  self.assertEqual('',p['fields']['health_exam_date']['value'])
 def test_existing_official_parser_contract_unchanged(self):
  from visa_documents_candidate import propose_letter
  text='IMMI Grant Notification\nSent on: 8 September 2026\nDate of Grant: 7 September 2026\nLength of stay: Until 15 March 2029'
  original=propose_letter(text)
  self.assertEqual('2026-09-07',original['decision_date'])
  self.assertEqual('Until 15 March 2029',original['length_of_stay'])
  self.assertEqual('2029-03-15',original['expiry_date'])
  self.assertNotIn('grant_sent_on',original)
  self.assertEqual('2026-09-08',s.parse_processing_dates(text,today='2026-09-08')['fields']['grant_sent_on']['value'])
 def test_failed_correction_appends_no_revision(self):
  with self.assertRaises(ValueError):self.correct({'medical_completed_date':'2026-09-08','grant_sent_on':'2026-09-07'})
  self.assertEqual(0,self.c.execute('SELECT COUNT(*) FROM visa_processing_date_revisions').fetchone()[0])
 def test_archived_source_restore_reenables_selected_date(self):
  self.save(1,'Acknowledge Letter\nSent on: 1 September 2026')
  self.correct({'acknowledge_sent_on':'2026-09-02'},source_document_ids={'acknowledge_sent_on':1})
  self.c.execute("UPDATE visa_case_documents SET archived_at='archived' WHERE id=1")
  self.assertEqual('manual',self.summary()['fields']['acknowledge_sent_on']['status']);self.assertEqual('2026-09-02',self.summary()['fields']['acknowledge_sent_on']['value'])
  self.c.execute('UPDATE visa_case_documents SET archived_at=NULL WHERE id=1')
  self.assertEqual('2026-09-02',self.summary()['fields']['acknowledge_sent_on']['value'])
