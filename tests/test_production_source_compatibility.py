"""Synthetic regressions for business fixes already present in production."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from database import Database
from business_changes.schema import install
from student_lifecycle.transfer_state import load_student_transfer_state_map

NOW = '2026-11-20T00:00:00+00:00'


class PendingTransferCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / 'compat.db'); self.db.initialize()
        with self.db.transaction() as conn:
            install(conn)
            conn.execute("ALTER TABLE contracts ADD COLUMN application_cycle_id INTEGER")
            conn.execute("INSERT INTO system_settings(key,value,updated_at) VALUES('business_change_sync_v1','enabled',?)", (NOW,))
            conn.execute("INSERT INTO students(student_id,passport_first_name,passport_last_name,created_date,updated_at,form_data_json) VALUES('S1','Synthetic','Compatibility',?,?,'{}')", (NOW,NOW))
            self.cid = conn.execute("INSERT INTO contracts(student_id,contract_uid,contract_category,contract_type,contract_title,contract_status,service_scope_json,generated_at,updated_at) VALUES('S1','compat-transfer','Study','TransferApplicationContract','Synthetic','Sent','[]',?,?)", (NOW,NOW)).lastrowid
            self.lid = conn.execute("INSERT INTO contract_signing_links(contract_id,student_id,token_hash,token_last_4,signing_url,status,expires_at,created_at,bound_wechat_openid) VALUES(?,'S1',?,'test','/synthetic','active','2099-01-01T00:00:00+00:00',?,'synthetic-openid')", (self.cid, app.contract_signing_token_hash('synthetic-token'),NOW)).lastrowid
        self.h = object.__new__(app.CRMHandler)
        self.h.user = {'id':1,'username':'admin','display_name':'Admin'}
        self.h.headers = {}; self.h.client_address = ('127.0.0.1',1)
        self.p = patch.object(app,'db',self.db); self.p.start(); self.addCleanup(self.p.stop)

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('student_lifecycle.pending_transfer_withdrawal'), 'production withdrawal service is missing')
        from student_lifecycle import pending_transfer_withdrawal
        return pending_transfer_withdrawal

    def withdraw(self, keep=False):
        module = self.module()
        with self.db.transaction() as conn:
            return module.withdraw_pending_transfer(conn,'S1',self.cid,'Synthetic cancellation',actor=self.h.user,timestamp=NOW,keep_signature=keep)

    def marker(self):
        self.db.execute("INSERT INTO audit_logs(action_type,module_name,contract_id,created_at) VALUES('pending_transfer_withdrawn','contracts',?,?)", (str(self.cid),NOW))

    def test_unsigned_withdrawal_revokes_links_and_publishes_versions(self):
        self.withdraw()
        self.assertEqual('Revoked',self.db.one('SELECT contract_status FROM contracts WHERE id=?',(self.cid,))[0])
        self.assertEqual('revoked',self.db.one('SELECT status FROM contract_signing_links WHERE id=?',(self.lid,))[0])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='contract.pending_transfer_withdrawn'")[0])
        self.assertEqual(1,self.db.one("SELECT version FROM business_change_versions WHERE subject_kind='student' AND subject_id='S1'")[0])

    def test_disabled_mode_withdrawal_keeps_production_semantics_without_events(self):
        self.db.execute("UPDATE system_settings SET value='disabled' WHERE key='business_change_sync_v1'")
        self.withdraw()
        self.assertEqual('Revoked',self.db.one('SELECT contract_status FROM contracts')[0])
        self.assertEqual('revoked',self.db.one('SELECT status FROM contract_signing_links')[0])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM audit_logs WHERE action_type='pending_transfer_withdrawn'")[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM business_change_events')[0])

    def test_signed_withdrawal_retains_signature_and_exits_pending_state(self):
        self.db.execute("UPDATE contracts SET contract_status='ParentSigned',signed_at=? WHERE id=?",(NOW,self.cid))
        self.db.execute("UPDATE contract_signing_links SET status='signed' WHERE id=?",(self.lid,))
        self.db.execute("INSERT INTO contract_signatures(contract_id,student_id,link_id,signed_at,status,signature_image_path,created_at,updated_at) VALUES(?,'S1',?,?,'signed','/synthetic/signature.png',?,?)",(self.cid,self.lid,NOW,NOW,NOW))
        self.withdraw(keep=True)
        self.assertEqual('ParentSigned',self.db.one('SELECT contract_status FROM contracts')[0])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM contract_signatures WHERE status='signed'")[0])
        with self.db.read_snapshot() as conn:
            state=load_student_transfer_state_map(conn,('S1',))['S1']
        self.assertEqual('ordinary',state.state)

    def test_confirmation_rejects_signature_state_changed_after_page(self):
        module=self.module()
        self.db.execute("UPDATE contracts SET contract_status='ParentSigned',signed_at=? WHERE id=?",(NOW,self.cid))
        with self.assertRaises(module.PendingTransferWithdrawalConflict):self.withdraw()
        self.assertEqual('active',self.db.one('SELECT status FROM contract_signing_links')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM business_change_events')[0])

    def test_publish_failure_rolls_back_contract_links_and_audit(self):
        self.module()
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic publication failure')):
            with self.assertRaises(RuntimeError):self.withdraw()
        self.assertEqual('Sent',self.db.one('SELECT contract_status FROM contracts')[0])
        self.assertEqual('active',self.db.one('SELECT status FROM contract_signing_links')[0])
        self.assertEqual(0,self.db.one("SELECT count(*) FROM audit_logs WHERE action_type='pending_transfer_withdrawn'")[0])

    def test_stopped_signed_contract_cannot_regenerate_link_at_persistence(self):
        self.db.execute("UPDATE contracts SET contract_status='ParentSigned' WHERE id=?",(self.cid,))
        self.marker()
        contract=dict(self.db.one('SELECT * FROM contracts WHERE id=?',(self.cid,)))
        with patch.object(app,'require_transfer_contract_signable'):
            with self.assertRaises(ValueError):self.h._contract_signing_persist_link(contract,self.h.user,True)
        self.assertEqual(1,self.db.one('SELECT count(*) FROM contract_signing_links')[0])

    def test_withdrawal_audit_suppresses_latest_contract_without_reviving_older_draft(self):
        self.db.execute("INSERT INTO contracts(student_id,contract_uid,contract_category,contract_type,contract_title,contract_status,generated_at,updated_at) VALUES('S1','compat-old','Study','TransferApplicationContract','Older','Draft','2025-01-01','2025-01-01')")
        self.marker()
        with self.db.read_snapshot() as conn:state=load_student_transfer_state_map(conn,('S1',))['S1']
        self.assertEqual('ordinary',state.state)

    def test_enabled_signature_command_rejects_stopped_transfer_audit(self):
        from business_changes.parent_contract_signing import record_parent_signature
        self.marker()
        def persist(view):
            sid=view.execute("INSERT INTO contract_signatures(contract_id,student_id,link_id,signed_at,signature_image_path,status,archive_status,created_at,updated_at) VALUES(?,'S1',?,?,'/synthetic/signature.png','signed','pending',?,?)",(self.cid,self.lid,NOW,NOW,NOW)).lastrowid
            view.execute("UPDATE contracts SET contract_status='ParentSigned' WHERE id=?",(self.cid,))
            return int(sid)
        with self.assertRaises(ValueError):
            with self.db.transaction() as conn:
                record_parent_signature(conn,contract_id=self.cid,link_id=self.lid,student_id='S1',actor_ref='b'*64,image_sha256='a'*64,signature_path='/synthetic/signature.png',occurred_at=NOW,authorize=lambda view:None,operation=persist)
        self.assertEqual(0,self.db.one('SELECT count(*) FROM contract_signatures')[0])

    def test_enabled_handler_retains_atomic_parent_step_when_pdf_archive_fails(self):
        self.h.contract_signing_cookie_valid=lambda link:True
        self.h.contract_signing_pdf_preview_pages_data_uris=lambda *args:[]
        self.h.school_plan_share_public_page=lambda *args,**kwargs:None
        image=Path(self.temp.name)/'signatures'/str(self.cid)/'signature_sync_synthetic.png'
        image.parent.mkdir(parents=True);image.write_bytes(b'synthetic-signature')
        self.h.contract_signing_save_signature_image=lambda *args,**kwargs:image
        self.h.contract_signing_archive_documents=lambda *args,**kwargs:(_ for _ in ()).throw(OSError('synthetic PDF archive unavailable'))
        with patch.object(app,'CONTRACT_SIGNING_DIR',Path(self.temp.name)/'signatures'), patch.object(app,'parse_request_body',return_value=({'declaration':'yes','signature_data':'synthetic'},{})):
            self.h.contract_signing_submit('POST','synthetic-token')
        self.assertEqual('ParentSigned',self.db.one('SELECT contract_status FROM contracts')[0])
        self.assertEqual('signed',self.db.one('SELECT status FROM contract_signing_links')[0])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM contract_signatures WHERE status='signed'")[0])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='contract.parent_signed'")[0])

    def test_legacy_signature_submission_cannot_win_after_link_revocation(self):
        self.h.business_changes_enabled=lambda:False
        self.h.contract_signing_cookie_valid=lambda link:True
        image=Path(self.temp.name)/'synthetic-signature.png'
        def revoke_during_image_save(*args,**kwargs):
            image.write_bytes(b'synthetic')
            self.db.execute("UPDATE contract_signing_links SET status='revoked',invalidated_at=? WHERE id=?",(NOW,self.lid))
            self.marker()
            return image
        self.h.contract_signing_save_signature_image=revoke_during_image_save
        result={}
        self.h.school_plan_share_public_page=lambda title,body,status=200:result.update(status=status)
        self.h.contract_signing_archive_documents=lambda *a,**k:{}
        with patch.object(app,'parse_request_body',return_value=({'declaration':'yes','signature_data':'synthetic'},{})):
            self.h.contract_signing_submit('POST','synthetic-token')
        self.assertEqual(0,self.db.one('SELECT count(*) FROM contract_signatures')[0])
        self.assertEqual('Sent',self.db.one('SELECT contract_status FROM contracts')[0])
        self.assertEqual(410,result['status'])


if __name__ == '__main__':unittest.main()
