from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from database import Database
from business_changes.schema import install


class PendingUploadSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=Database(Path(self.temp.name)/'pending.db');self.db.initialize()
        with self.db.transaction() as conn:
            install(conn)
            conn.execute("INSERT INTO system_settings(key,value,updated_at) VALUES('business_change_sync_v1','enabled','2026-11-19')")
            conn.execute("INSERT INTO students(student_id,passport_first_name,passport_last_name,created_date,updated_at,form_data_json) VALUES('S1','Synthetic','Pending','2026-11-19','2026-11-19','{}')")
        self.files=[{'original_name':'synthetic.pdf','staging_path':'/synthetic/upload/0.bin',
            'content_type':'application/pdf','file_size':4,'sha256':'a'*64}]
        self.allowed=True;self.metadata=[]

    def create(self, *, actor_id=1, files=None, after=None):
        from business_changes.upload_review import start_review
        def authorize(view):
            if not self.allowed:raise PermissionError('synthetic revoked upload')
        with self.db.transaction() as conn:
            return start_review(conn,student_id='S1',module_key='Applications',session_token='synthetic-private-session',
                prepared_files=self.files if files is None else files,actor={'id':actor_id,'username':'synthetic'},
                occurred_at='2026-11-19T00:00:00+00:00',request_context={},authorize=authorize,
                metadata_after=after or (lambda sid,items:self.metadata.append((sid,items))))

    def test_pending_files_counts_audit_and_event_commit_once_without_approval(self):
        before=tuple(self.db.one('SELECT * FROM students'))
        first=self.create();second=self.create(actor_id=2)
        self.assertFalse(first.replayed);self.assertTrue(second.replayed)
        self.assertEqual(1,len(self.metadata))
        self.assertEqual(('NeedsReview',1,0),tuple(self.db.one('SELECT status,total_files,confirmed_files FROM upload_sessions')))
        self.assertEqual('NeedsReview',self.db.one('SELECT status FROM upload_review_items')[0])
        self.assertEqual(before,tuple(self.db.one('SELECT * FROM students')))
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='materials.review_started'")[0])
        self.assertNotIn('synthetic-private-session',str([tuple(row) for row in self.db.all('SELECT * FROM business_change_commands')]))

    def test_changed_bytes_or_revoked_authority_cannot_replay_resource(self):
        self.create()
        changed=[dict(self.files[0],sha256='b'*64)]
        with self.assertRaises(ValueError):self.create(files=changed)
        self.allowed=False
        with self.assertRaises(PermissionError):self.create()
        self.assertEqual(1,self.db.one('SELECT count(*) FROM upload_review_items')[0])

    def test_metadata_or_event_failure_leaves_no_partial_pending_session(self):
        def fail(sid,items):raise OSError('synthetic metadata failure')
        with self.assertRaises(OSError):self.create(after=fail)
        with patch('business_changes.commands._append',side_effect=RuntimeError('synthetic publication failure')):
            with self.assertRaises(RuntimeError):self.create()
        for table in ('upload_sessions','upload_review_items','business_change_events','audit_logs'):
            self.assertEqual(0,self.db.one('SELECT count(*) FROM '+table)[0],table)

    def test_manifest_verification_is_readonly_and_detects_changed_metadata_or_hash(self):
        from business_changes.upload_review import verify_review_manifest
        result=self.create();sid,items=self.metadata[0]
        metadata={'token':'synthetic-private-session','session_id':sid,'student_id':'S1','module':'Applications',
                  'form':{},'files':items,'business_sync_version':1,'created_by':1,'created_at':'2026-11-19T00:00:00+00:00'}
        with self.db.read_snapshot() as conn:
            conn.execute('PRAGMA query_only=ON')
            self.assertTrue(verify_review_manifest(conn,metadata=metadata,prepared_files=self.files))
            with self.assertRaises(ValueError):
                verify_review_manifest(conn,metadata=dict(metadata,student_id='S2'),prepared_files=self.files)
            with self.assertRaises(ValueError):
                verify_review_manifest(conn,metadata=metadata,prepared_files=[dict(self.files[0],sha256='b'*64)])
            with self.assertRaises(ValueError):
                verify_review_manifest(conn,metadata=dict(metadata,files=[dict(items[0],item_id=999)]),prepared_files=self.files)
