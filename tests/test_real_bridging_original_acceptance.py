"""Native bridging upload acceptance using a historical-hash-matched original."""
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace
import hashlib
import os
import io
import json
import tempfile
import unittest
from pypdf import PdfReader
from tests import test_visa_status_evidence_authority as evidence_fixture
from visa_workspace_candidate import VisaWorkspaceCandidateMixin
from visa_document_metadata import edit_document_metadata
from visa_document_lifecycle import case_projection, student_projection
from business_changes.schema import install
from business_changes.versions import version_of
from business_changes.contracts import Subject
from dashboard_visa_metrics import load_student_visa_grant_counts

ROOT=Path(__file__).resolve().parents[1]
ORIGINAL_DIR=Path(os.environ.get('AAIE_CRM_BRIDGING_ORIGINAL_DIR',str(ROOT/'STAGING_DATA/tmp/continued-visa-sources')))
@unittest.skipUnless(any(ORIGINAL_DIR.glob('*.pdf')), 'Private originals not supplied: AAIE_CRM_BRIDGING_ORIGINAL_DIR')
class OriginalBridgingAcceptance(unittest.TestCase):
    def test_real_bridging_native_upload_save_readback_and_no_grant(self):
        summaries=[]
        paths=sorted(ORIGINAL_DIR.glob('*.pdf'))
        self.assertEqual(1,len(paths),'Authorized historical-hash-matched bridging original required')
        for source in paths:
            with self.subTest(source=source.name):
                fixture=evidence_fixture.VisaStatusEvidenceAuthorityTests();fixture.setUp()
                try:
                    conn=fixture.c
                    conn.commit();conn.execute('PRAGMA foreign_keys=ON');conn.execute('BEGIN');install(conn)
                    conn.executescript("CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT); INSERT INTO system_settings VALUES('business_change_sync_v1','enabled'); ALTER TABLE students ADD COLUMN folder_path TEXT;")
                    stamp='2026-11-20T12:00:00+11:00'
                    conn.execute("INSERT INTO application_cycles(id,cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,created_at,updated_at) VALUES(7,'SYNTHETIC-C7','S1','initial',1,'active',?,?,?)",(stamp,)*3)
                    conn.execute('UPDATE visa_cases SET application_cycle_id=7 WHERE id=1')
                    folder=Path(tempfile.mkdtemp(prefix='visa-original-upload-'))
                    conn.execute('UPDATE students SET folder_path=? WHERE student_id=?',(str(folder),'S1'));conn.commit()
                    class DB:
                        def one(self,sql,args=()):return conn.execute(sql,args).fetchone()
                        def all(self,sql,args=()):return conn.execute(sql,args).fetchall()
                        @contextmanager
                        def transaction(self):
                            if not conn.in_transaction:conn.execute('BEGIN')
                            try:yield conn;conn.commit()
                            except BaseException:conn.rollback();raise
                    class Handler(VisaWorkspaceCandidateMixin):
                        csrf='fixture';user={'id':1};form={'action':'upload'}
                        files=[SimpleNamespace(field_name='document',filename=source.name,data=source.read_bytes())]
                        def _visa_allowed(self):return True
                        def business_changes_enabled(self):return False
                        def _visa_runtime(self):return runtime
                        def redirect(self,url):return url
                        def error_page(self,message,code):raise AssertionError((message,code))
                        def send_response(self,code):self.response_code=code
                        def send_header(self,*args):pass
                        def end_headers(self):pass
                    runtime=dict(db=DB(),form_text_value=lambda form,key:form.get(key,''),now_iso=lambda:stamp,
                        PdfReader=PdfReader,io=io,nas=SimpleNamespace(student_folder=lambda student:folder),
                        refresh_workflow_completion_metrics=lambda *args,**kwargs:None,
                        advance_workflow_stage_to=lambda *args:None,set_workflow_milestone=lambda *args:None)
                    Handler().visa_case_workspace('POST',1)
                    document=dict(conn.execute('SELECT * FROM visa_case_documents WHERE visa_case_id=1').fetchone())
                    saved=Path(document['file_path']).read_bytes()
                    self.assertEqual(source.read_bytes(),saved)
                    self.assertEqual(hashlib.sha256(saved).hexdigest(),document['sha256'])
                    download=Handler();download.wfile=io.BytesIO()
                    download.visa_case_document_file('GET',1,document['id'])
                    self.assertEqual(200,download.response_code)
                    self.assertEqual(source.read_bytes(),download.wfile.getvalue())
                    extraction=dict(conn.execute('SELECT * FROM visa_letter_extractions WHERE document_id=?',(document['id'],)).fetchone())
                    proposal=json.loads(extraction['proposed_json'])
                    self.assertEqual('Pending',case_projection(conn,1)['result_status'])
                    self.assertEqual('bridging_visa',proposal['document_kind'])
                    self.assertEqual('',proposal['decision_type'])
                    self.assertEqual('',proposal['decision_date'])
                    conn.execute('BEGIN')
                    edit_document_metadata(conn,1,document['id'],category='bridging_visa',actor_user_id=1,occurred_at=stamp)
                    conn.commit()
                    from visa_processing_dates import processing_summary, parse_processing_dates
                    text='\n'.join(page.extract_text() or '' for page in PdfReader(source).pages)
                    self.assertEqual('not_detected',parse_processing_dates(text)['fields']['grant_sent_on']['status'])
                    projection=case_projection(conn,1)
                    self.assertEqual('Pending',projection['result_status'])
                    self.assertFalse(projection['grant_date'])
                    self.assertEqual('Pending',student_projection(conn,'S1')['result_status'])
                    self.assertFalse(processing_summary(conn,1)['fields']['grant_sent_on']['value'])
                    self.assertEqual(0,conn.execute('SELECT COUNT(*) FROM visa_case_confirmed_decisions').fetchone()[0])
                    self.assertEqual(0,conn.execute('SELECT COUNT(*) FROM visa_decision_events').fetchone()[0])
                    counts=load_student_visa_grant_counts(conn,today=date(2026,11,20))
                    self.assertEqual((0,0),counts)
                    versions={key:version_of(conn,Subject(kind,identifier)) for key,kind,identifier in (
                        ('student','student','S1'),('cycle','application_cycle','7'),('workflow','dashboard','workflow'),('overview','dashboard','overview'),('visa','dashboard','visa_cases'))}
                    self.assertTrue(all(value>0 for value in versions.values()),versions)
                    self.assertEqual(1,len(set(versions.values())))
                    summaries.append(dict(filename=source.name,sha256=document['sha256'],byte_readback=True,
                        proposal=proposal,formal_result=projection['result_status'],grant_sent_on='',
                        formal_decisions=0,home_kpi_counts=counts,versions=versions))
                finally:fixture.tearDown()
        (ROOT/'STAGING_DATA/tmp/real-bridging-native-result.json').write_text(json.dumps(summaries,ensure_ascii=False,indent=2)+'\n')
