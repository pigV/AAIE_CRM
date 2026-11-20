from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import app
from database import Database
from business_changes.schema import install


class NativeApplicationUploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.folder=self.root/'Applications';self.folder.mkdir()
        self.previews=self.root/'previews';self.previews.mkdir()
        self.db=Database(self.root/'application.db');self.db.initialize()
        with self.db.transaction() as c:
            install(c)
            c.execute("INSERT INTO system_settings(key,value,updated_at) VALUES('business_change_sync_v1','enabled','2026-11-19')")
            c.execute("INSERT INTO students(student_id,passport_first_name,passport_last_name,created_date,updated_at,form_data_json,nas_status) VALUES('S1','Synthetic','Student','2026-11-19','2026-11-19','{}','ready')")
            session=c.execute("INSERT INTO upload_sessions(session_token,student_id,module_key,uploaded_by,uploaded_at) VALUES('synthetic-native','S1','Applications',1,'2026-11-19')").lastrowid
            self.item=c.execute("""INSERT INTO upload_review_items(upload_session_id,student_id,module_key,original_file_name,
                staging_file_path,file_size,uploaded_by,uploaded_at) VALUES(?,'S1','Applications','synthetic.pdf','/synthetic/staging',4,1,'2026-11-19')""",(session,)).lastrowid
        self.student=dict(self.db.one("SELECT * FROM students WHERE student_id='S1'"))
        self.handler=object.__new__(app.CRMHandler)
        self.handler.user={'id':1,'username':'admin','role_name':'Admin'}
        self.handler.transfer_case_flags=lambda: {'read_enabled':True,'progression_write_enabled':True}
        self.preview_calls=0
        def preview(document_id,filename,data,content_type,*,size='thumb'):
            state=self.db._request_state()
            self.assertFalse(state and state.get('connection') and state['connection'].in_transaction,
                             'preview rendering entered the writer transaction')
            self.preview_calls+=1;target=app.preview_target(document_id,size);target.write_bytes(b'synthetic-preview');return str(target)
        for item in (patch.object(app,'db',self.db),patch.object(app,'nas',SimpleNamespace(category_folder=lambda student,category:self.folder)),
                     patch.object(app,'UPLOAD_REVIEW_DIR',self.root/'upload-review'),
                     patch.object(app,'PREVIEW_DIR',self.previews),patch.object(app,'make_preview',side_effect=preview),
                     patch.object(app,'can_review_module_action',return_value=True)):
            item.start();self.addCleanup(item.stop)

    def upload(self):
        return self.handler.business_change_confirm_application_upload(self.student,
            SimpleNamespace(filename='synthetic.pdf',data=b'test',content_type='application/pdf'),
            self.item,'StudentPassport','','Synthetic acceptance',{})

    def test_verified_file_previews_document_and_review_round_trip_once(self):
        did,path,version=self.upload()
        self.assertEqual(b'test',path.read_bytes())
        self.assertEqual('Confirmed',self.db.one('SELECT status FROM upload_review_items')[0])
        self.assertTrue(Path(self.db.one('SELECT preview_path FROM student_documents WHERE id=?',(did,))[0]).is_file())
        self.assertEqual((did,path,version),self.upload())
        self.assertEqual(2,self.preview_calls)
        self.assertEqual(1,len(list(self.folder.iterdir())))
        self.assertEqual(2,len(list(self.previews.iterdir())))

    def test_case_link_failure_compensates_file_previews_and_source(self):
        with patch('upload_review_confirmation_coordinator.coordinate_confirmed_upload',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.upload()
        self.assertEqual([],list(self.folder.iterdir()))
        self.assertEqual([],list(self.previews.iterdir()))
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual('NeedsReview',self.db.one('SELECT status FROM upload_review_items')[0])
        self.assertTrue(self.db.one('SELECT error_message FROM upload_review_items')[0])
        self.upload()
        self.assertEqual('Confirmed',self.db.one('SELECT status FROM upload_review_items')[0])
        self.assertIsNone(self.db.one('SELECT error_message FROM upload_review_items')[0])

    def test_preview_failure_leaves_no_document_or_original_file(self):
        with patch.object(app,'make_preview',side_effect=RuntimeError('synthetic renderer failure')):
            with self.assertRaises(RuntimeError):self.upload()
        self.assertEqual([],list(self.folder.iterdir()))
        self.assertEqual([],list(self.previews.iterdir()))
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])

    def test_replay_rechecks_active_database_actor_instead_of_cached_handler_user(self):
        self.upload()
        self.db.execute('UPDATE users SET is_active=0 WHERE id=1')
        with self.assertRaises(PermissionError):self.upload()
        self.assertEqual(1,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual('Confirmed',self.db.one('SELECT status FROM upload_review_items')[0])

    def test_post_commit_temp_cleanup_failure_keeps_referenced_original_file(self):
        original=Path.unlink
        def fail_prepared(path,*args,**kwargs):
            if path.name.startswith('prepared_'):raise PermissionError('synthetic cleanup failure')
            return original(path,*args,**kwargs)
        with patch.object(Path,'unlink',fail_prepared):
            with self.assertRaises(RuntimeError):self.upload()
        document=self.db.one('SELECT file_path FROM student_documents')
        self.assertIsNotNone(document)
        self.assertEqual(b'test',Path(document['file_path']).read_bytes())
        self.assertEqual('Confirmed',self.db.one('SELECT status FROM upload_review_items')[0])

    def offer(self):
        self.handler.form={};self.handler.client_address=('127.0.0.1',10000);self.handler.headers={}
        def business(view,did,path):
            return self.handler._persist_reviewed_upload_business(student_id='S1',module='Offer',doc_type='Offer',
                upload=SimpleNamespace(filename='synthetic.pdf',data=b'test',content_type='application/pdf'),
                destination=path,document_id=did,index='0',stored_form={},target_name='Synthetic School',
                transfer_branch_snapshot=None,intake=None,intake_upload=None,pending_coe_tuition={},prepared_inferred={})
        return self.handler.business_change_confirm_upload_review(self.student,
            SimpleNamespace(filename='synthetic.pdf',data=b'test',content_type='application/pdf'),
            self.item,'Offer','Synthetic School','Synthetic acceptance',{},module_key='Offer',business_after=business)

    def prepare_offer_item(self):
        self.db.execute("UPDATE upload_sessions SET module_key='Offer'")
        self.db.execute("UPDATE upload_review_items SET module_key='Offer'")

    def test_native_offer_business_record_and_source_file_share_confirmation(self):
        self.prepare_offer_item()
        with patch.object(app,'infer_offer_details',side_effect=AssertionError('inference was not prepared')):
            first=self.offer();second=self.offer()
        self.assertEqual(first,second)
        self.assertEqual(1,self.db.one('SELECT count(*) FROM offers')[0])
        self.assertEqual('Offer',self.db.one('SELECT folder_category FROM student_documents')[0])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='materials.document_confirmed'")[0])

    def test_native_offer_classification_failure_rolls_back_record_file_and_review(self):
        self.prepare_offer_item()
        with patch.object(app,'apply_offer_classification',side_effect=RuntimeError('synthetic classification failure')):
            with self.assertRaises(RuntimeError):self.offer()
        self.assertEqual(0,self.db.one('SELECT count(*) FROM offers')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual([],list(self.folder.iterdir()))
        self.assertEqual([],list(self.previews.iterdir()))

    def test_coe_inference_and_secure_tuition_staging_run_before_writer_transaction(self):
        self.handler.form={'intake_year_0':'2027','intake_grade_0':'Year 8','intake_aud_0':'1200.00'}
        def stage(upload,extensions):
            state=self.db._request_state()
            self.assertFalse(state and state.get('connection') and state['connection'].in_transaction)
            return 'synthetic-prepared-tuition'
        with patch.object(app,'infer_coe_oshc_details',return_value={'synthetic':'proposal'}),patch(
            'revenue.tuition_storage.TuitionEvidenceStorage') as storage:
            storage.return_value.stage.side_effect=stage
            result=self.handler.business_change_prepare_upload_review('COE + OSHC','COE',
                SimpleNamespace(filename='synthetic.pdf',data=b'test',content_type='application/pdf'),'0')
        self.assertEqual({'synthetic':'proposal'},result['inferred'])
        self.assertEqual('synthetic-prepared-tuition',result['tuition_upload'])
        self.assertIsNone(result['tuition_error'])

    def test_coe_secure_staging_failure_is_explicit_pending_finance_information(self):
        self.handler.form={'intake_year_0':'2027','intake_grade_0':'Year 8','intake_aud_0':'1200.00'}
        with patch.object(app,'infer_coe_oshc_details',return_value={}),patch(
            'revenue.tuition_storage.TuitionEvidenceStorage',side_effect=ValueError('synthetic storage unavailable')):
            result=self.handler.business_change_prepare_upload_review('COE + OSHC','COE',
                SimpleNamespace(filename='synthetic.pdf',data=b'test',content_type='application/pdf'),'0')
        self.assertEqual('synthetic storage unavailable',result['tuition_error'])
        self.assertIsNone(result['tuition_upload'])

    def test_visa_letter_is_prepared_once_before_writer_and_empty_text_is_preserved(self):
        self.handler.form={}
        upload=SimpleNamespace(filename='synthetic.txt',data=b'Synthetic Visa letter',content_type='text/plain')
        with patch.object(app,'extract_uploaded_text',side_effect=AssertionError('duplicate extraction')):
            result=self.handler.business_change_prepare_upload_review('Visa','VisaGrantNotice',upload,'0')
        self.assertEqual(upload.data,result['visa_bytes'])
        self.assertEqual('Synthetic Visa letter',result['visa_text'])
        corrupt=SimpleNamespace(filename='synthetic.pdf',data=b'corrupt',content_type='application/pdf')
        result=self.handler.business_change_prepare_upload_review('Visa','VisaGrantNotice',corrupt,'0')
        self.assertEqual('',result['visa_text'])

    def visa_upload(self, *, reassign=False):
        self.db.execute('ALTER TABLE visa_cases ADD COLUMN application_cycle_id INTEGER')
        self.db.execute('ALTER TABLE visa_cases ADD COLUMN lodgement_recorded_at TEXT')
        import re
        root=Path(__file__).resolve().parents[1]
        ddl=(root/'migrations/20260907_035_visa_management_authorities.sql').read_text()
        with self.db.transaction() as conn:
            columns={row[1] for row in conn.execute('PRAGMA table_info(visa_case_document_links)')}
            for name,kind in (('document_category','TEXT'),('document_type','TEXT'),('used_in_this_case','INTEGER')):
                if name not in columns:conn.execute('ALTER TABLE visa_case_document_links ADD COLUMN '+name+' '+kind)
            for table in ('visa_case_documents','visa_document_audit'):
                conn.execute(re.search(r'CREATE TABLE '+table+r'\s*\([\s\S]*?\);',ddl)[0])
        conn=self.db.connect()
        try:conn.executescript((root/'migrations/20260909_037_visa_document_metadata.sql').read_text())
        finally:self.db._release_connection(conn)
        self.db.execute("UPDATE upload_sessions SET module_key='Visa'")
        self.db.execute("UPDATE upload_review_items SET module_key='Visa'")
        stamp=app.now_iso()
        cid=self.db.execute("""INSERT INTO visa_cases(student_id,visa_case_name,visa_type,status,result_status,created_at,updated_at)
            VALUES('S1','Synthetic','500 Student Visa','Information Collection','Pending',?,?)""",(stamp,stamp))
        case=dict(self.db.one('SELECT * FROM visa_cases WHERE id=?',(cid,)))
        self.handler.form={};self.handler.client_address=('127.0.0.1',10000);self.handler.headers={}
        app.nas.student_folder=lambda student:self.root
        def business(view,did,path):
            if reassign:view.execute('UPDATE visa_cases SET archived_at=? WHERE id=?',(stamp,cid))
            return self.handler._persist_reviewed_upload_business(student_id='S1',module='Visa',doc_type='VisaOther',
                upload=SimpleNamespace(filename='synthetic.pdf',data=b'test'),destination=path,document_id=did,
                index='0',stored_form={},target_name='',transfer_branch_snapshot=None,intake=None,intake_upload=None,
                pending_coe_tuition={},visa_review_case=case,prepared_inferred={},owned_connection=view,
                prepared_visa_bytes=b'test',prepared_visa_text='')
        with patch.object(self.handler,'_visa_read_text',side_effect=AssertionError('writer repeated extraction')):
            result=self.handler.business_change_confirm_upload_review(self.student,
                SimpleNamespace(filename='synthetic.pdf',data=b'test',content_type='application/pdf'),self.item,
                'VisaOther','','Synthetic',{},module_key='Visa',business_after=business)
        return result,cid

    def test_visa_review_commits_legacy_record_registered_document_and_shared_event(self):
        (did,path,version),cid=self.visa_upload()
        self.assertEqual(b'test',path.read_bytes())
        self.assertEqual(1,self.db.one('SELECT count(*) FROM visas')[0])
        self.assertEqual(did,self.db.one('SELECT student_document_id FROM visa_case_documents')[0])
        self.assertEqual('Pending',self.db.one('SELECT result_status FROM visa_cases WHERE id=?',(cid,))[0])
        self.assertIsNone(self.db.one('SELECT lodgement_recorded_at FROM visa_cases WHERE id=?',(cid,))[0])

    def test_visa_review_rechecks_case_inside_writer_and_rolls_back_all_outputs(self):
        with self.assertRaisesRegex(ValueError,'Case'):
            self.visa_upload(reassign=True)
        self.assertEqual(0,self.db.one('SELECT count(*) FROM visas')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual([],list(self.folder.iterdir()))
        self.assertEqual('NeedsReview',self.db.one('SELECT status FROM upload_review_items')[0])

    def test_shared_snapshot_never_reextracts_or_backfills_coe_on_read(self):
        path=self.folder/'synthetic-coe.pdf';path.write_bytes(b'synthetic')
        app.nas.student_folder=lambda student:self.root
        record={'id':1,'file_path':str(path),'document_kind':'COE','coe_provider':None,'coe_course':None,
                'coe_course_level':None,'coe_start_date':None,'coe_end_date':None}
        with patch.object(app,'infer_coe_oshc_details',side_effect=AssertionError('GET attempted inference')) as inference:
            self.assertEqual(record,self.handler.backfill_coe_record('S1',record))
            inference.assert_not_called()

    def test_manual_coe_edit_rolls_back_source_and_audit_when_finance_sync_fails(self):
        rid=self.db.execute("INSERT INTO coe_oshc(student_id,document_kind,file_path,created_at) VALUES('S1','OSHC','','2026-11-19')")
        self.handler.user=dict(self.db.one('SELECT u.*,r.name role_name,r.permissions_json FROM users u JOIN roles r ON r.id=u.role_id WHERE u.id=1'))
        self.handler.csrf='synthetic';self.handler.headers={'Accept':'application/json'}
        self.handler.json_response=lambda value,status=200:(status,value)
        self.handler.form=self.handler.business_change_record_form_values('COE + OSHC','S1',rid)
        self.handler.form.update(document_kind='OSHC',oshc_policy_number='SYNTHETIC-EDIT')
        with patch.object(app,'process_revenue_coe_change',side_effect=RuntimeError('synthetic finance failure')):
            with self.assertRaises(RuntimeError):self.handler.coe_update('POST','S1',rid)
        self.assertFalse(self.db.one('SELECT oshc_policy_number FROM coe_oshc WHERE id=?',(rid,))[0])
        self.assertEqual(0,self.db.one("SELECT count(*) FROM audit_logs WHERE action_type='update' AND module_name='coe_oshc'")[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM business_change_events')[0])
        with patch.object(app,'process_revenue_coe_change',return_value=None) as finance:
            response=self.handler.coe_update('POST','S1',rid)
            replay=self.handler.coe_update('POST','S1',rid)
        self.assertEqual(1,finance.call_count)
        self.assertEqual(200,response[0]);self.assertEqual(200,replay[0])
        self.assertEqual('SYNTHETIC-EDIT',self.db.one('SELECT oshc_policy_number FROM coe_oshc WHERE id=?',(rid,))[0])
        self.handler.deny_module_access=lambda module:(403,module)
        self.db.execute('UPDATE users SET is_active=0 WHERE id=1')
        self.assertEqual(403,self.handler.coe_update('POST','S1',rid)[0])

    def create_pending(self):
        return app.create_upload_review('S1','Applications',
            [app.Upload('files','synthetic-pending.txt','text/plain',b'synthetic staged bytes')],self.handler.user,{})

    def test_manual_visa_metadata_save_replays_once_and_rolls_back_audit_on_event_failure(self):
        rid=self.db.execute("INSERT INTO visas(student_id,file_path,visa_type,visa_status,created_at) VALUES('S1','','500 Student Visa','pending','2026-11-19')")
        self.handler.csrf='synthetic';self.handler.headers={'Accept':'application/json'}
        self.handler.client_address=('127.0.0.1',10000)
        self.handler.uploaded_for=lambda:None
        self.handler.json_response=lambda value,status=200:(status,value)
        self.handler.deny_module_access=lambda module:(403,module)
        fields=self.handler.business_change_record_form_values('Visa','S1',rid)
        self.handler.form=dict(fields,visa_type='Synthetic Metadata Update',visa_status='pending')
        before=self.db.one('SELECT count(*) FROM audit_logs')[0]
        with patch('business_changes.commands._append',side_effect=RuntimeError('synthetic publication')):
            with self.assertRaisesRegex(RuntimeError,'synthetic publication'):self.handler.visa_update('POST','S1',rid)
        self.assertEqual('500 Student Visa',self.db.one('SELECT visa_type FROM visas WHERE id=?',(rid,))[0])
        self.assertEqual(before,self.db.one('SELECT count(*) FROM audit_logs')[0])
        first=self.handler.visa_update('POST','S1',rid)
        second=self.handler.visa_update('POST','S1',rid)
        self.assertEqual(200,first[0]);self.assertEqual(200,second[0])
        self.assertIn('business_receipt',first[1]);self.assertEqual(first[1]['business_receipt'],second[1]['business_receipt'])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='visa_record.updated'")[0])

        from business_changes.snapshots import read_scope
        with read_scope(self.db) as conn:
            with patch.object(self.handler,'_persist_reviewed_upload_business',side_effect=AssertionError('unexpected IO')):
                receipt=self.handler.business_change_save_manual_record('Visa','S1',rid,
                    lambda view:(_ for _ in ()).throw(AssertionError('replay repeated operation')),
                    caller_connection=conn)
            self.assertTrue(receipt.replayed)
            self.assertEqual(1,conn.execute('PRAGMA query_only').fetchone()[0])
            self.handler.form=dict(self.handler.form,**first[1]['business_receipt'])
            missing=self.handler.business_change_save_manual_record('Visa','S1',rid,
                lambda view:(_ for _ in ()).throw(AssertionError('lookup executed preparation')),
                caller_connection=conn)
            self.assertIsNone(missing)
            self.assertTrue(conn.in_transaction)
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='visa_record.updated'")[0])

    def test_manual_visa_create_without_file_retries_without_duplicate_source_or_reminder(self):
        self.handler.csrf='synthetic';self.handler.headers={};self.handler.client_address=('127.0.0.1',10000)
        self.handler.uploaded_for=lambda:None
        self.handler.flash_redirect=lambda location,message,kind='success':(location,message,kind)
        self.handler.deny_module_access=lambda module:(403,module)
        self.handler.form=dict(self.handler.business_change_record_form_values('Visa','S1'),
            visa_type='Synthetic Manual Visa',visa_status='pending',visa_expiry_date='31/12/2027')
        before=self.db.one('SELECT count(*) FROM audit_logs')[0]
        with patch('business_changes.commands._append',side_effect=RuntimeError('create publication failure')):
            with self.assertRaisesRegex(RuntimeError,'create publication failure'):self.handler.visa_create('POST','S1')
        self.assertEqual(0,self.db.one('SELECT count(*) FROM visas')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM reminders')[0])
        self.assertEqual(before,self.db.one('SELECT count(*) FROM audit_logs')[0])
        first=self.handler.visa_create('POST','S1');second=self.handler.visa_create('POST','S1')
        self.assertEqual('success',first[2]);self.assertEqual('success',second[2])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM visas')[0])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM reminders')[0])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='visa_record.created'")[0])

    def test_direct_new_visa_file_creates_one_document_record_and_event_with_retry(self):
        self.handler.csrf='synthetic';self.handler.headers={};self.handler.client_address=('127.0.0.1',10000)
        upload=SimpleNamespace(filename='new-visa.txt',data=b'synthetic new visa material',content_type='text/plain')
        self.handler.uploaded_for=lambda:upload
        self.handler.flash_redirect=lambda location,message,kind='success':(location,message,kind)
        self.handler.deny_module_access=lambda module:(403,module)
        self.handler.form=dict(self.handler.business_change_record_form_values('Visa','S1'),visa_type='Synthetic New File Visa',visa_status='pending')
        with patch('business_changes.commands._append',side_effect=RuntimeError('new file publication failure')):
            with self.assertRaisesRegex(RuntimeError,'new file publication failure'):self.handler.visa_create('POST','S1')
        self.assertEqual(0,self.db.one('SELECT count(*) FROM visas')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual([],list(self.folder.iterdir()));self.assertEqual([],list(self.previews.iterdir()))
        first=self.handler.visa_create('POST','S1');previews=self.preview_calls
        with patch.object(self.handler,'business_change_prepare_upload_review',side_effect=AssertionError('replay repeated recognition')):
            second=self.handler.visa_create('POST','S1')
        self.assertEqual('success',first[2]);self.assertEqual('success',second[2]);self.assertEqual(previews,self.preview_calls)
        self.assertEqual(1,self.db.one('SELECT count(*) FROM visas')[0])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='visa_record.created'")[0])
        self.assertEqual(upload.data,Path(self.db.one('SELECT file_path FROM visas')[0]).read_bytes())

    def test_manual_visa_replacement_file_and_source_commit_once_with_failure_compensation(self):
        rid=self.db.execute("INSERT INTO visas(student_id,file_path,visa_type,visa_status,created_at) VALUES('S1','','Original Visa','pending','2026-11-19')")
        self.handler.csrf='synthetic';self.handler.headers={'Accept':'application/json'}
        self.handler.client_address=('127.0.0.1',10000)
        upload=SimpleNamespace(filename='replacement.txt',data=b'synthetic replacement',content_type='text/plain')
        self.handler.uploaded_for=lambda:upload
        self.handler.json_response=lambda value,status=200:(status,value)
        self.handler.deny_module_access=lambda module:(403,module)
        self.handler.form=dict(self.handler.business_change_record_form_values('Visa','S1',rid),
            visa_type='Replacement Visa',visa_status='pending',visa_document_type='other')
        with patch('business_changes.commands._append',side_effect=RuntimeError('replacement publication failure')):
            with self.assertRaisesRegex(RuntimeError,'replacement publication failure'):self.handler.visa_update('POST','S1',rid)
        self.assertEqual(('Original Visa',''),tuple(self.db.one('SELECT visa_type,file_path FROM visas WHERE id=?',(rid,))))
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual([],list(self.folder.iterdir()));self.assertEqual([],list(self.previews.iterdir()))
        first=self.handler.visa_update('POST','S1',rid)
        previews=self.preview_calls
        second=self.handler.visa_update('POST','S1',rid)
        self.assertEqual(200,first[0]);self.assertEqual(200,second[0]);self.assertEqual(previews,self.preview_calls)
        self.assertEqual(first[1]['business_receipt'],second[1]['business_receipt'])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='visa_record.updated'")[0])
        path=Path(self.db.one('SELECT file_path FROM visas WHERE id=?',(rid,))[0])
        self.assertEqual(upload.data,path.read_bytes())
        document_id=self.db.one('SELECT id FROM student_documents')[0]
        self.assertIsNotNone(self.db.one("SELECT 1 FROM business_change_impacts WHERE subject_kind='document' AND subject_id=?",(str(document_id),)))
        upload.data=b'changed file with same save key'
        response=self.handler.visa_update('POST','S1',rid)
        self.assertEqual(409,response[0]);self.assertEqual(1,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.handler.form=dict(self.handler.form,**first[1]['business_receipt'])
        permitted=[True];prepare=self.handler.business_change_prepare_upload_review
        def revoke_after_preparation(*args,**kwargs):
            result=prepare(*args,**kwargs);permitted[0]=False;return result
        with patch.object(app,'can_save_visa_record',return_value=True),patch.object(app,'allowed',return_value=False),\
                patch.object(app,'can_module_action',side_effect=lambda *args:permitted[0]),\
                patch.object(self.handler,'business_change_prepare_upload_review',side_effect=revoke_after_preparation):
            response=self.handler.visa_update('POST','S1',rid)
        self.assertEqual(403,response[0]);self.assertTrue(path.is_file())
        self.assertEqual(1,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual(1,len(list(self.folder.iterdir())))

    def existing_visa_pending(self):
        self.db.execute('''CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT,cycle_type TEXT,status TEXT)''')
        self.db.execute("INSERT INTO application_cycles VALUES(7,'S1','transfer','active')")
        self.db.execute('''CREATE TABLE lifecycle_module_disposition_events(id INTEGER PRIMARY KEY,event_uid TEXT UNIQUE,
            application_cycle_id INTEGER,module_key TEXT,disposition TEXT,reason TEXT,source_record_type TEXT,
            source_record_id INTEGER,actor_user_id INTEGER,occurred_at TEXT,created_at TEXT)''')
        self.db.execute('ALTER TABLE visa_cases ADD COLUMN legacy_visa_id INTEGER')
        self.db.execute('ALTER TABLE visa_cases ADD COLUMN application_cycle_id INTEGER')
        context={'visa_record_purpose':'existing_evidence','visa_type':'500 Student Visa','visa_status':'granted'}
        token=app.create_upload_review('S1','Visa',[app.Upload('files','existing.txt','text/plain',b'synthetic existing visa')],self.handler.user,context)
        _,metadata=app.load_upload_review(token)
        self.handler.form={'confirm_item_0':'1','document_type_0':'VisaGrantLetter'}
        self.handler.client_address=('127.0.0.1',10000);self.handler.headers={}
        self.handler.flash_redirect=lambda location,message,kind='success':(location,message,kind)
        return token,metadata

    def test_existing_visa_review_uses_frozen_lifecycle_without_creating_or_linking_visa_case(self):
        token,metadata=self.existing_visa_pending()
        self.assertEqual('7',metadata['form']['business_existing_visa_cycle_id'])
        self.handler.visa_lodgement_resolve_target_case=lambda *args:(_ for _ in ()).throw(AssertionError('existing evidence resolved new Visa Case'))
        result=self.handler.upload_review_confirm('POST','S1',token)
        self.assertEqual('success',result[2])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM visa_cases')[0])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM visas')[0])
        self.assertEqual((7,'visa','existing'),tuple(self.db.one('SELECT application_cycle_id,module_key,disposition FROM lifecycle_module_disposition_events')))
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='materials.document_confirmed'")[0])
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='lifecycle.module_policy_changed'")[0])

    def test_existing_visa_review_cannot_move_to_another_lifecycle_after_upload(self):
        token,metadata=self.existing_visa_pending()
        self.db.execute("UPDATE application_cycles SET status='closed' WHERE id=7")
        self.db.execute("INSERT INTO application_cycles VALUES(9,'S1','progression','active')")
        result=self.handler.upload_review_confirm('POST','S1',token)
        self.assertEqual('error',result[2]);self.assertIn(token,result[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM visas')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM lifecycle_module_disposition_events')[0])

    def test_existing_visa_cycle_is_rechecked_after_preparation_inside_atomic_writer(self):
        token,metadata=self.existing_visa_pending()
        prepare=self.handler.business_change_prepare_upload_review
        def close_after_prepare(*args,**kwargs):
            result=prepare(*args,**kwargs)
            self.db.execute("UPDATE application_cycles SET status='closed' WHERE id=7")
            return result
        with patch.object(self.handler,'business_change_prepare_upload_review',side_effect=close_after_prepare):
            result=self.handler.upload_review_confirm('POST','S1',token)
        self.assertEqual('error',result[2])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM visas')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM lifecycle_module_disposition_events')[0])
        self.assertEqual([],list(self.folder.iterdir()))
        self.assertEqual(0,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='materials.document_confirmed'")[0])
        item=self.db.one('SELECT status,error_message FROM upload_review_items WHERE upload_session_id=?',(metadata['session_id'],))
        self.assertEqual('NeedsReview',item[0]);self.assertTrue(item[1])

    def test_partial_batch_failure_returns_to_review_with_committed_count_and_retry(self):
        token=app.create_upload_review('S1','Applications',[
            app.Upload('files',name,'text/plain',b'synthetic '+name.encode())
            for name in ('first.txt','second.txt')],self.handler.user,{})
        self.handler.form={'confirm_item_0':'1','document_type_0':'StudentPassport',
                           'confirm_item_1':'1','document_type_1':'BirthCertificate'}
        self.handler.client_address=('127.0.0.1',10000);self.handler.headers={}
        self.handler.flash_redirect=lambda location,message,kind='success':(location,message,kind)
        original=app.make_preview
        def fail_second(*args,**kwargs):
            if self.preview_calls>=2:raise OSError('synthetic private storage failure')
            return original(*args,**kwargs)
        with patch.object(app,'make_preview',side_effect=fail_second):
            location,message,kind=self.handler.upload_review_confirm('POST','S1',token)
        self.assertIn(token,location);self.assertEqual('error',kind)
        self.assertIn('已保存 1 份',message);self.assertNotIn('private storage',message)
        folder,metadata=app.load_upload_review(token)
        rows=self.db.all('SELECT status FROM upload_review_items WHERE upload_session_id=? ORDER BY id',(metadata['session_id'],))
        self.assertEqual(['Confirmed','NeedsReview'],[row[0] for row in rows])
        self.handler.upload_review_confirm('POST','S1',token)
        self.assertEqual(2,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual('Confirmed',self.db.one('SELECT status FROM upload_sessions WHERE id=?',(metadata['session_id'],))[0])

    def test_pending_native_file_manifest_and_shared_event_round_trip_without_approval(self):
        token=self.create_pending();folder,metadata=app.load_upload_review(token)
        self.assertEqual(b'synthetic staged bytes',app.upload_from_review(folder,metadata['files'][0]).data)
        self.assertEqual(1,metadata.get('business_sync_version'))
        self.assertEqual(1,self.db.one("SELECT count(*) FROM business_change_events WHERE event_type='materials.review_started'")[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])

    def test_pending_publication_failure_compensates_whole_folder_and_source(self):
        with patch('business_changes.commands._append',side_effect=RuntimeError('synthetic publication failure')):
            with self.assertRaises(RuntimeError):self.create_pending()
        self.assertEqual(1,self.db.one('SELECT count(*) FROM upload_sessions')[0])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM upload_review_items')[0])
        self.assertEqual([],list(app.UPLOAD_REVIEW_DIR.iterdir()))

    def test_pending_metadata_failure_does_not_leave_partial_session_or_staging_bytes(self):
        original=Path.open
        def fail_metadata(path,*args,**kwargs):
            if path.name=='review.json':raise OSError('synthetic metadata failure')
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',fail_metadata):
            with self.assertRaises(OSError):self.create_pending()
        self.assertEqual(1,self.db.one('SELECT count(*) FROM upload_sessions')[0])
        self.assertEqual([],list(app.UPLOAD_REVIEW_DIR.iterdir()))

    def test_pending_file_or_manifest_change_is_rejected_before_preview_or_confirmation(self):
        import hashlib,json
        token=self.create_pending();folder,metadata=app.load_upload_review(token);item=metadata['files'][0]
        data=b'changed staged content';self.assertEqual(len(b'synthetic staged bytes'),len(data))
        (folder/item['blob']).write_bytes(data)
        with self.assertRaises(ValueError):app.upload_from_review(folder,item)
        item['sha256']=hashlib.sha256(data).hexdigest()
        (folder/'review.json').write_text(json.dumps(metadata))
        with self.assertRaises(ValueError):app.load_upload_review(token)
        metadata.pop('business_sync_version');metadata['token']='synthetic-fake-legacy-token'
        (folder/'review.json').write_text(json.dumps(metadata))
        with self.assertRaises(ValueError):app.load_upload_review(token)

    def test_pending_rechecks_actor_after_staging_bytes_are_prepared(self):
        from business_changes.files import persist_verified_file
        def revoke(path,data,save):
            result=persist_verified_file(path,data,save)
            if Path(path).name=='0.bin':self.db.execute('UPDATE users SET is_active=0 WHERE id=1')
            return result
        with patch('business_changes.upload_staging.persist_verified_file',side_effect=revoke):
            with self.assertRaises(PermissionError):self.create_pending()
        self.assertEqual(1,self.db.one('SELECT count(*) FROM upload_sessions')[0])
        self.assertEqual([],list(app.UPLOAD_REVIEW_DIR.iterdir()))

    def test_pending_second_file_failure_compensates_prepared_first_file_too(self):
        from business_changes.files import persist_verified_file
        def fail_second(path,data,save):
            if Path(path).name=='1.bin':raise OSError('synthetic second-file failure')
            return persist_verified_file(path,data,save)
        uploads=[app.Upload('files',name,'text/plain',b'synthetic') for name in ('first.txt','second.txt')]
        with patch('business_changes.upload_staging.persist_verified_file',side_effect=fail_second):
            with self.assertRaises(OSError):app.create_upload_review('S1','Applications',uploads,self.handler.user,{})
        self.assertEqual(1,self.db.one('SELECT count(*) FROM upload_sessions')[0])
        self.assertEqual([],list(app.UPLOAD_REVIEW_DIR.iterdir()))

    def test_pending_zip_uses_real_archive_guards_and_keeps_only_verified_manifest_files(self):
        import io,zipfile
        from types import MethodType
        from nas import NasManager,NasSafetyError
        app.nas.safe_zip_members=MethodType(NasManager.safe_zip_members,app.nas)
        def archive(files):
            output=io.BytesIO()
            with zipfile.ZipFile(output,'w') as package:
                for name,data in files:package.writestr(name,data)
            return app.Upload('files','synthetic.zip','application/zip',output.getvalue())
        token=app.create_upload_review('S1','Applications',[archive([('first.txt',b'first'),('nested/second.txt',b'second'),
            ('__MACOSX/hidden.txt',b'ignored'),('._hidden.txt',b'ignored')])],self.handler.user,{})
        folder,metadata=app.load_upload_review(token)
        self.assertEqual({'first.txt','second.txt'},{item['original_name'] for item in metadata['files']})
        self.assertEqual({b'first',b'second'},{app.upload_from_review(folder,item).data for item in metadata['files']})
        with self.assertRaises(NasSafetyError):
            app.create_upload_review('S1','Applications',[archive([('../escape.txt',b'unsafe')])],self.handler.user,{})
        self.assertFalse((self.root/'escape.txt').exists())
        self.assertEqual([folder],list(app.UPLOAD_REVIEW_DIR.iterdir()))

    def test_pending_preserves_entry_specific_upload_permission_and_ignores_forged_form_policy(self):
        upload=app.Upload('files','synthetic-permission.txt','text/plain',b'synthetic')
        with patch.object(app,'can_review_module_action',return_value=False),patch.object(app,'can_workflow_module_save',return_value=True):
            with self.assertRaises(PermissionError):
                app.create_upload_review('S1','Applications',[upload],self.handler.user,{'permission_policy':'workflow_save'})
            token=app.create_upload_review('S1','Offer',[upload],self.handler.user,{},permission_policy='workflow_save')
        folder,metadata=app.load_upload_review(token)
        self.assertEqual('Offer',metadata['module'])
        self.assertEqual([folder],list(app.UPLOAD_REVIEW_DIR.iterdir()))

    def signed_form(self,context):
        self.handler.form={};self.handler.client_address=('127.0.0.1',10000);self.handler.headers={}
        def business(view,did,path):
            return self.handler._persist_reviewed_upload_business(student_id='S1',module='Forms',
                doc_type='SignedStudyAbroadContract',upload=SimpleNamespace(filename='synthetic.pdf',data=b'test'),
                destination=path,document_id=did,index='0',stored_form=context,target_name='',
                transfer_branch_snapshot=None,intake=None,intake_upload=None,pending_coe_tuition={},
                prepared_inferred={},strict_contract_owner=True)
        return self.handler.business_change_confirm_upload_review(self.student,
            SimpleNamespace(filename='synthetic.pdf',data=b'test',content_type='application/pdf'),self.item,
            'SignedStudyAbroadContract','','Synthetic signed file',context,module_key='Forms',business_after=business)

    def prepare_contract_item(self):
        self.db.execute("UPDATE upload_sessions SET module_key='Forms'")
        self.db.execute("UPDATE upload_review_items SET module_key='Forms'")
        return self.db.execute("INSERT INTO contracts(student_id,contract_category,contract_type,contract_title,contract_status,updated_at) VALUES('S1','Synthetic','StudyAbroadContract','Synthetic target','Draft','2026-11-19')")

    def test_signed_contract_file_requires_explicit_target_and_cannot_revive_voided_contract(self):
        cid=self.prepare_contract_item()
        with self.assertRaises(ValueError):self.signed_form({})
        self.db.execute("UPDATE contracts SET contract_status='Voided' WHERE id=?",(cid,))
        with self.assertRaises(ValueError):self.signed_form({'contract_id':str(cid)})
        self.assertEqual('Voided',self.db.one('SELECT contract_status FROM contracts')[0])
        self.assertEqual(0,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertEqual([],list(self.folder.iterdir()))

    def test_parent_signed_file_status_audit_and_contract_revision_commit_together(self):
        cid=self.prepare_contract_item()
        first=self.signed_form({'contract_id':str(cid)});second=self.signed_form({'contract_id':str(cid)})
        self.assertEqual(first,second)
        self.assertEqual('ParentSigned',self.db.one('SELECT contract_status FROM contracts')[0])
        self.assertEqual(1,self.db.one('SELECT count(*) FROM student_documents')[0])
        self.assertIsNotNone(self.db.one("SELECT 1 FROM business_change_impacts WHERE subject_kind='contract' AND subject_id=?",(str(cid),)))
