"""Exercise pending-review behavior through the real app GET/POST handlers."""
from unittest.mock import patch
import unittest
import app
import tests.test_business_change_application_upload as native


class PendingReviewAppIntegrationTests(unittest.TestCase):
    setUp = native.NativeApplicationUploadTests.setUp

    def batch(self):
        self.handler.form={};self.handler.csrf='test-csrf'
        self.handler.client_address=('127.0.0.1',10000);self.handler.headers={}
        self.handler.flash_redirect=lambda location,message,kind='success':(location,message,kind)
        self.handler.send_html=lambda body:body
        token=app.create_upload_review('S1','Applications',[
            app.Upload('files','first.txt','text/plain',b'synthetic first'),
            app.Upload('files','second.txt','text/plain',b'synthetic second')],self.handler.user,{})
        folder,metadata=app.load_upload_review(token)
        return token,folder,metadata

    def test_unselected_file_remains_pending_and_get_shows_only_it(self):
        token,folder,metadata=self.batch()
        self.handler.form={'confirm_item_0':'1','document_type_0':'StudentPassport'}
        result=self.handler.upload_review_confirm('POST','S1',token)
        self.assertEqual('success',result[2])
        self.assertEqual(['Confirmed','NeedsReview'],[
            row[0] for row in self.db.all('SELECT status FROM upload_review_items WHERE upload_session_id=? ORDER BY id',(metadata['session_id'],))])
        self.assertEqual(('NeedsReview',1,0),tuple(self.db.one(
            'SELECT status,confirmed_files,cancelled_files FROM upload_sessions WHERE id=?',(metadata['session_id'],))))
        self.assertTrue((folder/'review.json').is_file())
        self.assertEqual(metadata,app.load_upload_review(token)[1])
        with patch.object(app,'page',side_effect=lambda title,body,*args,**kwargs:body), \
             patch.object(app.nas,'status',return_value={},create=True):
            html=self.handler.upload_review('GET','S1',token)
        self.assertNotIn('name="confirm_item_0"',html)
        self.assertIn('name="confirm_item_1"',html)
        self.assertEqual(1,self.db.one('SELECT count(*) FROM student_documents')[0])

    def test_stale_resubmission_cannot_reclassify_a_confirmed_file(self):
        token,folder,metadata=self.batch()
        self.handler.form={'confirm_item_0':'1','document_type_0':'StudentPassport'}
        self.handler.upload_review_confirm('POST','S1',token)
        # A stale tab posts the completed index with altered classification;
        # only the still-pending second file may reach the persistence adapter.
        self.handler.form={'confirm_item_0':'1','document_type_0':'BirthCertificate',
                           'confirm_item_1':'1','document_type_1':'BirthCertificate'}
        result=self.handler.upload_review_confirm('POST','S1',token)
        self.assertEqual('success',result[2])
        self.assertEqual(2,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual('StudentPassport',self.db.one(
            'SELECT selected_document_type FROM upload_review_items WHERE id=?',(metadata['files'][0]['item_id'],))[0])
        self.assertEqual('Confirmed',self.db.one('SELECT status FROM upload_sessions WHERE id=?',(metadata['session_id'],))[0])
        self.assertFalse(folder.exists())

    def test_save_failure_keeps_unselected_and_selected_items_retryable(self):
        token,folder,metadata=self.batch()
        self.handler.form={'confirm_item_0':'1','document_type_0':'StudentPassport'}
        with patch.object(app,'make_preview',side_effect=OSError('isolated renderer failure')):
            result=self.handler.upload_review_confirm('POST','S1',token)
        self.assertEqual('error',result[2])
        rows=self.db.all('SELECT status,error_message FROM upload_review_items WHERE upload_session_id=? ORDER BY id',(metadata['session_id'],))
        self.assertEqual(['NeedsReview','NeedsReview'],[r[0] for r in rows])
        self.assertTrue(rows[0][1]);self.assertFalse(rows[1][1])
        self.assertTrue((folder/'review.json').is_file())
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])

    def test_explicit_discard_cancels_only_that_item_and_plain_unselected_survives(self):
        self.handler.form={};self.handler.csrf='test-csrf'
        self.handler.client_address=('127.0.0.1',10000);self.handler.headers={}
        self.handler.flash_redirect=lambda location,message,kind='success':(location,message,kind)
        token=app.create_upload_review('S1','Applications',[
            app.Upload('files',f'{index}.txt','text/plain',f'file {index}'.encode())
            for index in range(3)],self.handler.user,{})
        folder,metadata=app.load_upload_review(token)
        self.handler.form={'confirm_item_0':'1','document_type_0':'StudentPassport',
                           'discard_item_1':'1'}
        self.handler.upload_review_confirm('POST','S1',token)
        rows=self.db.all('SELECT status FROM upload_review_items WHERE upload_session_id=? ORDER BY id',(metadata['session_id'],))
        self.assertEqual(['Confirmed','Cancelled','NeedsReview'],[r[0] for r in rows])
        self.assertTrue((folder/'review.json').is_file())
        self.assertEqual(('NeedsReview',1,1),tuple(self.db.one(
            'SELECT status,confirmed_files,cancelled_files FROM upload_sessions WHERE id=?',(metadata['session_id'],))))
        # Replaying the stale selection cannot create a second official file.
        self.handler.upload_review_confirm('POST','S1',token)
        self.assertEqual(1,self.db.one('SELECT count(*) FROM student_documents')[0])

    def test_only_discard_retains_unchecked_then_last_discard_completes_without_document(self):
        token,folder,metadata=self.batch()
        self.handler.form={'discard_item_0':'1'}
        result=self.handler.upload_review_confirm('POST','S1',token)
        self.assertEqual('success',result[2])
        self.assertEqual(['Cancelled','NeedsReview'],[
            row[0] for row in self.db.all('SELECT status FROM upload_review_items WHERE upload_session_id=? ORDER BY id',(metadata['session_id'],))])
        self.assertTrue((folder/'review.json').is_file())
        self.assertEqual(metadata,app.load_upload_review(token)[1])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])
        events=self.db.one('SELECT count(*) FROM business_change_events')[0]
        # A stale cancel-only POST may show a no-selection message, but cannot
        # cancel the unchecked file or append another cancellation event.
        self.handler.upload_review_confirm('POST','S1',token)
        self.assertEqual(events,self.db.one('SELECT count(*) FROM business_change_events')[0])
        self.assertEqual('NeedsReview',self.db.one('SELECT status FROM upload_review_items WHERE id=?',(metadata['files'][1]['item_id'],))[0])
        self.handler.form={'discard_item_1':'1'}
        result=self.handler.upload_review_confirm('POST','S1',token)
        self.assertEqual('success',result[2])
        self.assertEqual(('Cancelled',0,2),tuple(self.db.one(
            'SELECT status,confirmed_files,cancelled_files FROM upload_sessions WHERE id=?',(metadata['session_id'],))))
        self.assertFalse(folder.exists())
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])
        events=self.db.one('SELECT count(*) FROM business_change_events')[0]
        with self.assertRaisesRegex(ValueError,'已失效或不存在'):
            self.handler.upload_review_confirm('POST','S1',token)
        self.assertEqual(events,self.db.one('SELECT count(*) FROM business_change_events')[0])

    def test_empty_selection_without_explicit_discard_preserves_entire_batch(self):
        token,folder,metadata=self.batch()
        self.handler.form={}
        before=[tuple(row) for row in self.db.all('SELECT * FROM upload_review_items WHERE upload_session_id=? ORDER BY id',(metadata['session_id'],))]
        events=self.db.one('SELECT count(*) FROM business_change_events')[0]
        result=self.handler.upload_review_confirm('POST','S1',token)
        self.assertEqual('error',result[2])
        self.assertEqual(before,[tuple(row) for row in self.db.all('SELECT * FROM upload_review_items WHERE upload_session_id=? ORDER BY id',(metadata['session_id'],))])
        self.assertEqual(events,self.db.one('SELECT count(*) FROM business_change_events')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertTrue((folder/'review.json').is_file())
