from pathlib import Path
import tempfile
import unittest
from database import Database
from business_changes.schema import install


class AtomicApplicationUploadReviewTests(unittest.TestCase):
    def mark(self,action,student_id='S1'):
        from business_changes.upload_review import update_review_state
        with self.db.transaction() as conn:
            return update_review_state(conn,item_id=self.item,student_id=student_id,module_key='Applications',
                action=action,actor={'id':1,'username':'admin'},occurred_at='2026-11-19T00:00:00+00:00',
                authorize=lambda view,review:None)

    def test_failed_review_is_visible_once_and_success_retry_uses_original_revision(self):
        self.mark('failed');self.mark('failed')
        self.assertEqual('NeedsReview',self.db.one('SELECT status FROM upload_review_items')[0])
        self.assertTrue(self.db.one('SELECT error_message FROM upload_review_items')[0])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM business_change_events')[0])
        first=self.confirm();second=self.confirm(actor=2)
        self.assertFalse(first.replayed);self.assertTrue(second.replayed)
        self.assertEqual(2,self.db.one('SELECT count(*) FROM business_change_events')[0])
        self.assertIsNone(self.db.one('SELECT error_message FROM upload_review_items')[0])

    def test_cancel_keeps_confirmed_fact_and_cannot_cross_students(self):
        with self.assertRaises(ValueError):self.mark('cancelled',student_id='S2')
        self.confirm();self.mark('cancelled')
        self.assertEqual('Confirmed',self.db.one('SELECT status FROM upload_review_items')[0])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM business_change_events')[0])

    def test_pending_cancel_updates_session_and_event_atomically_and_cannot_confirm(self):
        self.mark('cancelled');self.mark('cancelled')
        self.assertEqual('Cancelled',self.db.one('SELECT status FROM upload_review_items')[0])
        self.assertEqual('Cancelled',self.db.one('SELECT status FROM upload_sessions')[0])
        with self.assertRaises(ValueError):self.confirm()
        self.assertEqual(1,self.db.one('SELECT count(*) FROM business_change_events')[0])

    def test_confirmed_and_cancelled_items_complete_the_batch_without_revoking_document(self):
        with self.db.transaction() as conn:
            session_id=conn.execute('SELECT upload_session_id FROM upload_review_items WHERE id=?',(self.item,)).fetchone()[0]
            second=conn.execute("""INSERT INTO upload_review_items(upload_session_id,student_id,module_key,
                original_file_name,staging_file_path,file_size,uploaded_by,uploaded_at)
                VALUES(?,'S1','Applications','second.pdf','/synthetic/second',4,1,'2026-11-19')""",(session_id,)).lastrowid
        self.confirm()
        self.assertEqual('NeedsReview',self.db.one('SELECT status FROM upload_sessions')[0])
        self.item=second
        self.mark('cancelled');self.mark('cancelled')
        session=self.db.one('SELECT status,total_files,confirmed_files,cancelled_files FROM upload_sessions')
        self.assertEqual(('Confirmed',2,1,1),tuple(session))
        self.assertEqual(1,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual(2,self.db.one('SELECT count(*) FROM business_change_events')[0])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.db=Database(Path(self.tmp.name)/'review.db');self.db.initialize()
        with self.db.transaction() as c:
            install(c)
            c.execute("INSERT INTO system_settings(key,value,updated_at) VALUES('business_change_sync_v1','enabled','2026-11-19')")
            c.execute("INSERT INTO students(student_id,passport_first_name,passport_last_name,created_date,updated_at,form_data_json) VALUES('S1','Synthetic','Student','2026-11-19','2026-11-19','{}')")
            session=c.execute("INSERT INTO upload_sessions(session_token,student_id,module_key,uploaded_by,uploaded_at) VALUES('synthetic-job','S1','Applications',1,'2026-11-19')").lastrowid
            self.item=c.execute("""INSERT INTO upload_review_items(upload_session_id,student_id,module_key,original_file_name,
                staging_file_path,file_size,uploaded_by,uploaded_at) VALUES(?,'S1','Applications','synthetic.pdf','/synthetic/staged',4,1,'2026-11-19')""",(session,)).lastrowid
        self.allowed=True;self.authorizations=0

    def confirm(self, *, actor=1, student_id='S1', document_type='StudentPassport', after_document=None,module_key='Applications'):
        from business_changes.upload_review import confirm_application_document
        def authorize(view,review):
            self.authorizations+=1
            if not self.allowed:raise PermissionError('permission revoked')
        with self.db.transaction() as c:
            return confirm_application_document(c,item_id=self.item,student_id=student_id,
                document_type=document_type,target_name='',notes='Synthetic review',original_name='synthetic.pdf',
                file_sha256='a'*64,file_size=4,destination='/synthetic/final.pdf',file_version=1,
                actor={'id':actor,'username':'synthetic'},occurred_at='2026-11-19T00:00:00+00:00',
                request_context={},authorize=authorize,after_document=after_document or (lambda view,did:None),module_key=module_key)

    def test_offer_material_and_business_record_share_the_review_command(self):
        from business_changes.upload_review import confirm_application_document
        self.db.execute("UPDATE upload_sessions SET module_key='Offer'")
        self.db.execute("UPDATE upload_review_items SET module_key='Offer'")
        def business(view,did):
            return int(view.execute("INSERT INTO offers(student_id,school_name,document_type,offer_status,offer_file_path,created_at) VALUES('S1','Synthetic School','Offer','received','/synthetic/final.pdf','2026-11-19')").lastrowid)
        with self.db.transaction() as c:
            result=confirm_application_document(c,item_id=self.item,student_id='S1',module_key='Offer',
                document_type='Offer',target_name='Synthetic School',notes='',original_name='synthetic.pdf',
                file_sha256='b'*64,file_size=4,destination='/synthetic/final.pdf',file_version=1,
                actor={'id':1,'username':'admin'},occurred_at='2026-11-19T00:00:00+00:00',
                request_context={},authorize=lambda view,review:None,after_document=business)
        self.assertIsNotNone(result.result['record_id'])
        self.assertEqual('Offer',self.db.one('SELECT folder_category FROM student_documents')[0])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM offers')[0])
        self.assertIsNotNone(self.db.one("SELECT 1 FROM business_change_impacts WHERE subject_kind='module' AND subject_id='offers'"))
        self.assertIsNotNone(self.db.one("SELECT 1 FROM business_change_impacts WHERE subject_kind='offer' AND subject_id=?",(str(result.result['record_id']),)))

    def test_business_callback_cannot_claim_another_students_offer(self):
        self.db.execute("INSERT INTO students(student_id,passport_first_name,passport_last_name,created_date,updated_at,form_data_json) VALUES('S2','Other','Synthetic','2026-11-19','2026-11-19','{}')")
        bid=self.db.execute("INSERT INTO offers(student_id,school_name,document_type,offer_status,created_at) VALUES('S2','Synthetic School','Offer','received','2026-11-19')")
        self.db.execute("UPDATE upload_sessions SET module_key='Offer'")
        self.db.execute("UPDATE upload_review_items SET module_key='Offer'")
        with self.assertRaises(ValueError):self.confirm(module_key='Offer',document_type='Offer',after_document=lambda view,did:bid)
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual('NeedsReview',self.db.one('SELECT status FROM upload_review_items')[0])

    def test_review_document_audit_and_event_commit_once_across_staff_retry(self):
        first=self.confirm();second=self.confirm(actor=2)
        self.assertFalse(first.replayed);self.assertTrue(second.replayed)
        self.assertEqual(first.result['document_id'],second.result['document_id'])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual('Confirmed',self.db.one('SELECT status FROM upload_review_items')[0])
        self.assertEqual('Confirmed',self.db.one('SELECT status FROM upload_sessions')[0])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM business_change_events')[0])
        self.assertEqual(2,self.authorizations)

    def test_failed_case_link_rolls_back_document_review_and_audit(self):
        def fail(view,did):raise RuntimeError('synthetic case link failure')
        with self.assertRaises(RuntimeError):self.confirm(after_document=fail)
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual('NeedsReview',self.db.one('SELECT status FROM upload_review_items')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM audit_logs')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM business_change_events')[0])

    def test_changed_classification_reuses_no_saved_command(self):
        self.confirm()
        with self.assertRaises(ValueError):self.confirm(document_type='Transcripts')
        self.assertEqual(1,self.db.one('SELECT count(*) FROM student_documents')[0])

    def test_foreign_student_and_revoked_permission_are_rejected(self):
        with self.assertRaises(ValueError):self.confirm(student_id='S2')
        self.confirm();self.allowed=False
        with self.assertRaises(PermissionError):self.confirm()
        self.assertEqual(1,self.db.one('SELECT count(*) FROM business_change_events')[0])
