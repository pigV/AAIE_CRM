import unittest
from unittest.mock import patch

import app
from tests import test_business_change_school_chain as school_fixture


class VisaCaseSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.f=school_fixture.SchoolMaterialChainTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db;self.h=self.f.h
        self.cid=self.db.execute('''INSERT INTO visa_cases(student_id,application_cycle_id,visa_case_name,visa_type,
            applicant_name,application_type,status,result_status,created_by,created_at,updated_at)
            VALUES(?,?,'SYNTHETIC SNAPSHOT','500 Student Visa','SYNTHETIC ONLY','New Application',
            'Information Collection','Pending',1,?,?)''',(self.f.sid,self.f.cycle,self.f.stamp,self.f.stamp))
        self.h.json_response=lambda value,status=200:(status,value)
        self.h.send_html=lambda value,*args,**kwargs:value
        self.h._visa_read_text=lambda *args,**kwargs:(_ for _ in ()).throw(AssertionError('snapshot read a source file'))
        self.query={'case_id':[str(self.cid)],'student_id':[self.f.sid],'visa_tab':['visa_result']}
        item=patch.object(app.nas,'status',return_value={},create=True);item.start();self.addCleanup(item.stop)

    def read(self,surface,**query):
        self.h.query=dict(self.query,surface=[surface],**query)
        return self.h.business_change_snapshot('GET')

    def test_student_case_and_workspace_share_stable_readonly_source_snapshots(self):
        before=self.db.one('SELECT count(*) FROM business_change_events')[0]
        for surface in ('student_visa_case','visa_case_workspace'):
            with self.subTest(surface=surface):
                status,first=self.read(surface);self.assertEqual(200,status,first)
                status,second=self.read(surface);self.assertEqual(200,status,second)
                self.assertEqual(first['etag'],second['etag'])
                self.assertEqual([{'kind':'visa_case','id':str(self.cid),'version':0}],first['versions'])
                html=''.join(first['data']['fragments'].values())
                self.assertIn('data-business-sync-endpoint',html)
                self.assertIn('data-business-sync-fragment',html)
                self.assertTrue('data-preview-dialog' not in html,'preview dialog would be replaced by live refresh')
        self.assertEqual(before,self.db.one('SELECT count(*) FROM business_change_events')[0])

    def test_case_snapshot_checks_exact_owner_version_and_module_rights(self):
        self.assertEqual(404,self.read('student_visa_case',student_id=['FOREIGN'])[0])
        self.assertEqual(409,self.read('visa_case_workspace',minimum_version=['999999'])[0])
        with patch.object(app,'can_access_module',return_value=False):
            self.assertEqual(403,self.read('visa_case_workspace')[0])

    def test_initialized_checklist_get_never_backfills_or_writes(self):
        case=self.db.one('SELECT * FROM visa_cases WHERE id=?',(self.cid,))
        self.h.visa_material_initialize_checklist(case)
        before=tuple(tuple(row) for row in self.db.all('SELECT * FROM visa_checklist_items WHERE visa_case_id=? ORDER BY id',(self.cid,)))
        for surface in ('student_visa_case','visa_case_workspace'):
            status,value=self.read(surface,visa_tab=['material_checklist'])
            self.assertEqual(200,status,value)
        after=tuple(tuple(row) for row in self.db.all('SELECT * FROM visa_checklist_items WHERE visa_case_id=? ORDER BY id',(self.cid,)))
        self.assertEqual(before,after)

    def test_checklist_receipt_forms_stay_stable_until_a_committed_save(self):
        case=self.db.one('SELECT * FROM visa_cases WHERE id=?',(self.cid,))
        self.h.visa_material_initialize_checklist(case)
        fields=self.h.business_change_checklist_form_values(self.cid)
        self.assertEqual(fields,self.h.business_change_checklist_form_values(self.cid))
        item=self.db.one('SELECT id FROM visa_checklist_items WHERE visa_case_id=? ORDER BY id LIMIT 1',(self.cid,))[0]
        self.h.form=fields
        result=self.h.business_change_save_checklist(self.f.sid,self.cid,[(item,'missing','')],self.f.stamp)
        self.assertNotEqual(fields['business_command_key'],result['business_receipt']['business_command_key'])
        self.assertEqual(fields['business_expected_version'],result['business_receipt']['business_expected_version'])
        self.assertEqual(result['business_receipt'],self.h.business_change_checklist_form_values(self.cid))
        self.assertTrue(self.h.business_change_save_checklist(self.f.sid,self.cid,[(item,'missing','')],self.f.stamp)['replayed'])

    def test_legacy_granted_without_current_evidence_has_the_same_review_stage_in_both_projections(self):
        from workflow_projection import load_current_workflow_projection
        from dashboard_confirmed_stages import load_confirmed_cycle_stages
        from student_workflow_projection import load_student_workflow_projection_map
        self.db.execute("UPDATE visa_cases SET status='Granted',result_status='Granted' WHERE id=?",(self.cid,))
        with self.db.read_snapshot() as connection:
            current=load_current_workflow_projection(connection)
            stages=load_confirmed_cycle_stages(connection,current)
            self.assertEqual('visa_result_needs_review',stages[self.f.cycle][0])
            profile=load_student_workflow_projection_map(connection,(self.f.sid,))[self.f.sid]
            self.assertEqual('visa_result_needs_review',profile.stage_key)
        self.assertEqual('Granted',self.db.one('SELECT status FROM visa_cases WHERE id=?',(self.cid,))[0])

    def test_dashboard_distribution_excludes_review_but_retains_separate_issue_and_student_status(self):
        self.db.execute("UPDATE visa_cases SET status='Granted',result_status='Granted' WHERE id=?",(self.cid,))
        self.h.query={'surface':['dashboard_overview']}
        status,packet=self.h.business_change_snapshot('GET')
        self.assertEqual(200,status)
        self.assertNotIn('visa_result_needs_review',packet['data']['counts'])
        self.assertEqual(0,packet['data']['total'])
        self.assertTrue(packet['data']['review_issues'])
        body=packet['data']['fragments']['dashboard-overview']
        self.assertNotIn('data-business-sync-key="visa_result_needs_review"',body)
        self.assertIn('data-business-sync-key="10_visa_submission"',body)
        self.assertIn('签证材料需核实',body)

    def test_initial_case_page_keeps_default_tab_and_seeds_its_source_snapshot(self):
        self.h.query={}
        html=self.h.student_visa_case_detail('GET',self.f.sid,self.cid)
        self.assertTrue('visa_tab=info_collection' in html,'default tab changed in snapshot endpoint')
        self.assertTrue('data-business-sync-initial-snapshot=' in html,'initial source snapshot missing')

    def test_changed_case_source_refreshes_both_views_without_nas_or_ocr(self):
        from visa_processing_dates import correct_processing_dates
        status,before=self.read('student_visa_case');self.assertEqual(200,status,before)
        with self.db.transaction() as conn:
            correct_processing_dates(conn,self.cid,{'medical_completed_date':self.f.stamp[:10]},
                actor_user_id=1,occurred_at=self.f.stamp)
        for surface in ('student_visa_case','visa_case_workspace'):
            status,after=self.read(surface);self.assertEqual(200,status,after)
            self.assertGreater(after['versions'][0]['version'],before['versions'][0]['version'])
            self.assertNotEqual(before['etag'],after['etag'])

    def test_active_grant_views_use_saved_extraction_instead_of_opening_source_files(self):
        import hashlib
        import visa_documents_candidate as docs
        text='IMMI Grant Notification\nDate of Grant: '+self.f.stamp[:10]
        path=self.f.student_root/'synthetic-grant.txt';path.write_text(text)
        with self.db.transaction() as conn:
            document=docs.register_document(conn,self.cid,file_path=str(path),original_name=path.name,version=1,
                sha256=hashlib.sha256(text.encode()).hexdigest(),category='grant_letter',actor_user_id=1,occurred_at=self.f.stamp)
            docs.save_extraction(conn,self.cid,document['id'],text=text,actor_user_id=1,occurred_at=self.f.stamp)
        with patch('visa_document_lifecycle.safe_text',side_effect=AssertionError('polling opened source bytes')):
            for surface in ('student_visa_case','visa_case_workspace'):
                status,value=self.read(surface);self.assertEqual(200,status,value)

    def test_an_old_cycles_review_issue_does_not_replace_a_new_active_cycle_stage(self):
        from application_cycle import create_student_cycle
        from student_workflow_projection import load_student_workflow_projection_map
        from workflow_projection import load_current_workflow_projection
        from dashboard_confirmed_stages import load_confirmed_cycle_stages
        self.db.execute("UPDATE visa_cases SET status='Granted',result_status='Granted' WHERE id=?",(self.cid,))
        self.db.execute("UPDATE application_cycles SET status='closed',outcome='withdrawn',closed_at=? WHERE id=?",(self.f.stamp,self.f.cycle))
        with self.db.transaction() as connection:
            current=create_student_cycle(connection,self.f.sid,'initial',self.f.stamp,1).id
        with self.db.read_snapshot() as connection:
            profile=load_student_workflow_projection_map(connection,(self.f.sid,))[self.f.sid]
            self.assertEqual(current,profile.cycle_id)
            self.assertNotEqual('visa_result_needs_review',profile.stage_key)
            stages=load_confirmed_cycle_stages(connection,load_current_workflow_projection(connection))
            stage=stages.get(current)
            self.assertTrue(stage is None or stage[0]!='visa_result_needs_review')

    def test_unassigned_legacy_grant_has_actionable_review_without_guessing_the_new_cycle(self):
        from application_cycle import create_student_cycle
        from student_workflow_projection import load_student_workflow_projection_map
        from student_management_summary import load_student_management_summary_map
        with self.db.transaction() as connection:
            connection.execute("UPDATE application_cycles SET status='closed',outcome='withdrawn',closed_at=? WHERE id=?",(self.f.stamp,self.f.cycle))
            create_student_cycle(connection,self.f.sid,'initial',self.f.stamp,1,education_stream='higher_education')
            connection.execute("UPDATE visa_cases SET application_cycle_id=NULL,status='Granted',result_status='Granted' WHERE id=?",(self.cid,))
        with self.db.read_snapshot() as connection:
            profile=load_student_workflow_projection_map(connection,(self.f.sid,))[self.f.sid]
            summary=load_student_management_summary_map(connection,(self.f.sid,))[self.f.sid]
            self.assertEqual('visa_case_cycle_unassigned',profile.stage_key)
            self.assertEqual('visa_case_cycle_unassigned',summary.stage_key)
            self.assertIsNone(profile.cycle_id);self.assertEqual('',summary.visa_grant_date)
        notice=self.h.business_change_pending_reviews_html(self.f.sid)
        self.assertIn('待关联申请周期',notice)
        self.assertIn(f'/students/{self.f.sid}/visa-cases/{self.cid}?visa_tab=visa_result',notice)
        status,packet=self.read('student_visa_case')
        self.assertEqual(200,status,packet)
        self.assertIn('data-business-unassigned-visa',''.join(packet['data']['fragments'].values()))
        self.assertIsNone(self.db.one('SELECT application_cycle_id FROM visa_cases WHERE id=?',(self.cid,))[0])

    def test_current_review_does_not_clear_saved_manual_result_or_grant_date_form_defaults(self):
        # A material-backed historical outcome remains reviewable after its
        # source is withdrawn; a genuinely independent staff fact is authoritative.
        import visa_documents_candidate as documents
        with self.db.transaction() as connection:
            source=documents.register_document(connection,self.cid,
                file_path=str(self.f.student_root/'retired-synthetic-grant.pdf'),
                original_name='retired-synthetic-grant.pdf',version=1,sha256='a'*64,
                category='grant_letter',actor_user_id=1,occurred_at=self.f.stamp)
            documents.set_document_archived(connection,self.cid,source['id'],archived=True,
                actor_user_id=1,occurred_at=self.f.stamp)
            connection.execute("UPDATE visa_cases SET status='Granted',result_status='Granted',grant_date='2025-01-20' WHERE id=?",(self.cid,))
            event=connection.execute('''INSERT INTO visa_result_status_events(visa_case_id,event_kind,previous_effective_result,
                effective_result,evidence_json,reason,actor_user_id,occurred_at)
                VALUES(?,'manual','Pending','Granted','{}','SYNTHETIC historical fixture',1,?)''',(self.cid,self.f.stamp)).lastrowid
            connection.execute('''INSERT INTO visa_result_status_current(visa_case_id,effective_result,authority,manual_override,
                conflict,source_event_id,updated_by,updated_at) VALUES(?,'Granted','manual',1,0,?,1,?)''',
                (self.cid,event,self.f.stamp))
        status,packet=self.read('student_visa_case')
        self.assertEqual(200,status,packet)
        html=''.join(packet['data']['fragments'].values())
        self.assertIn('<option selected>Granted</option>',html)
        self.assertIn('name="grant_date" value="2025-01-20"',html)
        self.assertIn('原人工结果记录',html)
        self.assertEqual('2025-01-20',self.db.one('SELECT grant_date FROM visa_cases WHERE id=?',(self.cid,))[0])
