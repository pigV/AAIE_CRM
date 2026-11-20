import base64
from datetime import datetime
import io
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from PIL import Image,ImageDraw
import app
from business_changes.schema import install
from tests.test_parent_contract_completion import ParentCompletionFixture


class ParentSignatureNativeTests(unittest.TestCase):
    def setUp(self):
        self.f=ParentCompletionFixture();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db;self.h=self.f.h
        self.db.execute('DELETE FROM contract_signatures WHERE link_id=?',(self.f.lid,))
        self.db.execute("UPDATE contracts SET contract_status='Sent',final_signed_file_path='' WHERE id=?",(self.f.cid,))
        self.db.execute("UPDATE contract_signing_links SET status='active',signed_signature_id=NULL,signed_at=NULL,expires_at='2099-01-01T00:00:00+11:00' WHERE id=?",(self.f.lid,))
        with self.db.transaction() as conn:
            install(conn);conn.execute("INSERT INTO system_settings(key,value,updated_at) VALUES('business_change_sync_v1','enabled','2026-11-19')")
        self.signing=self.f.root/'signing'
        setup=patch.object(app,'CONTRACT_SIGNING_DIR',self.signing);setup.start();self.addCleanup(setup.stop)
        image=Image.new('RGBA',(80,30),(255,255,255,0));ImageDraw.Draw(image).line((4,24,25,4,44,25,75,8),fill=(0,0,0,255),width=4)
        data=io.BytesIO();image.save(data,format='PNG')
        self.signature_data='data:image/png;base64,'+base64.b64encode(data.getvalue()).decode()
        body=urlencode({'declaration':'yes','signature_data':self.signature_data}).encode()
        self.h.headers.update({'Content-Type':'application/x-www-form-urlencoded','Content-Length':str(len(body)),
                              'User-Agent':'MicroMessenger synthetic source test'})
        self.h.rfile=io.BytesIO(body)

    def test_publication_failure_rolls_back_signature_parent_state_link_and_new_image(self):
        with patch('business_changes.commands._append',side_effect=RuntimeError('synthetic parent publication')),\
                patch.object(self.h,'contract_signing_archive_documents') as archive:
            self.h.contract_signing_submit('POST',self.f.token)
        self.assertEqual(503,self.f.output['status']);archive.assert_not_called()
        self.assertEqual(0,self.db.one('SELECT count(*) FROM contract_signatures')[0])
        self.assertEqual('Sent',self.db.one('SELECT contract_status FROM contracts')[0])
        self.assertEqual('active',self.db.one('SELECT status FROM contract_signing_links')[0])
        self.assertEqual([],list(self.signing.rglob('*.png')))

    def test_archive_failure_keeps_one_parent_step_without_requiring_resigning(self):
        def fail_archive(*args,**kwargs):
            with self.db.transaction() as conn:conn.execute('SELECT 1')
            raise RuntimeError('synthetic archive failure')
        with patch.object(self.h,'contract_signing_archive_documents',side_effect=fail_archive):
            self.h.contract_signing_submit('POST',self.f.token)
        self.assertEqual(202,self.f.output['status']);self.assertIn('无需重新签名',self.f.output['html'])
        self.assertEqual('ParentSigned',self.db.one('SELECT contract_status FROM contracts')[0])
        self.assertEqual(('signed','failed'),tuple(self.db.one('SELECT status,archive_status FROM contract_signatures')))
        self.assertEqual('signed',self.db.one('SELECT status FROM contract_signing_links')[0])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='contract.parent_signed'")[0])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='contract.archive_failed'")[0])
        self.h.contract_signing_submit('POST',self.f.token)
        self.assertEqual(410,self.f.output['status'])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM contract_signatures')[0])

    def test_identity_is_rechecked_after_image_preparation(self):
        original=self.h.contract_signing_save_signature_image
        def rebind(*args,**kwargs):
            path=original(*args,**kwargs)
            self.db.execute("UPDATE contract_signing_links SET bound_wechat_openid='other-parent' WHERE id=?",(self.f.lid,))
            return path
        with patch.object(self.h,'contract_signing_save_signature_image',side_effect=rebind),\
                patch.object(self.h,'contract_signing_archive_documents') as archive:
            self.h.contract_signing_submit('POST',self.f.token)
        self.assertEqual(403,self.f.output['status']);archive.assert_not_called()
        self.assertEqual(0,self.db.one('SELECT count(*) FROM contract_signatures')[0])
        self.assertEqual([],list(self.signing.rglob('*.png')))

    def test_prepared_images_never_overwrite_each_other_in_the_same_second(self):
        with patch.object(app,'datetime') as clock:
            clock.now.return_value=datetime(2026,11,19,12,0,0)
            first=self.h.contract_signing_save_signature_image(self.f.cid,self.signature_data)
            original=first.read_bytes()
            second=self.h.contract_signing_save_signature_image(self.f.cid,self.signature_data)
        self.assertNotEqual(first,second);self.assertEqual(original,first.read_bytes())

    def test_enabled_parent_source_preserves_real_complete_pdf_and_download_flow(self):
        import sys
        from tests import test_contract_master_layout as master
        from tests.test_parent_contract_completion import ParentSubmissionTests
        fixture=ParentSubmissionTests();fixture.setUp()
        try:
            with fixture.db.transaction() as conn:
                install(conn);conn.execute("INSERT INTO system_settings(key,value,updated_at) VALUES('business_change_sync_v1','enabled','2026-11-19')")
            with patch.dict(sys.modules,{'test_contract_master_layout':master}):
                fixture.test_submit_generates_full_pdf_and_reopen_downloads_same_bytes()
            self.assertEqual(1,fixture.db.one("SELECT count(*) FROM business_change_events WHERE event_type='contract.parent_signed'")[0])
            queued=str([tuple(row) for row in fixture.db.all('SELECT * FROM business_change_commands')])
            self.assertNotIn('test-openid',queued);self.assertNotIn(fixture.token,queued)
        finally:fixture.doCleanups()

    def test_archive_publication_failure_keeps_parent_step_and_retry_is_idempotent(self):
        import sys
        from tests import test_contract_master_layout as master
        from tests.test_parent_contract_completion import ParentSubmissionTests
        from business_changes import commands
        fixture=ParentSubmissionTests();fixture.setUp()
        try:
            with fixture.db.transaction() as conn:
                install(conn);conn.execute("INSERT INTO system_settings(key,value,updated_at) VALUES('business_change_sync_v1','enabled','2026-11-19')")
            with patch.dict(sys.modules,{'test_contract_master_layout':master}):fixture.prepare_submission()
            append=commands._append
            def fail_completion(conn,key,mutation,registry,stamp):
                if mutation.event_type=='contract.archive_completed':raise RuntimeError('synthetic archive publication failure')
                return append(conn,key,mutation,registry,stamp)
            with patch.object(commands,'_append',side_effect=fail_completion):fixture.h.contract_signing_submit('POST',fixture.token)
            self.assertEqual(202,fixture.output['status'])
            self.assertEqual('ParentSigned',fixture.db.one('SELECT contract_status FROM contracts')[0])
            self.assertEqual(('signed','failed'),tuple(fixture.db.one('SELECT status,archive_status FROM contract_signatures')))
            self.assertEqual(0,fixture.db.one('SELECT count(*) FROM student_documents')[0])
            self.assertEqual('signed',fixture.db.one('SELECT status FROM contract_signing_links')[0])
            sid=fixture.db.one('SELECT id FROM contract_signatures')[0]
            first=fixture.h.contract_signing_archive_documents(sid)
            self.assertEqual('FullyExecuted',fixture.db.one('SELECT contract_status FROM contracts')[0])
            self.assertEqual(3,fixture.db.one('SELECT count(*) FROM student_documents')[0])
            with patch.object(app,'apply_aaie_signature_and_stamp',side_effect=AssertionError('replay repeated PDF generation')):
                second=fixture.h.contract_signing_archive_documents(sid)
            self.assertEqual(first,second)
            self.assertEqual(3,fixture.db.one('SELECT count(*) FROM student_documents')[0])
            self.assertEqual(1,fixture.db.one("SELECT count(*) FROM business_change_events WHERE event_type='contract.archive_completed'")[0])
        finally:fixture.doCleanups()

    def test_void_during_pdf_generation_never_revives_link_or_publishes_archive(self):
        import sys
        from tests import test_contract_master_layout as master
        from tests.test_parent_contract_completion import ParentSubmissionTests
        fixture=ParentSubmissionTests();fixture.setUp()
        try:
            with fixture.db.transaction() as conn:
                install(conn);conn.execute("INSERT INTO system_settings(key,value,updated_at) VALUES('business_change_sync_v1','enabled','2026-11-19')")
            with patch.dict(sys.modules,{'test_contract_master_layout':master}):fixture.prepare_submission()
            stamp_pdf=app.apply_aaie_signature_and_stamp
            def void_after_pdf(*args,**kwargs):
                result=stamp_pdf(*args,**kwargs)
                fixture.db.execute("UPDATE contracts SET contract_status='Voided' WHERE id=?",(fixture.cid,))
                fixture.db.execute("UPDATE contract_signing_links SET status='revoked',invalidated_at=? WHERE id=?",(app.now_iso(),fixture.lid))
                return result
            with patch.object(app,'apply_aaie_signature_and_stamp',side_effect=void_after_pdf):
                fixture.h.contract_signing_submit('POST',fixture.token)
            self.assertEqual(202,fixture.output['status'])
            self.assertEqual('Voided',fixture.db.one('SELECT contract_status FROM contracts')[0])
            self.assertEqual('revoked',fixture.db.one('SELECT status FROM contract_signing_links')[0])
            self.assertEqual(0,fixture.db.one('SELECT count(*) FROM student_documents')[0])
            self.assertEqual(0,fixture.db.one("SELECT count(*) FROM business_change_events WHERE event_type='contract.archive_completed'")[0])
        finally:fixture.doCleanups()
