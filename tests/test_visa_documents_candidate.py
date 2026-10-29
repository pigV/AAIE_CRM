"""Behavior tests: immutable evidence, explicit confirmation, and case isolation."""
import importlib
import importlib.util
import json
import sqlite3
import unittest

STAMP = '2026-09-07T12:00:00+10:00'

SCHEMA = '''
CREATE TABLE users(id INTEGER PRIMARY KEY);
INSERT INTO users VALUES(1);
CREATE TABLE students(student_id TEXT PRIMARY KEY);
INSERT INTO students VALUES('S1');
CREATE TABLE visa_cases(id INTEGER PRIMARY KEY, student_id TEXT, application_cycle_id INTEGER,
 status TEXT DEFAULT 'Draft',decision_status TEXT DEFAULT 'Pending',result_status TEXT DEFAULT 'Pending',
 result_date TEXT,grant_date TEXT,refusal_date TEXT,expiry_date TEXT,updated_at TEXT,archived_at TEXT);
INSERT INTO visa_cases(id) VALUES(1),(2);
CREATE TABLE student_documents(id INTEGER PRIMARY KEY, student_id TEXT, file_path TEXT, original_name TEXT,
 version INTEGER,document_type TEXT,uploaded_at TEXT,is_active INTEGER,document_status TEXT);
CREATE TABLE visas(id INTEGER PRIMARY KEY,student_id TEXT,file_path TEXT,visa_type TEXT,visa_sector TEXT,
 visa_grant_date TEXT,visa_start_date TEXT,visa_expiry_date TEXT,visa_conditions TEXT,visa_status TEXT,created_at TEXT);
CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,cycle_uid TEXT,student_id TEXT,parent_form_token TEXT,
 source_student_case_id INTEGER,cycle_type TEXT,sequence_no INTEGER,status TEXT,outcome TEXT,opened_at TEXT,
 closed_at TEXT,created_by_user_id INTEGER,created_by_name TEXT,created_at TEXT,updated_by_user_id INTEGER,
 updated_by_name TEXT,updated_at TEXT);
CREATE TABLE visa_decision_events(id INTEGER PRIMARY KEY,application_cycle_id INTEGER UNIQUE,student_id TEXT,
 visa_case_id INTEGER,student_document_id INTEGER,visa_record_id INTEGER,decision_type TEXT,decision_date TEXT,
 confirmed_at TEXT,evidence_uploaded_at TEXT,decision_source TEXT,idempotency_key TEXT,recorded_by_user_id INTEGER,
 recorded_by_name TEXT,created_at TEXT);
CREATE TABLE visa_case_documents(id INTEGER PRIMARY KEY,visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id),
 student_document_id INTEGER REFERENCES student_documents(id),file_path TEXT NOT NULL,original_name TEXT NOT NULL,
 version INTEGER NOT NULL,sha256 TEXT NOT NULL,created_by INTEGER NOT NULL REFERENCES users(id),created_at TEXT NOT NULL,
 archived_at TEXT,archived_by INTEGER REFERENCES users(id),UNIQUE(visa_case_id,student_document_id));
CREATE TABLE visa_immi_submissions(id INTEGER PRIMARY KEY,visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id),
 document_id INTEGER NOT NULL REFERENCES visa_case_documents(id),file_path TEXT NOT NULL,version INTEGER NOT NULL,
 sha256 TEXT NOT NULL,status TEXT NOT NULL,submitted_by INTEGER NOT NULL REFERENCES users(id),submitted_at TEXT NOT NULL,
 supersedes_id INTEGER REFERENCES visa_immi_submissions(id),UNIQUE(visa_case_id,document_id));
CREATE TABLE visa_letter_extractions(id INTEGER PRIMARY KEY,visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id),
 document_id INTEGER NOT NULL REFERENCES visa_case_documents(id),proposed_json TEXT NOT NULL,extraction_method TEXT NOT NULL,
 created_by INTEGER NOT NULL REFERENCES users(id),created_at TEXT NOT NULL);
CREATE TABLE visa_letter_reviews(id INTEGER PRIMARY KEY,extraction_id INTEGER NOT NULL REFERENCES visa_letter_extractions(id),
 visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id),confirmed_json TEXT NOT NULL,confirmed_by INTEGER NOT NULL REFERENCES users(id),confirmed_at TEXT NOT NULL);
CREATE TABLE visa_case_confirmed_decisions(id INTEGER PRIMARY KEY,visa_case_id INTEGER NOT NULL UNIQUE REFERENCES visa_cases(id),
 extraction_id INTEGER NOT NULL REFERENCES visa_letter_extractions(id),decision_type TEXT NOT NULL,decision_date TEXT NOT NULL,
 confirmed_json TEXT NOT NULL,confirmed_by INTEGER NOT NULL REFERENCES users(id),confirmed_at TEXT NOT NULL,
 canonical_event_id INTEGER REFERENCES visa_decision_events(id));
CREATE TABLE visa_document_audit(id INTEGER PRIMARY KEY,visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id),
 document_id INTEGER REFERENCES visa_case_documents(id),action TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,
 actor_user_id INTEGER NOT NULL REFERENCES users(id),occurred_at TEXT NOT NULL);
'''


class VisaDocumentsCandidateTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('visa_documents_candidate'),
                             'Missing case document / explicit decision service')
        self.service = importlib.import_module('visa_documents_candidate')
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.conn.executescript(SCHEMA)
        from pathlib import Path
        self.conn.executescript((Path(__file__).resolve().parents[1]/'migrations/20260914_039_visa_result_status_authority.sql').read_text())
        self.addCleanup(self.conn.close)
        self.actor = dict(actor_user_id=1, occurred_at=STAMP)

    def doc(self, case_id=1, version=1):
        return self.service.register_document(self.conn,case_id,file_path=f'/synthetic/case-{case_id}/v{version}.pdf',
            original_name='Grant.pdf',version=version,sha256=str(version)*64,**self.actor)

    def extract(self, case_id=1, text='Visa grant notification\nDate of grant: 5 September 2026\nLength of stay: Until 15 March 2029'):
        doc=self.doc(case_id)
        # This fixture models final staff classification, not a filename guess.
        kind=self.service.propose_letter(text)['document_kind']
        category={'grant':'grant_letter','refusal':'refusal_letter','medical':'health_examination','lodgement_receipt':'immi_acknowledgment'}.get(kind)
        if category:
            self.service._audit(self.conn,case_id,doc['id'],'document_category_selected',{}, {'category':category,'confirmed':True},1,STAMP)
        return self.service.save_extraction(self.conn,case_id,doc['id'],text=text,**self.actor)

    def test_confirmed_grant_upload_updates_result_without_inventing_student_decision(self):
        result=self.extract()
        self.assertEqual('grant',json.loads(result['proposed_json'])['decision_type'])
        self.assertEqual('Granted',self.conn.execute('SELECT result_status FROM visa_cases WHERE id=1').fetchone()[0])
        self.assertEqual('auto',self.conn.execute('SELECT authority FROM visa_result_status_current WHERE visa_case_id=1').fetchone()[0])
        self.assertEqual(0,self.conn.execute('SELECT COUNT(*) FROM visa_case_confirmed_decisions').fetchone()[0])
        self.assertEqual(1,self.conn.execute('SELECT COUNT(*) FROM students').fetchone()[0])

    def test_refusal_and_unknown_proposals_do_not_drive_grant_ack_status(self):
        self.extract(text='IMMI Visa Refusal Notification\nDate of refusal: 5 September 2026')
        self.assertEqual('Pending',self.conn.execute('SELECT result_status FROM visa_cases WHERE id=1').fetchone()[0])
        self.extract(case_id=2,text='General correspondence without a Visa outcome')
        self.assertEqual('Pending',self.conn.execute('SELECT result_status FROM visa_cases WHERE id=2').fetchone()[0])
        self.assertIsNone(self.conn.execute('SELECT 1 FROM visa_result_status_current WHERE visa_case_id=2').fetchone())

    def test_grant_extracts_official_date_and_raw_length_without_arbitrary_dates(self):
        value=self.service.propose_letter('Gmail received 7 September 2026\nVisa grant notification\nDate of grant\n5 September 2026\nLength of stay: 3 years')
        self.assertEqual('2026-09-05',value['decision_date'])
        self.assertEqual('3 years',value['length_of_stay'])
        self.assertEqual('',value['expiry_date'])
        unknown=self.service.propose_letter('Received 7 September 2026\nDOB 2 January 2000')
        self.assertEqual('',unknown['official_date'])

    def test_grant_absolute_length_sets_expiry_without_copying_decision_to_official_date(self):
        value=self.service.propose_letter(
            'IMMI Grant Notification\n'
            'Date of grant:\n10 September 2026\n'
            'Must not arrive after:\n15 March 2031\n'
            'Length of stay:\n15 March 2031'
        )
        self.assertEqual('grant',value['document_kind'])
        self.assertEqual('grant',value['decision_type'])
        self.assertEqual('2026-09-10',value['decision_date'])
        self.assertEqual('',value['official_date'])
        self.assertEqual('15 March 2031',value['length_of_stay'])
        self.assertEqual('2031-03-15',value['expiry_date'])
        self.assertEqual('',value['medical_completed_date'])

    def test_refusal_decision_date_does_not_become_official_document_date(self):
        value=self.service.propose_letter(
            'IMMI Visa Refusal Notification\nDate of refusal: 9 September 2026'
        )
        self.assertEqual('refusal',value['document_kind'])
        self.assertEqual('refusal',value['decision_type'])
        self.assertEqual('2026-09-09',value['decision_date'])
        self.assertEqual('',value['official_date'])

    def test_grant_duration_length_does_not_invent_expiry(self):
        for length in ('3 months','12 months','Until course completion'):
            with self.subTest(length=length):
                value=self.service.propose_letter(
                    'IMMI Grant Notification\nDate of grant: 10 September 2026\n'
                    f'Length of stay: {length}'
                )
                self.assertEqual(length,value['length_of_stay'])
                self.assertEqual('',value['expiry_date'])

    def test_medical_and_receipt_official_dates_require_label(self):
        for text,kind in [('Health examination request\nDate: 4 September 2026','medical'),('Application lodgement receipt\nDate of lodgement: 3 September 2026','lodgement_receipt')]:
            value=self.service.propose_letter(text)
            self.assertEqual(kind,value['document_kind'])
            self.assertEqual('2026-09-04' if kind=='medical' else '2026-09-03',value['official_date'])

    def test_supported_acknowledgement_wording_is_not_unknown(self):
        for title in ('Acknowledge Letter','Application Acknowledgement'):
            with self.subTest(title=title):
                self.assertEqual('lodgement_receipt',self.service.propose_letter(title+'\nDate: 3 September 2026')['document_kind'])

    def test_prepared_and_submitted_records_pin_exact_version_without_copy(self):
        old=self.doc()
        self.assertEqual([],self.service.submitted_documents(self.conn,1))
        submitted=self.service.mark_submitted(self.conn,1,old['id'],**self.actor)
        new=self.doc(version=2)
        rows=self.service.submitted_documents(self.conn,1)
        self.assertEqual([old['id']],[row['document_id'] for row in rows])
        self.assertEqual('/synthetic/case-1/v1.pdf',rows[0]['file_path'])
        self.assertEqual(2,self.conn.execute('SELECT COUNT(*) FROM visa_case_documents').fetchone()[0])
        same=self.service.mark_submitted(self.conn,1,old['id'],**self.actor)
        self.assertEqual(submitted['id'],same['id'])
        self.service.mark_submitted(self.conn,1,new['id'],supersedes_id=submitted['id'],**self.actor)
        self.assertEqual([new['id']],[row['document_id'] for row in self.service.submitted_documents(self.conn,1)])
        self.assertEqual('superseded',self.conn.execute('SELECT status FROM visa_immi_submissions WHERE id=?',(submitted['id'],)).fetchone()[0])
        self.assertEqual('1'*64,self.conn.execute('SELECT sha256 FROM visa_immi_submissions WHERE id=?',(submitted['id'],)).fetchone()[0])

    def test_archive_excludes_active_analysis_and_restore_keeps_submission_history(self):
        doc=self.doc()
        submission=self.service.mark_submitted(self.conn,1,doc['id'],**self.actor)
        self.service.set_document_archived(self.conn,1,doc['id'],archived=True,**self.actor)
        self.assertEqual([],self.service.submitted_documents(self.conn,1))
        self.assertEqual('/synthetic/case-1/v1.pdf',self.conn.execute('SELECT file_path FROM visa_case_documents').fetchone()[0])
        self.service.set_document_archived(self.conn,1,doc['id'],archived=False,**self.actor)
        self.assertEqual(submission['id'],self.service.submitted_documents(self.conn,1)[0]['id'])
        self.assertEqual(['document_archived','document_restored'],[r[0] for r in self.conn.execute("SELECT action FROM visa_document_audit WHERE action IN ('document_archived','document_restored') ORDER BY id")])

    def test_cross_case_actions_rejected_before_mutation(self):
        doc=self.doc()
        for action in [lambda:self.service.mark_submitted(self.conn,2,doc['id'],**self.actor),lambda:self.service.set_document_archived(self.conn,2,doc['id'],archived=True,**self.actor)]:
            with self.assertRaises(ValueError):action()
        self.assertEqual(0,self.conn.execute('SELECT COUNT(*) FROM visa_immi_submissions').fetchone()[0])

    def test_standalone_confirmation_does_not_invent_student_or_cycle(self):
        extraction=self.extract()
        values=json.loads(extraction['proposed_json'])
        result=self.service.confirm_letter(self.conn,1,extraction['id'],values=values,**self.actor)
        self.assertEqual('Granted',self.conn.execute('SELECT result_status FROM visa_cases WHERE id=1').fetchone()[0])
        self.assertEqual(0,self.conn.execute('SELECT COUNT(*) FROM application_cycles').fetchone()[0])
        self.assertEqual(0,self.conn.execute('SELECT COUNT(*) FROM visas').fetchone()[0])
        self.assertEqual(0,self.conn.execute('SELECT COUNT(*) FROM visa_decision_events').fetchone()[0])
        self.assertIsNone(result['canonical_event_id'])
        self.assertEqual(('2026-09-05','2029-03-15'),tuple(self.conn.execute('SELECT grant_date,expiry_date FROM visa_cases WHERE id=1').fetchone()))
        with self.assertRaises(ValueError):
            self.service.confirm_letter(self.conn,1,extraction['id'],values={**values,'decision_type':'refusal'},**self.actor)

    def test_medical_correction_retains_extraction_and_each_review(self):
        # This workflow now uses the deployed processing-date authority.
        from pathlib import Path
        migration_root = Path(__file__).resolve().parents[1] / 'migrations'
        for filename in ('20260909_037_visa_document_metadata.sql', '20260909_038_visa_processing_dates.sql'):
            self.conn.executescript((migration_root / filename).read_text())
        extraction=self.extract(text='Health examination request\nDate: 4 September 2026')
        values=json.loads(extraction['proposed_json'])
        values['medical_completed_date']='2026-09-06'
        self.service.confirm_letter(self.conn,1,extraction['id'],values=values,**self.actor)
        values['medical_completed_date']='2026-09-07'
        self.service.confirm_letter(self.conn,1,extraction['id'],values=values,**self.actor)
        self.assertEqual(2,self.conn.execute('SELECT COUNT(*) FROM visa_letter_reviews').fetchone()[0])
        self.assertEqual('',json.loads(self.conn.execute('SELECT proposed_json FROM visa_letter_extractions').fetchone()[0])['medical_completed_date'])
        self.assertEqual('Pending',self.conn.execute('SELECT result_status FROM visa_cases WHERE id=1').fetchone()[0])

    def student_cycle(self):
        self.conn.execute("UPDATE visa_cases SET student_id='S1',application_cycle_id=1 WHERE id=1")
        self.conn.execute("INSERT INTO application_cycles(id,cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,created_at,updated_at) VALUES(1,'C1','S1','initial',1,'active',?,?,?)",(STAMP,STAMP,STAMP))

    def test_student_confirm_uses_canonical_exact_cycle_business_date(self):
        self.student_cycle()
        extraction=self.extract()
        result=self.service.confirm_letter(self.conn,1,extraction['id'],values=json.loads(extraction['proposed_json']),**self.actor)
        row=self.conn.execute('SELECT application_cycle_id,decision_type,decision_date FROM visa_decision_events').fetchone()
        self.assertEqual((1,'grant','2026-09-05'),tuple(row))
        self.assertEqual('2029-03-15',self.conn.execute('SELECT expiry_date FROM visa_cases WHERE id=1').fetchone()[0])
        self.assertEqual('closed',self.conn.execute('SELECT status FROM application_cycles').fetchone()[0])
        self.assertEqual(result['canonical_event_id'],self.conn.execute('SELECT id FROM visa_decision_events').fetchone()[0])
        self.service.confirm_letter(self.conn,1,extraction['id'],values=json.loads(extraction['proposed_json']),**self.actor)
        self.assertEqual(1,self.conn.execute('SELECT COUNT(*) FROM visas').fetchone()[0])

    def test_confirmation_cannot_bypass_opposite_manual_result_authority(self):
        self.student_cycle()
        extraction=self.extract()
        from visa_result_status import record_manual_result
        record_manual_result(
            self.conn,1,'Refused',actor_user_id=1,occurred_at=STAMP,
            reason='Staff verified the effective result is refusal',
        )

        result=self.service.confirm_letter(
            self.conn,1,extraction['id'],values=json.loads(extraction['proposed_json']),**self.actor
        )

        self.assertIsNone(result.get('canonical_event_id'))
        self.assertEqual(0,self.conn.execute('SELECT COUNT(*) FROM visa_decision_events').fetchone()[0])
        self.assertEqual(0,self.conn.execute('SELECT COUNT(*) FROM visas').fetchone()[0])
        self.assertEqual('Refused',self.conn.execute('SELECT result_status FROM visa_cases WHERE id=1').fetchone()[0])
        self.assertIsNone(self.conn.execute('SELECT grant_date FROM visa_cases WHERE id=1').fetchone()[0])

    def test_refusal_uses_canonical_without_grant_and_missing_cycle_fails_closed(self):
        self.student_cycle()
        extraction=self.extract(text='Visa refusal notification\nDate of decision: 4 September 2026')
        # Explicit staff decision history remains separate from read-time workflow.
        from visa_result_status import record_manual_result
        record_manual_result(self.conn,1,'Refused',reason='Staff confirmed refusal',**self.actor)
        self.service.confirm_letter(self.conn,1,extraction['id'],values=json.loads(extraction['proposed_json']),**self.actor)
        self.assertEqual('refusal',self.conn.execute('SELECT decision_type FROM visa_decision_events').fetchone()[0])
        self.conn.execute("UPDATE visa_cases SET student_id='S1' WHERE id=2")
        second=self.extract(2)
        with self.assertRaises(ValueError):
            self.service.confirm_letter(self.conn,2,second['id'],values=json.loads(second['proposed_json']),**self.actor)
        self.assertEqual(0,self.conn.execute('SELECT COUNT(*) FROM visa_letter_reviews WHERE visa_case_id=2').fetchone()[0])

    def test_medical_dob_and_received_headers_never_become_letter_date(self):
        result=self.service.propose_letter('Health examination request\nDate of birth: 2 January 2000\nReceived: 7 September 2026\nDate: 4 September 2026')
        self.assertEqual('2026-09-04',result['official_date'])

    def test_duplicate_confirmation_must_not_silently_discard_changed_values(self):
        extraction=self.extract()
        values=json.loads(extraction['proposed_json'])
        self.service.confirm_letter(self.conn,1,extraction['id'],values=values,**self.actor)
        with self.assertRaises(ValueError):
            self.service.confirm_letter(self.conn,1,extraction['id'],values={**values,'expiry_date':'2030-01-01'},**self.actor)

    def test_failed_cross_student_canonical_confirmation_rolls_back_all_new_records(self):
        self.student_cycle()
        self.conn.execute("UPDATE application_cycles SET student_id='OTHER' WHERE id=1")
        extraction=self.extract()
        with self.assertRaises(Exception):
            self.service.confirm_letter(self.conn,1,extraction['id'],values=json.loads(extraction['proposed_json']),**self.actor)
        for table in ('visa_letter_reviews','visa_case_confirmed_decisions','visas','visa_decision_events'):
            self.assertEqual(0,self.conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0])

    def test_archive_prevents_confirmation_and_submission_even_when_extraction_exists(self):
        extraction=self.extract()
        self.service.set_document_archived(self.conn,1,extraction['document_id'],archived=True,**self.actor)
        with self.assertRaises(ValueError):
            self.service.confirm_letter(self.conn,1,extraction['id'],values=json.loads(extraction['proposed_json']),**self.actor)
        with self.assertRaises(ValueError):
            self.service.mark_submitted(self.conn,1,extraction['document_id'],**self.actor)

    def test_staff_identity_is_required_and_bad_digest_rejected(self):
        with self.assertRaises(ValueError):
            self.service.register_document(self.conn,1,file_path='/synthetic/a.pdf',original_name='a.pdf',version=1,sha256='z'*64,**self.actor)
        doc=self.doc()
        with self.assertRaises(ValueError):
            self.service.mark_submitted(self.conn,1,doc['id'],actor_user_id=999,occurred_at=STAMP)

    def test_student_source_must_match_case_identity_and_exact_version(self):
        self.conn.execute("UPDATE visa_cases SET student_id='S1' WHERE id=1")
        self.conn.execute("INSERT INTO student_documents VALUES(10,'OTHER','/synthetic/source.pdf','source.pdf',1,'VisaGrantLetter',?,1,'active')",(STAMP,))
        args=dict(student_document_id=10,file_path='/synthetic/source.pdf',original_name='source.pdf',version=1,sha256='a'*64,**self.actor)
        with self.assertRaises(ValueError):self.service.register_document(self.conn,1,**args)
        self.conn.execute("UPDATE student_documents SET student_id='S1' WHERE id=10")
        doc=self.service.register_document(self.conn,1,**args)
        self.assertEqual(10,doc['student_document_id'])
        with self.assertRaises(ValueError):self.service.register_document(self.conn,1,**{**args,'sha256':'b'*64})

if __name__=='__main__':unittest.main()
