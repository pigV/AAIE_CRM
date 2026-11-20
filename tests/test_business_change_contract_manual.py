import unittest
from unittest.mock import patch

import app
from business_changes.schema import install
from tests.test_parent_contract_completion import ParentCompletionFixture


class ManualContractCommandTests(unittest.TestCase):
    def setUp(self):
        self.f=ParentCompletionFixture();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db;self.h=self.f.h;self.cid=self.f.cid
        self.db.execute("UPDATE contracts SET contract_status='Sent',signed_at=NULL,fully_executed_at=NULL,final_signed_file_path='' WHERE id=?",(self.cid,))
        with self.db.transaction() as conn:
            install(conn);conn.execute("INSERT INTO system_settings(key,value,updated_at) VALUES('business_change_sync_v1','enabled','2026-11-19')")
        self.h.user=dict(self.db.one('SELECT u.*,r.name role_name,r.permissions_json FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=1'))
        self.h.csrf='synthetic';self.h.form=dict(self.h.business_change_record_form_values('Contracts','SIGNTEST',self.cid),contract_status='ParentSigned')
        self.h.flash_redirect=lambda location,message,kind='success':(location,message,kind)
        self.h.error_page=lambda message,status=400:(status,message)
        self.h.deny_module_access=lambda module:(403,module)
        self.h.require=lambda permission:True

    def test_center_status_source_audit_notification_and_event_rollback_then_retry_once(self):
        before=self.db.one('SELECT count(*) FROM audit_logs')[0]
        with patch('business_changes.commands._append',side_effect=RuntimeError('status publication failure')):
            with self.assertRaisesRegex(RuntimeError,'status publication failure'):
                self.h.contract_center_status_update('POST',self.cid)
        self.assertEqual('Sent',self.db.one('SELECT contract_status FROM contracts')[0])
        self.assertEqual(before,self.db.one('SELECT count(*) FROM audit_logs')[0])
        first=self.h.contract_center_status_update('POST',self.cid)
        second=self.h.contract_center_status_update('POST',self.cid)
        self.assertEqual('success',first[2]);self.assertEqual('success',second[2])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='contract.updated'")[0])

    def test_student_status_uses_same_source_version_and_rejects_center_stale_edit(self):
        self.h.contract_status_update('POST','SIGNTEST',self.cid)
        stale=dict(self.h.form,business_command_key='synthetic-center-stale-key',contract_status='Sent')
        self.h.form=stale
        result=self.h.contract_center_status_update('POST',self.cid)
        self.assertEqual('error',result[2])
        self.assertEqual('ParentSigned',self.db.one('SELECT contract_status FROM contracts')[0])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='contract.updated'")[0])

    def test_signed_upload_rolls_back_file_document_status_and_audit_then_replays(self):
        self.db.execute("UPDATE students SET nas_status='ready' WHERE student_id='SIGNTEST'")
        upload=app.Upload('file','signed.pdf','application/pdf',self.f.final.read_bytes())
        self.h.uploaded_for=lambda:upload
        folder=self.f.student_root/'Forms';folder.mkdir()
        with patch.object(app,'forms_folder_for_student',return_value=folder):
            before=self.db.one('SELECT count(*) FROM audit_logs')[0]
            with patch('business_changes.commands._append',side_effect=RuntimeError('upload publication failure')):
                with self.assertRaisesRegex(RuntimeError,'upload publication failure'):
                    self.h.contract_center_signed_upload('POST',self.cid)
            self.assertEqual([],list(folder.iterdir()))
            self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])
            self.assertEqual('Sent',self.db.one('SELECT contract_status FROM contracts')[0])
            self.assertEqual(before,self.db.one('SELECT count(*) FROM audit_logs')[0])
            self.h.contract_center_signed_upload('POST',self.cid)
            self.h.contract_center_signed_upload('POST',self.cid)
            self.assertEqual(1,len(list(folder.iterdir())))
            self.assertEqual(1,self.db.one('SELECT count(*) FROM student_documents')[0])
            self.assertEqual('ParentSigned',self.db.one('SELECT contract_status FROM contracts')[0])
            self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='contract.updated'")[0])

    def prepare_upload(self):
        self.db.execute("UPDATE students SET nas_status='ready' WHERE student_id='SIGNTEST'")
        upload=app.Upload('file','signed.pdf','application/pdf',self.f.final.read_bytes())
        self.h.uploaded_for=lambda:upload
        folder=self.f.student_root/'Forms';folder.mkdir()
        return upload,folder

    def test_student_upload_uses_original_permission_and_same_contract_revision(self):
        upload,folder=self.prepare_upload()
        with patch.object(app,'forms_folder_for_student',return_value=folder):
            self.assertEqual('success',self.h.contract_signed_upload('POST','SIGNTEST',self.cid)[2])
            self.assertEqual('success',self.h.contract_signed_upload('POST','SIGNTEST',self.cid)[2])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='contract.updated'")[0])

    def test_standalone_upload_no_student_document_and_uid_commits_with_source(self):
        upload,folder=self.prepare_upload()
        self.db.execute('UPDATE contracts SET student_id=NULL,contract_uid=NULL WHERE id=?',(self.cid,))
        self.h.form=self.h.business_change_record_form_values('Contracts',None,self.cid)
        with patch.object(app,'standalone_contract_folder',return_value=folder):
            with patch('business_changes.commands._append',side_effect=RuntimeError('standalone failure')):
                with self.assertRaisesRegex(RuntimeError,'standalone failure'):
                    self.h.contract_center_signed_upload('POST',self.cid)
            self.assertIsNone(self.db.one('SELECT contract_uid FROM contracts')[0])
            self.assertEqual([],list(folder.iterdir()))
            self.h.contract_center_signed_upload('POST',self.cid)
            self.h.contract_center_signed_upload('POST',self.cid)
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual(1,len(list(folder.iterdir())))
        self.assertTrue(self.db.one('SELECT contract_uid FROM contracts')[0])

    def test_changed_bytes_cannot_reuse_receipt_and_existing_file_remains(self):
        upload,folder=self.prepare_upload()
        with patch.object(app,'forms_folder_for_student',return_value=folder):
            self.h.contract_center_signed_upload('POST',self.cid)
            changed=app.Upload('file','signed.pdf','application/pdf',upload.data+b'\n%new version')
            self.h.uploaded_for=lambda:changed
            result=self.h.contract_center_signed_upload('POST',self.cid)
        self.assertEqual('error',result[2])
        self.assertEqual([upload.data],[path.read_bytes() for path in folder.iterdir()])

    def test_permission_revoked_during_file_preparation_compensates_new_file(self):
        upload,folder=self.prepare_upload()
        def prepare(student):
            self.db.execute('UPDATE users SET is_active=0 WHERE id=?',(self.h.user['id'],))
            return folder
        with patch.object(app,'forms_folder_for_student',side_effect=prepare):
            self.assertEqual(403,self.h.contract_center_signed_upload('POST',self.cid)[0])
        self.assertEqual([],list(folder.iterdir()))
        self.assertEqual('Sent',self.db.one('SELECT contract_status FROM contracts')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])

    def test_final_stamp_publication_failure_rolls_back_all_four_entries_and_retry_renders_once(self):
        from contextlib import ExitStack
        for student_entry,manual in ((False,False),(True,False),(False,True),(True,True)):
            with self.subTest(student_entry=student_entry,manual=manual):
                # Each entry has its own exact source Contract and version.
                cid=self.db.execute('''INSERT INTO contracts(student_id,contract_uid,contract_category,contract_type,contract_title,
                    contract_status,signed_file_path,service_scope_json,generated_at,updated_at)
                    VALUES('SIGNTEST',?,'留学合同','StudyAbroadContract','Synthetic final','ParentSigned',?,'[]','2026-11-19','2026-11-19')''',
                    ('final-'+str(student_entry)+'-'+str(manual),str(self.f.final)))
                self.h.form=self.h.business_change_record_form_values('Contracts','SIGNTEST',cid)
                self.db.execute("UPDATE students SET nas_status='ready' WHERE student_id='SIGNTEST'")
                folder=self.f.student_root/('final-'+str(cid));folder.mkdir()
                assets=[]
                for name in ('company.png','stamp.png'):
                    path=self.f.root/name;path.write_bytes(b'SYNTHETIC');assets.append(path)
                method=(self.h.contract_manual_placement_confirm if manual else self.h.contract_apply_aaie_stamp) if student_entry else (
                    self.h.contract_center_manual_placement_confirm if manual else self.h.contract_center_apply_aaie_stamp)
                args=('POST','SIGNTEST',cid) if student_entry else ('POST',cid)
                def render(source,target,*args,**kwargs):target.write_bytes(source.read_bytes())
                with ExitStack() as stack:
                    stack.enter_context(patch.object(app,'forms_folder_for_student',return_value=folder))
                    stack.enter_context(patch.object(app,'active_contract_asset_paths',return_value=assets))
                    stack.enter_context(patch.object(app,'contract_assets_configured',return_value=True))
                    stack.enter_context(patch.object(app,'contract_stamp_placement',return_value={}))
                    stack.enter_context(patch.object(app,'CONTRACT_SIGNING_DIR',self.f.root/'signing'))
                    stack.enter_context(patch.object(app,'apply_aaie_signature_and_stamp',side_effect=render))
                    stack.enter_context(patch.object(app,'apply_manual_aaie_signature_and_stamp',side_effect=render))
                    marker='synthetic-final-fact-'+str(cid)
                    def completion(*args):
                        self.db.execute('INSERT INTO system_settings(key,value,updated_at) VALUES(?,?,?)',(marker,'complete','2026-11-19'))
                    stack.enter_context(patch.object(app,'record_contract_completion_fact',side_effect=completion))
                    before=self.db.one('SELECT count(*) FROM student_documents')[0]
                    with patch('business_changes.commands._append',side_effect=RuntimeError('final publication failure')):
                        with self.assertRaisesRegex(RuntimeError,'final publication failure'):method(*args)
                    self.assertEqual([],list(folder.iterdir()))
                    self.assertEqual(before,self.db.one('SELECT count(*) FROM student_documents')[0])
                    self.assertEqual('ParentSigned',self.db.one('SELECT contract_status FROM contracts WHERE id=?',(cid,))[0])
                    self.assertIsNone(self.db.one('SELECT value FROM system_settings WHERE key=?',(marker,)))
                    self.assertEqual('success',method(*args)[2])
                    with patch.object(app,'apply_aaie_signature_and_stamp',side_effect=AssertionError('repeated rendering')),\
                         patch.object(app,'apply_manual_aaie_signature_and_stamp',side_effect=AssertionError('repeated rendering')):
                        self.assertEqual('success',method(*args)[2])
                    self.assertEqual(1,len(list(folder.iterdir())))
                    self.assertEqual('complete',self.db.one('SELECT value FROM system_settings WHERE key=?',(marker,))[0])
                    self.assertEqual(before+1,self.db.one('SELECT count(*) FROM student_documents')[0])
                    self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='contract.updated' AND source_id=?",(str(cid),))[0])

    def test_real_manual_and_auto_render_preserve_source_pdf_and_use_snapshotted_assets(self):
        from PIL import Image
        from pypdf import PdfReader
        from contextlib import ExitStack
        self.db.execute("UPDATE students SET nas_status='ready' WHERE student_id='SIGNTEST'")
        assets=[]
        for name,color in (('signature.png',(20,80,180,180)),('seal.png',(200,0,0,120))):
            path=self.f.root/name;Image.new('RGBA',(100,50),color).save(path);assets.append(path)
        folder=self.f.student_root/'Final';folder.mkdir()
        for manual in (False,True):
            with self.subTest(manual=manual),ExitStack() as stack:
                self.db.execute("UPDATE contracts SET contract_status='ParentSigned',signed_file_path=? WHERE id=?",(str(self.f.final),self.cid))
                self.h.form=dict(self.h.business_change_record_form_values('Contracts','SIGNTEST',self.cid),
                    page_number='1',stamp_x='120',stamp_y='120',signature_x='100',signature_y='100',date_x='120',date_y='90')
                stack.enter_context(patch.object(app,'forms_folder_for_student',return_value=folder))
                stack.enter_context(patch.object(app,'active_contract_asset_paths',return_value=assets))
                stack.enter_context(patch.object(app,'contract_assets_configured',return_value=True))
                stack.enter_context(patch.object(app,'CONTRACT_SIGNING_DIR',self.f.root/'signing'))
                stack.enter_context(patch.object(app,'record_contract_completion_fact',return_value=None))
                original=self.f.final.read_bytes()
                method=self.h.contract_center_manual_placement_confirm if manual else self.h.contract_center_apply_aaie_stamp
                self.assertEqual('success',method('POST',self.cid)[2])
                final=self.db.one('SELECT final_signed_file_path FROM contracts WHERE id=?',(self.cid,))[0]
                reader=PdfReader(final)
                self.assertEqual(1,len(reader.pages));self.assertIn('Complete contract fixture',reader.pages[0].extract_text())
                self.assertEqual(original,self.f.final.read_bytes())
