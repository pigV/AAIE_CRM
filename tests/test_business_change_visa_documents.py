import unittest
from unittest.mock import patch
from tests import test_visa_status_evidence_authority as fixture
from business_changes.schema import install
from business_changes.contracts import Subject
from business_changes.versions import version_of
import visa_documents_candidate as docs
from visa_document_metadata import edit_document_metadata
from visa_result_status import record_manual_result, result_summary


class VisaDocumentSyncTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.VisaStatusEvidenceAuthorityTests();self.f.setUp();self.addCleanup(self.f.tearDown);self.c=self.f.c
        self.c.commit();self.c.execute('PRAGMA foreign_keys=ON');self.c.execute('BEGIN');install(self.c)
        self.c.execute('CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT)')
        self.c.execute("INSERT INTO system_settings VALUES('business_change_sync_v1','enabled')")

    def register(self):return docs.register_document(self.c,1,file_path='/synthetic/new.pdf',original_name='new.pdf',version=1,sha256='d'*64,**self.f.actor)
    def count(self):return self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0]

    def test_register_classify_archive_restore_publish_changes_without_confirming_result(self):
        d=self.register();self.assertEqual(2,self.count())
        self.assertEqual({'visa.document_registered','visa.document_classified'},
            {r[0] for r in self.c.execute('SELECT event_type FROM business_change_events')})
        edit_document_metadata(self.c,1,d['id'],category='grant_letter',**self.f.actor)
        self.assertEqual('Pending',self.f.projection()['result_status'])
        self.assertEqual('Granted',self.f.projection()['auto_detected_result'])
        docs.set_document_archived(self.c,1,d['id'],archived=True,**self.f.actor)
        self.assertEqual('Pending',self.f.projection()['result_status'])
        docs.set_document_archived(self.c,1,d['id'],archived=True,**self.f.actor)
        self.assertEqual(4,self.count())
        docs.set_document_archived(self.c,1,d['id'],archived=False,**self.f.actor)
        self.assertEqual('Pending',self.f.projection()['result_status'])
        self.assertEqual(5,self.count());self.assertEqual(5,version_of(self.c,Subject('student','S1')))

    def test_document_publication_failure_rolls_back_source_and_audit(self):
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.register()
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM visa_case_documents').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM visa_document_audit').fetchone()[0])

    def test_audited_manual_refusal_is_same_in_result_summary_and_material_projection(self):
        record_manual_result(self.c,1,'Refused',reason='Synthetic official refusal reviewed',**self.f.actor)
        self.assertEqual('Refused',result_summary(self.c,1)['effective_result'])
        self.assertEqual('Refused',self.f.projection()['result_status'])
        self.assertEqual(1,self.count())

    def test_processing_date_and_grant_date_corrections_publish_case_revision(self):
        from datetime import date
        from visa_processing_dates import correct_processing_dates
        from visa_documents_candidate import correct_grant_date
        correct_processing_dates(self.c,1,{'medical_completed_date':'2026-11-18'},today=date(2026,11,19),**self.f.actor)
        self.assertEqual(1,self.count())
        correct_grant_date(self.c,1,'2026-11-17',**self.f.actor)
        self.assertEqual(2,self.count())
        self.assertEqual('Pending',self.f.projection()['result_status'])
