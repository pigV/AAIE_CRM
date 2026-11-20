"""Case writes must use current rights, including when a session is stale."""
import unittest
import hashlib
from contextlib import contextmanager
from unittest.mock import patch

import app
from tests import test_business_change_case_snapshots as case_fixture


class VisaWorkspaceWritePermissionsTests(unittest.TestCase):
    # Reuse the isolated schema/handler fixture, not its tests.
    setUp=case_fixture.VisaCaseSnapshotTests.setUp
    def test_view_only_case_access_cannot_change_a_grant_date(self):
        role=self.db.one("SELECT id FROM roles WHERE name='View Only'")[0]
        uid=self.db.execute("""INSERT INTO users(username,display_name,password_hash,role_id,is_active,created_at)
            VALUES('synthetic-case-reader','SYNTHETIC READER','unused',?,1,?)""",(role,self.f.stamp))
        app.save_user_module_permissions(uid,['visa'],1,role_name='View Only')
        self.h.user=dict(self.db.one('''SELECT u.*,r.name role_name,r.permissions_json FROM users u
            JOIN roles r ON r.id=u.role_id WHERE u.id=?''',(uid,)))
        self.h.user['module_permissions_json']=app.modules_json_for_user(uid)
        self.assertTrue(app.can_access_module(self.h.user,'visa'))
        self.assertFalse(app.can_save_visa_record(self.h.user))
        before=self.db.one('SELECT count(*) FROM business_change_events')[0]
        self.h.form={'action':'correct-grant-date','grant_date':'2025-01-20'}
        result=self.h.visa_case_workspace('POST',self.cid)
        self.assertEqual(403,result[0],result)
        self.assertIsNone(self.db.one('SELECT grant_date FROM visa_cases WHERE id=?',(self.cid,))[0])
        self.assertEqual(before,self.db.one('SELECT count(*) FROM business_change_events')[0])

    def test_disabled_account_in_an_old_admin_session_cannot_write(self):
        self.db.execute('UPDATE users SET is_active=0 WHERE id=?',(self.h.user['id'],))
        self.h.form={'action':'correct-grant-date','grant_date':'2025-01-20'}
        result=self.h.visa_case_workspace('POST',self.cid)
        self.assertEqual(403,result[0],result)
        self.assertIsNone(self.db.one('SELECT grant_date FROM visa_cases WHERE id=?',(self.cid,))[0])

    def test_permissions_revoked_after_preflight_are_checked_before_the_write(self):
        original=self.db.transaction
        @contextmanager
        def revoke():
            with original() as connection:
                connection.execute('UPDATE users SET is_active=0 WHERE id=?',(self.h.user['id'],))
            with original() as connection:yield connection
        self.h.form={'action':'correct-grant-date','grant_date':'2025-01-20'}
        with patch.object(self.db,'transaction',side_effect=revoke):
            result=self.h.visa_case_workspace('POST',self.cid)
        self.assertEqual(403,result[0],result)
        self.assertIsNone(self.db.one('SELECT grant_date FROM visa_cases WHERE id=?',(self.cid,))[0])

    def test_authorized_visa_officer_can_save_checklist_with_current_module_access(self):
        role=self.db.one("SELECT id FROM roles WHERE name='Visa Officer'")[0]
        uid=self.db.execute("""INSERT INTO users(username,display_name,password_hash,role_id,is_active,created_at)
            VALUES('synthetic-case-officer','SYNTHETIC OFFICER','unused',?,1,?)""",(role,self.f.stamp))
        app.save_user_module_permissions(uid,['visa'],1,role_name='Visa Officer')
        self.h.user=dict(self.db.one('''SELECT u.*,r.name role_name,r.permissions_json FROM users u
            JOIN roles r ON r.id=u.role_id WHERE u.id=?''',(uid,)))
        self.h.user['module_permissions_json']=app.modules_json_for_user(uid)
        self.assertTrue(app.can_workflow_module_save(self.h.user,'Visa'))
        case=self.db.one('SELECT * FROM visa_cases WHERE id=?',(self.cid,))
        self.h.visa_material_initialize_checklist(case)
        item=self.db.one('SELECT id FROM visa_checklist_items WHERE visa_case_id=? ORDER BY id LIMIT 1',(self.cid,))[0]
        self.h.form=self.h.business_change_checklist_form_values(self.cid)
        self.h.business_change_save_checklist(self.f.sid,self.cid,[(item,'missing','SYNTHETIC OFFICER NOTE')],self.f.stamp)
        self.assertEqual('SYNTHETIC OFFICER NOTE',self.db.one('SELECT notes FROM visa_checklist_items WHERE id=?',(item,))[0])
        self.h.form={'action':'correct-grant-date','grant_date':'2025-01-20'}
        result=self.h.visa_case_workspace('POST',self.cid)
        self.assertEqual('redirect',result[0],result)
        saved=self.db.one("SELECT after_json,actor_user_id FROM visa_document_audit WHERE visa_case_id=? AND action='grant_date_corrected' ORDER BY id DESC LIMIT 1",(self.cid,))
        self.assertIn('2025-01-20',saved[0])
        self.assertEqual(uid,saved[1])

    def test_case_write_rechecks_deleted_student_and_cycle_ownership(self):
        self.h.form={'action':'correct-grant-date','grant_date':'2025-01-20'}
        self.db.execute('UPDATE students SET deleted_at=? WHERE student_id=?',(self.f.stamp,self.f.sid))
        result=self.h.visa_case_workspace('POST',self.cid)
        self.assertEqual(409,result[0],result)
        self.assertIsNone(self.db.one('SELECT grant_date FROM visa_cases WHERE id=?',(self.cid,))[0])

    def _ai_document_and_provider(self,*,revoke=False):
        import visa_documents_candidate as docs
        path=self.f.student_root/'synthetic-ai-evidence.txt'
        path.write_text('SYNTHETIC ONLY - retained source')
        with self.db.transaction() as connection:
            document=docs.register_document(connection,self.cid,file_path=str(path),original_name=path.name,
                version=1,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),actor_user_id=1,occurred_at=self.f.stamp)
        self.h._visa_text_from_document=lambda *args:'SYNTHETIC ONLY - retained source'
        database=self.db
        class Provider:
            def extract_letter(inner,text,evidence):
                if revoke:database.execute('UPDATE users SET is_active=0 WHERE id=1')
                return {'result':docs.propose_letter(''),'metadata':{
                    'source_evidence':[evidence],'provider':'synthetic','model':'no-network'}}
            def analyze(inner,context):
                if revoke:database.execute('UPDATE users SET is_active=0 WHERE id=1')
                return {'result':{'Summary':'SYNTHETIC ONLY','Risk':'Staff review','Missing':'','Action Required':'Review','full_analysis':'Synthetic'},
                    'metadata':{'source_evidence':context['documents'],'provider':'synthetic','model':'no-network'}}
        self.h.form={'action':'ai-extract','document_id':str(document['id'])}
        return document,Provider()

    def test_ai_reply_cannot_write_after_staff_account_is_disabled_during_preparation(self):
        document,provider=self._ai_document_and_provider(revoke=True)
        before=self.db.one('SELECT count(*) FROM business_change_events')[0]
        with patch('visa_ai_candidate.provider_from_environment',return_value=provider):
            result=self.h.visa_case_workspace('POST',self.cid)
        self.assertEqual(403,result[0],result)
        self.assertEqual(0,self.db.one('SELECT count(*) FROM visa_letter_extractions WHERE document_id=?',(document['id'],))[0])
        self.assertEqual(before,self.db.one('SELECT count(*) FROM business_change_events')[0])

    def test_saved_ai_proposal_publishes_a_case_change_without_confirming_a_result(self):
        document,provider=self._ai_document_and_provider()
        with patch('visa_ai_candidate.provider_from_environment',return_value=provider):
            result=self.h.visa_case_workspace('POST',self.cid)
        self.assertEqual('redirect',result[0],result)
        self.assertEqual(1,self.db.one('SELECT count(*) FROM visa_letter_extractions WHERE document_id=?',(document['id'],))[0])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='visa.ai_extraction_saved' AND source_id=?",(str(self.cid),))[0])
        self.assertEqual('Pending',self.db.one('SELECT result_status FROM visa_cases WHERE id=?',(self.cid,))[0])

    def test_ai_analysis_rechecks_staff_after_the_external_preparation(self):
        import visa_documents_candidate as docs
        document,provider=self._ai_document_and_provider(revoke=True)
        with self.db.transaction() as connection:
            docs.mark_submitted(connection,self.cid,document['id'],actor_user_id=1,occurred_at=self.f.stamp)
        with patch('visa_ai_candidate.provider_from_environment',return_value=provider):
            result=self.h.visa_submission_analysis('POST',self.cid)
        self.assertEqual(403,result[0],result)
        self.assertEqual(0,self.db.one("SELECT count(*) FROM visa_document_audit WHERE action='ai_analysis_completed'")[0])

    def test_saved_ai_analysis_publishes_the_original_case_scope(self):
        import visa_documents_candidate as docs
        document,provider=self._ai_document_and_provider()
        with self.db.transaction() as connection:
            docs.mark_submitted(connection,self.cid,document['id'],actor_user_id=1,occurred_at=self.f.stamp)
        with patch('visa_ai_candidate.provider_from_environment',return_value=provider):
            result=self.h.visa_submission_analysis('POST',self.cid)
        self.assertEqual('redirect',result[0],result)
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='visa.ai_analysis_saved' AND source_id=?",(str(self.cid),))[0])

    def test_ai_reply_cannot_follow_a_case_reassigned_during_preparation(self):
        document,provider=self._ai_document_and_provider()
        extract=provider.extract_letter
        def reassign(text,evidence):
            result=extract(text,evidence)
            self.db.execute('UPDATE visa_cases SET application_cycle_id=NULL WHERE id=?',(self.cid,))
            return result
        provider.extract_letter=reassign
        with patch('visa_ai_candidate.provider_from_environment',return_value=provider):
            result=self.h.visa_case_workspace('POST',self.cid)
        self.assertEqual(409,result[0],result)
        self.assertEqual(0,self.db.one('SELECT count(*) FROM visa_letter_extractions WHERE document_id=?',(document['id'],))[0])

    def test_failed_ai_change_publication_rolls_back_proposal_and_retained_audit(self):
        document,provider=self._ai_document_and_provider()
        with patch('visa_ai_candidate.provider_from_environment',return_value=provider),patch(
                'business_changes.commands._append',side_effect=OSError('synthetic event-store failure')):
            with self.assertRaises(OSError):self.h.visa_case_workspace('POST',self.cid)
        self.assertEqual(0,self.db.one('SELECT count(*) FROM visa_letter_extractions WHERE document_id=?',(document['id'],))[0])
        self.assertEqual(0,self.db.one("SELECT count(*) FROM visa_document_audit WHERE action='ai_letter_extracted'")[0])
