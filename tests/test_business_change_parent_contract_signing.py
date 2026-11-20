from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from database import Database
from business_changes.schema import install


class ParentSignatureCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=Database(Path(self.temp.name)/'parent-signature.db');self.db.initialize()
        with self.db.transaction() as conn:
            install(conn)
            conn.execute("INSERT INTO system_settings(key,value,updated_at) VALUES('business_change_sync_v1','enabled','2026-11-19')")
            for sid in ('S1','S2'):
                conn.execute("INSERT INTO students(student_id,passport_first_name,passport_last_name,created_date,updated_at,form_data_json) VALUES(?,'Synthetic','Parent','2026-11-19','2026-11-19','{}')",(sid,))
            self.contract=conn.execute("INSERT INTO contracts(student_id,contract_uid,contract_category,contract_type,contract_title,contract_status,service_scope_json,generated_at,updated_at) VALUES('S1','SYNTHETIC-SIGN','Study','StudyAbroadContract','Synthetic','Sent','[]','2026-11-19','2026-11-19')").lastrowid
            self.link=conn.execute("INSERT INTO contract_signing_links(contract_id,student_id,token_hash,token_last_4,signing_url,status,expires_at,created_at) VALUES(?,'S1','private-token-hash','hash','/synthetic','active','2099-01-01','2026-11-19')",(self.contract,)).lastrowid
        self.permitted=True;self.calls=0

    def sign(self,*,sha='a'*64,after=None):
        from business_changes.parent_contract_signing import record_parent_signature
        def authorize(view):
            if not self.permitted:raise PermissionError('parent authority revoked')
        def persist(view):
            self.calls+=1
            signature=view.execute("INSERT INTO contract_signatures(contract_id,student_id,link_id,signed_at,signature_image_path,status,archive_status,created_at,updated_at) VALUES(?,'S1',?,'2026-11-19','/synthetic/verified.png','signed','pending','2026-11-19','2026-11-19')",(self.contract,self.link)).lastrowid
            view.execute("UPDATE contracts SET contract_status='ParentSigned' WHERE id=?",(self.contract,))
            view.execute("UPDATE contract_signing_links SET status='signed',signed_signature_id=? WHERE id=?",(signature,self.link))
            if after:after(view,signature)
            return int(signature)
        with self.db.transaction() as conn:
            return record_parent_signature(conn,contract_id=self.contract,link_id=self.link,student_id='S1',
                actor_ref='b'*64,image_sha256=sha,signature_path='/synthetic/verified.png',
                occurred_at='2026-11-19T00:00:00+00:00',authorize=authorize,operation=persist)

    def test_signature_parent_status_link_and_event_commit_once_without_full_execution(self):
        first=self.sign();second=self.sign()
        self.assertFalse(first.replayed);self.assertTrue(second.replayed);self.assertEqual(1,self.calls)
        self.assertEqual(first.result,second.result)
        self.assertEqual(('ParentSigned','signed','pending'),tuple(self.db.one('SELECT c.contract_status,s.status,s.archive_status FROM contracts c JOIN contract_signatures s ON s.contract_id=c.id')))
        self.assertEqual(1,self.db.one('SELECT count(*) FROM business_change_events')[0])
        queued=str([tuple(r) for r in self.db.all('SELECT * FROM business_change_commands')])
        self.assertNotIn('private-token',queued);self.assertNotIn('verified.png',queued)

    def test_same_link_different_signature_conflicts_and_replay_rechecks_authority(self):
        self.sign()
        with self.assertRaises(ValueError):self.sign(sha='c'*64)
        self.permitted=False
        with self.assertRaises(PermissionError):self.sign()
        self.assertEqual(1,self.calls)

    def test_publication_failure_cannot_leave_saved_signature_or_parent_signed_link(self):
        with patch('business_changes.commands._append',side_effect=RuntimeError('signature publication')):
            with self.assertRaises(RuntimeError):self.sign()
        self.assertEqual(0,self.db.one('SELECT count(*) FROM contract_signatures')[0])
        self.assertEqual('Sent',self.db.one('SELECT contract_status FROM contracts')[0])
        self.assertEqual('active',self.db.one('SELECT status FROM contract_signing_links')[0])

    def test_cross_student_link_and_callback_source_are_rejected(self):
        self.db.execute("UPDATE contract_signing_links SET student_id='S2' WHERE id=?",(self.link,))
        with self.assertRaises(ValueError):self.sign()
        self.assertEqual(0,self.calls)
        self.db.execute("UPDATE contract_signing_links SET student_id='S1' WHERE id=?",(self.link,))
        with self.assertRaises(ValueError):self.sign(after=lambda view,sid:view.execute("UPDATE contract_signatures SET student_id='S2' WHERE id=?",(sid,)))
        self.assertEqual(0,self.db.one('SELECT count(*) FROM contract_signatures')[0])

    def test_failed_retry_preserves_a_previous_completed_archive(self):
        from business_changes.parent_contract_signing import record_archive_failure
        result=self.sign();sid=result.result['record_id']
        self.db.execute("UPDATE contract_signatures SET archive_status='archived' WHERE id=?",(sid,))
        self.db.execute("UPDATE contracts SET contract_status='FullyExecuted' WHERE id=?",(self.contract,))
        with self.db.transaction() as conn:
            record_archive_failure(conn,signature_id=sid,error_message='synthetic retry failure',actor_kind='system',
                actor_ref='contract_archive',occurred_at='2026-11-19T00:00:00+00:00',authorize=lambda view:None)
        self.assertEqual('archived',self.db.one('SELECT archive_status FROM contract_signatures')[0])
        self.assertEqual('FullyExecuted',self.db.one('SELECT contract_status FROM contracts')[0])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='contract.archive_failed'")[0])

    def test_failure_event_error_rolls_back_only_failure_marker_and_diagnostic(self):
        from business_changes.parent_contract_signing import record_archive_failure
        sid=self.sign().result['record_id']
        with self.db.transaction() as conn:
            with patch('business_changes.commands._append',side_effect=RuntimeError('failure publication')):
                with self.assertRaises(RuntimeError):
                    record_archive_failure(conn,signature_id=sid,error_message='synthetic',actor_kind='system',
                        actor_ref='contract_archive',occurred_at='2026-11-19T00:00:00+00:00',authorize=lambda view:None)
        self.assertEqual('pending',self.db.one('SELECT archive_status FROM contract_signatures')[0])
        self.assertEqual('ParentSigned',self.db.one('SELECT contract_status FROM contracts')[0])
        self.assertEqual(0,self.db.one("SELECT count(*) FROM contract_signing_logs WHERE event_type='archive_attempt_failed'")[0])
