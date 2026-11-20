import unittest
from unittest.mock import patch

import app
from tests import test_business_change_school_chain as school_fixture


class ExistingDocumentLinkTests(unittest.TestCase):
    def setUp(self):
        self.f=school_fixture.SchoolMaterialChainTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db;self.h=self.f.h
        self.cid=self.db.execute('''INSERT INTO visa_cases(student_id,application_cycle_id,visa_case_name,visa_type,applicant_name,application_type,status,result_status,created_by,created_at,updated_at)
            VALUES(?,?,'SYNTHETIC LINK','500 Student Visa','SYNTHETIC ONLY','New Application','Information Collection','Pending',1,?,?)''',
            (self.f.sid,self.f.cycle,self.f.stamp,self.f.stamp))
        self.doc=self.f.review('Applications','StudentPassport')
        self.h.form={'student_document_id':str(self.doc['id']),'document_role':'supporting_evidence','notes':'SYNTHETIC ONLY'}
        self.h.json_response=lambda value,status=200:(status,value)
        preflight=self.h.visa_document_linking_validate_existing_document(self.cid,self.h.form,'synthetic preflight')
        self.assertTrue(preflight['ok'],preflight)

    def test_link_audit_and_event_roll_back_together_and_retry_creates_one_link(self):
        before=self.db.one('SELECT count(*) FROM audit_logs')[0]
        with patch('business_changes.commands._append',side_effect=RuntimeError('link publication failure')):
            with self.assertRaisesRegex(RuntimeError,'link publication failure'):
                self.h.visa_document_linking_confirm('POST',self.cid)
        self.assertEqual(0,self.db.one('SELECT count(*) FROM visa_case_document_links')[0])
        self.assertEqual(before,self.db.one('SELECT count(*) FROM audit_logs')[0])
        self.assertEqual(201,self.h.visa_document_linking_confirm('POST',self.cid)[0])
        self.assertEqual(400,self.h.visa_document_linking_confirm('POST',self.cid)[0])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM visa_case_document_links')[0])
        link=self.db.one('SELECT * FROM visa_case_document_links')
        self.assertEqual(self.doc['id'],link['document_id']);self.assertEqual(self.doc['id'],link['student_document_id'])
        self.assertEqual(link['document_role'],link['link_purpose'])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='visa.document_linked'")[0])
        self.assertIsNone(self.db.one('SELECT lodgement_recorded_at FROM visa_cases WHERE id=?',(self.cid,))[0])

    def test_actor_revoked_after_preflight_cannot_insert_link(self):
        validate=self.h.visa_document_linking_validate_existing_document
        def revoke(*args,**kwargs):
            result=validate(*args,**kwargs)
            self.db.execute('UPDATE users SET is_active=0 WHERE id=?',(self.h.user['id'],))
            return result
        with patch.object(self.h,'visa_document_linking_validate_existing_document',side_effect=revoke):
            self.assertEqual(403,self.h.visa_document_linking_confirm('POST',self.cid)[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM visa_case_document_links')[0])

    def test_document_or_case_archived_during_file_read_cannot_be_linked(self):
        original=app.nas.require_student_file_readable
        for kind in ('document','case'):
            with self.subTest(kind=kind):
                self.db.execute('UPDATE student_documents SET is_active=1 WHERE id=?',(self.doc['id'],))
                self.db.execute('UPDATE visa_cases SET archived_at=NULL WHERE id=?',(self.cid,))
                def invalidate(student,path):
                    if kind=='document':self.db.execute('UPDATE student_documents SET is_active=0 WHERE id=?',(self.doc['id'],))
                    else:self.db.execute('UPDATE visa_cases SET archived_at=? WHERE id=?',(self.f.stamp,self.cid))
                    return original(student,path)
                with patch.object(app.nas,'require_student_file_readable',side_effect=invalidate):
                    self.assertEqual(409,self.h.visa_document_linking_confirm('POST',self.cid)[0])
                self.assertEqual(0,self.db.one('SELECT count(*) FROM visa_case_document_links')[0])

    def test_missing_original_file_is_not_a_usable_link(self):
        from pathlib import Path
        Path(self.doc['file_path']).unlink()
        result=self.h.visa_document_linking_confirm('POST',self.cid)
        self.assertEqual(409,result[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM visa_case_document_links')[0])
