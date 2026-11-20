import sqlite3
import unittest
from unittest.mock import patch
from tests import test_student_lifecycle_case_integration as fixture
from tests.student_lifecycle_test_support import activate_progression
from business_changes.commands import TransactionView
from business_changes.schema import install
from progression_case_service import ProgressionCaseService
from progression_case_service import ProgressionCaseIntegrityError
from transfer_case_service import TransferCaseService


class ActualServiceTransactionPortTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.StudentLifecycleCaseIntegrationTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.c=sqlite3.connect(self.f.database_path);self.c.row_factory=sqlite3.Row;self.addCleanup(self.c.close)
        self.c.execute('PRAGMA foreign_keys=ON');self.c.execute('BEGIN');install(self.c)
        self.c.execute("INSERT OR REPLACE INTO system_settings(key,value) VALUES('business_change_sync_v1','enabled')");self.c.commit()

    def test_transfer_creation_internal_commit_remains_inside_caller_rollback(self):
        before={t:self.c.execute('SELECT count(*) FROM '+t).fetchone()[0]
                for t in ('student_cases','application_cycles','business_change_events')}
        self.c.execute('BEGIN IMMEDIATE')
        self.f.service=TransferCaseService(self.f.database_path,caller_transaction=TransactionView(self.c,'bc_test_owner'))
        self.f.create_transfer_draft()
        self.assertTrue(self.c.in_transaction)
        self.assertGreater(self.c.execute('SELECT count(*) FROM student_cases').fetchone()[0],before['student_cases'])
        self.c.rollback()
        for table,count in before.items():self.assertEqual(count,self.c.execute('SELECT count(*) FROM '+table).fetchone()[0],table)

    def test_upload_coordinator_links_uncommitted_document_and_rolls_back_with_caller(self):
        from record_picker_and_uploader import ProgressionUploadContext
        from upload_review_confirmation_coordinator import coordinate_confirmed_upload, ConfirmedUpload
        draft=self.f.create_progression_draft();activate_progression(self.f.progression_service,draft)
        before=self.c.execute('SELECT count(*) FROM progression_document_links').fetchone()[0]
        self.c.execute('BEGIN IMMEDIATE')
        self.c.execute("INSERT INTO student_documents(id,student_id) VALUES(9201,'student-1')")
        result=coordinate_confirmed_upload(self.f.database_path,
            stored_form=ProgressionUploadContext(draft.case_id,'application_material').form_values(),
            confirmed_upload=ConfirmedUpload('Applications',9201),actor_user_id=1,
            actor_permissions={'progression_cases.materials'},
            flags={'read_enabled':True,'progression_write_enabled':True},
            caller_transaction=TransactionView(self.c,'bc_test_owner'))
        self.assertTrue(result.linked)
        self.assertTrue(self.c.in_transaction)
        self.assertEqual(before+1,self.c.execute('SELECT count(*) FROM progression_document_links').fetchone()[0])
        self.c.rollback()
        self.assertEqual(before,self.c.execute('SELECT count(*) FROM progression_document_links').fetchone()[0])
        self.assertIsNone(self.c.execute('SELECT id FROM student_documents WHERE id=9201').fetchone())

    def test_foreign_student_document_is_rejected_before_progression_link(self):
        draft=self.f.create_progression_draft();activate_progression(self.f.progression_service,draft)
        self.c.execute("INSERT INTO student_documents(id,student_id) VALUES(9202,'student-2')");self.c.commit()
        with self.assertRaises(ProgressionCaseIntegrityError):
            self.f.progression_service.link_progression_document(draft.case_id,document_role='application_material',
                student_document_id=9202,target_application_id=None,status='confirmed',notes='synthetic foreign document',
                actor_user_id=1,actor_permissions={'progression_cases.materials'},
                flags={'read_enabled':True,'progression_write_enabled':True})

    def test_multi_service_coordination_rolls_back_first_link_when_arrangement_fails(self):
        from record_picker_and_uploader import ProgressionUploadContext
        from upload_review_confirmation_coordinator import coordinate_confirmed_upload, ConfirmedUpload
        draft=self.f.create_progression_draft();activate_progression(self.f.progression_service,draft)
        before=self.c.execute('SELECT count(*) FROM progression_document_links').fetchone()[0]
        self.c.execute('BEGIN IMMEDIATE')
        self.c.execute("INSERT INTO student_documents(id,student_id) VALUES(9203,'student-1')")
        with patch.object(ProgressionCaseService,'set_progression_arrangement',side_effect=RuntimeError('synthetic second-service failure')):
            with self.assertRaises(RuntimeError):
                coordinate_confirmed_upload(self.f.database_path,
                    stored_form=ProgressionUploadContext(draft.case_id,'oshc').form_values(),
                    confirmed_upload=ConfirmedUpload('COE + OSHC',9203),actor_user_id=1,
                    actor_permissions={'progression_cases.materials','progression_cases.arrangements'},
                    flags={'read_enabled':True,'progression_write_enabled':True},
                    caller_transaction=TransactionView(self.c,'bc_test_owner'))
        self.c.commit()
        self.assertEqual(before,self.c.execute('SELECT count(*) FROM progression_document_links').fetchone()[0])
        self.assertIsNotNone(self.c.execute('SELECT id FROM student_documents WHERE id=9203').fetchone())

    def test_progression_material_internal_commit_and_event_share_caller_transaction(self):
        draft=self.f.create_progression_draft();activate_progression(self.f.progression_service,draft)
        before={t:self.c.execute('SELECT count(*) FROM '+t).fetchone()[0]
                for t in ('progression_case_materials','progression_case_events','business_change_events')}
        self.c.execute('BEGIN IMMEDIATE')
        service=ProgressionCaseService(self.f.database_path,caller_transaction=TransactionView(self.c,'bc_test_owner'))
        service.add_material(draft.case_id,actor_user_id=1,actor_permissions={'progression_cases.materials'},
            flags={'read_enabled':True,'progression_write_enabled':True},material_key='synthetic-borrowed',
            material_name='Synthetic borrowed material',is_required=True,student_document_id=None,upload_metadata={},notes=None)
        self.assertTrue(self.c.in_transaction)
        self.c.rollback()
        for table,count in before.items():self.assertEqual(count,self.c.execute('SELECT count(*) FROM '+table).fetchone()[0],table)
