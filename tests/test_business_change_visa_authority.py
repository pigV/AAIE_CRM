import unittest
from unittest.mock import patch
from tests import test_visa_status_evidence_authority as fixture
from visa_business_state import formal_lodgement, resolve_case_state
from visa_result_status import record_manual_result,record_auto_result
import visa_documents_candidate as docs
from visa_document_metadata import edit_document_metadata


class VisaAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.VisaStatusEvidenceAuthorityTests();self.f.setUp();self.addCleanup(self.f.tearDown);self.c=self.f.c
        for column,kind in [('actual_lodgement_date','TEXT'),('lodgement_recorded_at','TEXT'),('lodged_by_user_id','INTEGER'),('lodged_by_name','TEXT')]:
            self.c.execute('ALTER TABLE visa_cases ADD COLUMN '+column+' '+kind)

    def case(self):return dict(self.c.execute('SELECT * FROM visa_cases WHERE id=1').fetchone())
    def state(self):return resolve_case_state(self.c,self.case())

    def enable_sync(self):
        from business_changes.schema import install
        self.c.commit();self.c.execute('PRAGMA foreign_keys=ON');self.c.execute('BEGIN');install(self.c)
        self.c.execute('CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT)')
        self.c.execute("INSERT INTO system_settings VALUES('business_change_sync_v1','enabled')")
        self.c.execute("""INSERT INTO application_cycles(id,cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,created_at,updated_at)
            VALUES(7,'SYNTHETIC-C7','S1','initial',1,'active',?,?,?)""",(self.f.actor['occurred_at'],)*3)
        self.c.execute('UPDATE visa_cases SET application_cycle_id=7 WHERE id=1')

    def test_dashboard_join_columns_do_not_duplicate_visa_authority_reads(self):
        from dashboard_read_context import DashboardProjectionContext
        from snapshot_read_cache import read_cache_scope
        from visa_document_lifecycle import case_projection

        row = self.case()
        joined = dict(row, effective_result='Granted', source_event_id=99,
                      review_first_name='Synthetic', review_last_name='Student')
        self.c.commit()
        self.c.execute('PRAGMA query_only=ON')
        self.c.execute('BEGIN')
        with read_cache_scope(self.c), patch('visa_document_lifecycle.case_projection', wraps=case_projection) as project:
            DashboardProjectionContext(self.c)
            expected = resolve_case_state(self.c, row)
            self.assertEqual(expected, resolve_case_state(self.c, joined))
            self.assertEqual(1, project.call_count)
            formally_lodged = dict(joined, actual_lodgement_date='2025-01-20',
                                  lodgement_recorded_at='2025-01-20T10:00:00', lodged_by_user_id=1)
            self.assertTrue(resolve_case_state(self.c, formally_lodged)['formally_lodged'])
            self.assertEqual(2, project.call_count)

    def test_status_label_and_date_alone_are_not_formal_lodgement(self):
        self.c.execute("UPDATE visa_cases SET status='Lodged',actual_lodgement_date='2026-11-19' WHERE id=1")
        self.assertFalse(formal_lodgement(self.case()));self.assertIsNone(self.state()['status'])
        self.c.execute("UPDATE visa_cases SET lodgement_recorded_at='2026-11-19T10:00:00+10:00',lodged_by_user_id=1 WHERE id=1")
        self.assertEqual('lodged',self.state()['status'])

    def test_invalid_or_future_lodgement_date_never_counts(self):
        self.assertFalse(formal_lodgement(dict(actual_lodgement_date='invalid',lodgement_recorded_at='2026-11-19T10:00:00+10:00',lodged_by_user_id=1)))
        self.assertFalse(formal_lodgement(dict(actual_lodgement_date='2099-01-01',lodgement_recorded_at='2026-11-19T10:00:00+10:00',lodged_by_user_id=1)))

    def test_date_only_correction_is_retained_separately_from_an_unconfirmed_grant(self):
        from visa_document_lifecycle import case_projection
        self.enable_sync()
        docs.correct_grant_date(self.c,1,'2025-01-20',**self.f.actor)
        current=case_projection(self.c,1,read_text=lambda document:'')
        self.assertEqual('Pending',current['result_status'])
        self.assertEqual('',current['grant_date'])
        self.assertEqual('2025-01-20',current.get('recorded_grant_date'))
        docs.correct_grant_date(self.c,1,'',**self.f.actor)
        self.assertEqual('',case_projection(self.c,1,read_text=lambda document:'').get('recorded_grant_date'))

    def test_current_material_archive_and_restore_reverse_result(self):
        d=docs.register_document(self.c,1,file_path='/synthetic/new.pdf',original_name='new.pdf',version=1,sha256='d'*64,category='grant_letter',**self.f.actor)
        self.assertEqual('granted',self.state()['status'])
        docs.set_document_archived(self.c,1,d['id'],archived=True,**self.f.actor)
        self.assertIsNone(self.state()['status'])
        docs.set_document_archived(self.c,1,d['id'],archived=False,**self.f.actor)
        self.assertEqual('granted',self.state()['status'])

    def test_explicit_manual_refusal_wins_over_active_grant_material(self):
        docs.register_document(self.c,1,file_path='/synthetic/new.pdf',original_name='new.pdf',version=1,sha256='d'*64,category='grant_letter',**self.f.actor)
        record_manual_result(self.c,1,'Refused',reason='Synthetic result conflict resolved by staff',**self.f.actor)
        self.assertEqual('refused',self.state()['status'])

    def test_guardian_case_never_becomes_student_workflow_fact(self):
        self.c.execute('ALTER TABLE visa_cases ADD COLUMN visa_type TEXT')
        self.c.execute("UPDATE visa_cases SET visa_type='590 Guardian Visa' WHERE id=1")
        record_manual_result(self.c,1,'Refused',reason='Synthetic guardian result',**self.f.actor)
        self.assertIsNone(self.state()['status']);self.assertEqual('Refused',self.state()['result'])

    def test_dashboard_stage_and_kpi_use_same_live_authority_after_void(self):
        from datetime import date
        from dashboard_confirmed_stages import _visa_status
        from dashboard_visa_metrics import load_student_visa_grant_counts
        from visa_document_lifecycle import student_grant_date_map
        self.enable_sync()
        d=docs.register_document(self.c,1,file_path='/synthetic/grant.pdf',original_name='grant.pdf',version=1,sha256='d'*64,category='grant_letter',**self.f.actor)
        before=(self.case()['status'],self.case()['result_status'])
        with patch('visa_cycle_assignment.reconcile_recorded_visa_grants') as reconcile:
            extraction=docs.save_extraction(self.c,1,d['id'],text=fixture.GRANT,**self.f.actor)
            reconcile.assert_not_called()
        self.assertEqual(before,(self.case()['status'],self.case()['result_status']))
        self.assertIsNone(_visa_status(self.case(),self.c))
        self.assertEqual((0,0),load_student_visa_grant_counts(self.c,today=date(2025,1,20)))

        before=(self.case()['status'],self.case()['result_status'])
        with patch('visa_cycle_assignment.reconcile_recorded_visa_grants') as reconcile:
            record_auto_result(self.c,1,d['id'],'Granted',{'deterministic':True,'document_role':'visa_result',
                'document_kind':'grant','extraction_id':extraction['id'],'extraction_method':extraction['extraction_method']},**self.f.actor)
            reconcile.assert_not_called()
        self.assertEqual(before,(self.case()['status'],self.case()['result_status']))
        self.assertFalse(self.state()['requires_review'],'A provisional machine result is not an old staff-confirmed Grant.')
        import json
        docs.confirm_letter(self.c,1,extraction['id'],values=json.loads(extraction['proposed_json']),**self.f.actor)
        self.assertEqual('granted',_visa_status(self.case(),self.c))
        self.assertEqual((1,1),load_student_visa_grant_counts(self.c,today=date(2025,1,20)))
        self.assertEqual({'S1':'2025-01-20'},student_grant_date_map(self.c,('S1',)))
        docs.set_document_archived(self.c,1,d['id'],archived=True,**self.f.actor)
        self.assertIsNone(_visa_status(self.case(),self.c))
        self.assertEqual((0,0),load_student_visa_grant_counts(self.c,today=date(2025,1,20)))
        self.assertEqual({'S1':''},student_grant_date_map(self.c,('S1',)))

    def test_staff_can_confirm_a_corrected_grant_when_machine_extraction_is_unknown(self):
        self.enable_sync()
        d=docs.register_document(self.c,1,file_path='/synthetic/manually-reviewed.pdf',original_name='manually-reviewed.pdf',
            version=1,sha256='f'*64,category='grant_letter',**self.f.actor)
        extraction=docs.save_extraction(self.c,1,d['id'],text='SYNTHETIC unknown extraction',**self.f.actor)
        from visa_result_status import result_summary
        before=result_summary(self.c,1,read_text=lambda document:'')
        self.assertEqual('Pending',before['effective_result']);self.assertIsNone(before['auto_detected_result'])
        docs.confirm_letter(self.c,1,extraction['id'],values={'document_kind':'grant','decision_type':'grant',
            'decision_date':'2025-01-20'},**self.f.actor)
        self.assertEqual('granted',self.state()['status'])

    def test_visa_kpis_do_not_count_uploaded_or_inapplicable_checklist_as_open(self):
        from datetime import date
        from visa_business_state import visa_case_kpis
        self.c.execute('CREATE TABLE visa_checklist_items(id INTEGER,visa_case_id INTEGER,status TEXT)')
        self.c.executemany('INSERT INTO visa_checklist_items VALUES(?,1,?)',[(1,'uploaded'),(2,'not_applicable'),(3,'missing')])
        self.c.execute("UPDATE visa_cases SET archived_at='2026-11-19' WHERE id=2")
        self.assertEqual({'total':1,'active':1,'expiring':0,'checklist_open':1},visa_case_kpis(self.c,today=date(2026,11,19)))

    def test_confirmed_legacy_source_without_case_document_registry_remains_valid(self):
        self.enable_sync()
        source=self.f.linked('Visa Result / S56','IMMI Grant Letter / IMMI Refusal Letter',fixture.GRANT,
            source_type='VisaGrantLetter')
        record=self.c.execute("""INSERT INTO visas(student_id,file_path,visa_grant_date,visa_status,created_at)
            VALUES('S1','/synthetic/legacy-grant.pdf','2025-01-20','granted',?)""",(self.f.actor['occurred_at'],)).lastrowid
        from workflow_visa_events import record_first_valid_visa_decision
        record_first_valid_visa_decision(self.c,7,visa_case_id=1,student_document_id=source,visa_record_id=record,
            decision_type='grant',decision_date='2025-01-20',confirmed_at=self.f.actor['occurred_at'],actor_user_id=1)
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM visa_case_documents').fetchone()[0])
        self.assertEqual('granted',self.state()['status'])
        self.c.execute('UPDATE student_documents SET is_active=0 WHERE id=?',(source,))
        self.assertIsNone(self.state()['status'])
        self.c.execute('UPDATE student_documents SET is_active=1 WHERE id=?',(source,))
        self.assertEqual('granted',self.state()['status'])

    def test_unbound_raw_granted_case_cannot_bypass_shared_authority_in_profile_fallback(self):
        self.enable_sync()
        self.c.execute("UPDATE visa_cases SET status='Granted',result_status='Granted',application_cycle_id=NULL WHERE id=2")
        from student_workflow_projection import _visa_fact_fallbacks
        fallback=_visa_fact_fallbacks(self.c,('S1',))
        self.assertNotEqual('visa_granted',fallback['S1'].stage_key if 'S1' in fallback else None)
        self.assertEqual('visa_case_cycle_unassigned',fallback['S1'].stage_key)
        self.assertEqual('needs_review',fallback['S1'].state)
        self.assertIsNone(fallback['S1'].cycle_id);self.assertIsNone(fallback['S1'].outcome)
        with patch('business_changes.runtime.enabled',return_value=False):
            self.assertEqual('visa_granted',_visa_fact_fallbacks(self.c,('S1',))['S1'].stage_key)
