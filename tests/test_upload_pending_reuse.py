"""Exact retry identities must not silently approve a different upload."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import app
from database import Database
from business_changes.schema import install

ROOT = Path(__file__).resolve().parents[1]


class PendingReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT/'STAGING_DATA'/'tmp')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = Database(self.root/'pending.db')
        self.db.initialize()
        with self.db.transaction() as conn:
            install(conn)
            conn.execute("INSERT INTO system_settings(key,value,updated_at) VALUES('business_change_sync_v1','enabled','2026-11-24')")
            conn.execute("INSERT INTO students(student_id,passport_first_name,passport_last_name,created_date,updated_at,form_data_json) VALUES('S1','Synthetic','Retry','2026-11-24','2026-11-24','{}')")
        self.user = {'id':1,'username':'admin','role_name':'Admin'}
        for mocked in (patch.object(app,'db',self.db),
                       patch.object(app,'UPLOAD_REVIEW_DIR',self.root/'pending'),
                       patch.object(app,'can_review_module_action',return_value=True)):
            mocked.start(); self.addCleanup(mocked.stop)

    def create(self, data=b'original', form=None, name='original.pdf'):
        return app.create_upload_review('S1','Applications',
            [app.Upload('files',name,'application/pdf',data)],self.user,form or {})

    def test_duplicate_preview_reuses_verified_pending_batch(self):
        token=self.create()
        self.assertEqual(token,self.create())
        self.assertEqual(1,self.db.one('SELECT count(*) FROM upload_sessions')[0])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM upload_review_items')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual(1,len(list((self.root/'pending').iterdir())))

    def test_changed_content_context_or_name_stays_independently_pending(self):
        tokens={self.create(), self.create(data=b'different'),
                self.create(form={'application_cycle_id':'2'}), self.create(name='other.pdf')}
        self.assertEqual(4,len(tokens))
        self.assertEqual(4,self.db.one("SELECT count(*) FROM upload_review_items WHERE status='NeedsReview'")[0])

    def test_corrupt_staging_cannot_be_reused(self):
        token=self.create()
        folder,metadata=app.load_upload_review(token)
        (folder/metadata['files'][0]['blob']).write_bytes(b'tampered')
        self.assertNotEqual(token,self.create())
        self.assertEqual(2,self.db.one("SELECT count(*) FROM upload_review_items WHERE status='NeedsReview'")[0])

    def test_completed_upload_does_not_block_an_intentional_new_version(self):
        token=self.create()
        self.db.execute("UPDATE upload_review_items SET status='Confirmed'")
        self.assertNotEqual(token,self.create())

    def test_retry_rechecks_permission_and_preserves_failed_pending_item(self):
        token=self.create()
        self.db.execute("UPDATE upload_review_items SET error_message='save failed'")
        self.assertEqual(token,self.create())
        self.assertEqual('save failed',self.db.one('SELECT error_message FROM upload_review_items')[0])
        with patch.object(app,'can_review_module_action',return_value=False):
            with self.assertRaises(PermissionError):self.create()
        self.assertEqual(1,self.db.one('SELECT count(*) FROM upload_sessions')[0])

    def test_partial_batch_retry_reuses_batch_and_keeps_unconfirmed_item(self):
        uploads=[app.Upload('files','first.pdf','application/pdf',b'first'),
                 app.Upload('files','second.pdf','application/pdf',b'second')]
        token=app.create_upload_review('S1','Applications',uploads,self.user,{})
        self.db.execute("UPDATE upload_review_items SET status='Confirmed' WHERE original_file_name='first.pdf'")
        self.assertEqual(token,app.create_upload_review('S1','Applications',uploads,self.user,{}))
        self.assertEqual(1,self.db.one("SELECT count(*) FROM upload_review_items WHERE status='NeedsReview'")[0])

    def test_partial_batch_retains_only_unconfirmed_files_for_next_review(self):
        from upload_review_confirmation_coordinator import pending_review_files
        token=app.create_upload_review('S1','Applications',[
            app.Upload('files','first.pdf','application/pdf',b'first'),
            app.Upload('files','second.pdf','application/pdf',b'second')],self.user,{})
        folder,metadata=app.load_upload_review(token)
        self.db.execute("UPDATE upload_review_items SET status='Confirmed' WHERE id=?",(metadata['files'][0]['item_id'],))
        with self.db.read_snapshot() as conn:
            self.assertEqual([metadata['files'][1]],pending_review_files(conn,metadata))
        self.assertTrue((folder/'review.json').is_file())
        self.assertEqual('NeedsReview',self.db.one('SELECT status FROM upload_review_items WHERE id=?',(metadata['files'][1]['item_id'],))[0])

    def test_new_login_csrf_reuses_same_business_pending_batch(self):
        first=self.create(form={'csrf':'first-login-token','application_cycle_id':'2'})
        second=self.create(form={'csrf':'new-login-token','application_cycle_id':'2'})
        self.assertEqual(first,second)
        folder,metadata=app.load_upload_review(first)
        self.assertEqual('first-login-token',metadata['form']['csrf'])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM upload_sessions')[0])
        # Only transport CSRF is ignored. A new cycle is a distinct intention.
        third=self.create(form={'csrf':'new-login-token','application_cycle_id':'3'})
        self.assertNotEqual(first,third)
