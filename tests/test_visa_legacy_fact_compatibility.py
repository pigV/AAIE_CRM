"""Frozen-production evidence shapes, recreated as isolated in-memory rows.

Sources: cases 28/13/31 manual events 1/3/5; canonical case40/61/54
rows5/4/3 with NULL visa_record_id and exact active Student Grant source.
No application import, source snapshot mutation, file access or network.
"""
import json
import unittest
from tests import test_business_change_visa_authority as authority
from tests import test_visa_status_evidence_authority as material
from visa_business_state import resolve_case_state
import visa_documents_candidate as documents


class LegacyVisaFactCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.f=authority.VisaAuthorityTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.enable_sync();self.c=self.f.c
        self.assertNotIn('app',__import__('sys').modules)

    def state(self):return resolve_case_state(self.c,self.f.case())

    def manual(self,event_id=1,stamp='2026-09-16T11:46:29+10:00',*,document_id=None,actor=1,event_result="Granted"):
        self.c.execute("UPDATE visa_cases SET status='Granted',result_status='Granted' WHERE id=1")
        self.c.execute("INSERT INTO visa_result_status_events(id,visa_case_id,document_id,event_kind,previous_effective_result,effective_result,conflict,evidence_json,reason,actor_user_id,occurred_at) VALUES(?,1,?,'manual','Pending',?,0,'{}','There is an IMMI grant letter.',?,?)",(event_id,document_id,event_result,actor,stamp))
        self.c.execute("INSERT INTO visa_result_status_current(visa_case_id,effective_result,authority,manual_override,conflict,source_event_id,updated_by,updated_at) VALUES(1,'Granted','manual',1,0,?,1,?)",(event_id,stamp))

    def test_case28_event1_independent_manual_grant_survives_missing_registered_letter(self):
        self.manual(1);self.assertEqual('granted',self.state()['status'])

    def test_case13_event3_independent_manual_grant_survives_missing_registered_letter(self):
        self.manual(3,'2026-09-24T10:49:00+10:00');self.assertEqual('granted',self.state()['status'])

    def test_case31_event5_independent_manual_grant_survives_missing_registered_letter(self):
        self.manual(5,'2026-09-24T11:34:16+10:00');self.assertEqual('granted',self.state()['status'])

    def test_manual_current_without_exact_event_is_not_independent(self):
        self.manual();self.c.execute('UPDATE visa_result_status_current SET source_event_id=NULL WHERE visa_case_id=1')
        self.assertIsNone(self.state()['status'])

    def test_manual_event_must_agree_with_current_grant(self):
        self.manual(event_result="Pending")
        self.assertIsNone(self.state()['status'])

    def test_document_bound_manual_is_not_independent(self):
        d=documents.register_document(self.c,1,file_path='/synthetic/unrelated.pdf',original_name='other.pdf',version=1,sha256='a'*64,**self.f.f.actor)
        self.manual(document_id=d['id'])
        self.assertIsNone(self.state()['status'])

    def test_null_document_manual_shadow_cannot_revive_archived_grant(self):
        d=documents.register_document(self.c,1,file_path='/synthetic/grant.pdf',original_name='grant.pdf',version=1,sha256='a'*64,category='grant_letter',**self.f.f.actor)
        self.manual();documents.set_document_archived(self.c,1,d['id'],archived=True,**self.f.f.actor)
        self.assertIsNone(self.state()['status'])

    def canonical(self,event_id=5):
        source=self.f.f.linked('Visa Result / S56','IMMI Grant Letter',material.GRANT,source_type='VisaGrantLetter')
        d=documents.register_document(self.c,1,student_document_id=source,file_path=f'/synthetic/{source}.pdf',original_name=f'{source}.pdf',version=1,sha256='b'*64,**self.f.f.actor)
        x=documents.save_extraction(self.c,1,d['id'],text=material.GRANT,**self.f.f.actor)
        self.c.execute("INSERT INTO visa_decision_events(id,application_cycle_id,student_id,visa_case_id,student_document_id,visa_record_id,decision_type,decision_date,confirmed_at,decision_source,recorded_by_name) VALUES(?,7,'S1',1,?,NULL,'grant','2025-01-20',?,'legacy_document','Codex (operator-approved backfill)')",(event_id,source,self.f.f.actor['occurred_at']))
        self.c.execute("UPDATE application_cycles SET status='closed',outcome='granted' WHERE id=7")
        return source,d,x

    def test_case40_event5_canonical_active_source_does_not_require_visas_row(self):
        self.canonical(5);self.assertEqual('granted',self.state()['status'])

    def test_case61_event4_canonical_active_source_does_not_require_visas_row(self):
        self.canonical(4);self.assertEqual('granted',self.state()['status'])

    def test_case54_event3_canonical_active_source_does_not_require_visas_row(self):
        self.canonical(3);self.assertEqual('granted',self.state()['status'])

    def test_canonical_source_requires_confirmation_timestamp(self):
        self.canonical();self.c.execute("UPDATE visa_decision_events SET confirmed_at='' WHERE id=5")
        self.assertIsNone(self.state()['status'])

    def test_canonical_source_requires_staff_actor(self):
        self.canonical();self.c.execute("UPDATE visa_decision_events SET recorded_by_name=NULL,recorded_by_user_id=NULL WHERE id=5")
        self.assertIsNone(self.state()['status'])

    def test_canonical_source_must_match_cycle_and_student(self):
        self.canonical();self.c.execute('UPDATE visa_decision_events SET application_cycle_id=8 WHERE id=5')
        self.assertIsNone(self.state()['status'])

    def test_canonical_source_archive_still_revokes_grant(self):
        source,d,x=self.canonical();documents.set_document_archived(self.c,1,d['id'],archived=True,**self.f.f.actor)
        self.assertIsNone(self.state()['status'])

    def test_canonical_student_source_inactive_still_revokes_grant(self):
        source,d,x=self.canonical();self.c.execute('UPDATE student_documents SET is_active=0 WHERE id=?',(source,))
        self.assertIsNone(self.state()['status'])

    def test_canonical_retired_extraction_still_revokes_grant(self):
        source,d,x=self.canonical()
        self.c.execute('CREATE TABLE audit_logs(module_name TEXT,action_type TEXT,record_id TEXT,new_value TEXT)')
        self.c.execute("INSERT INTO audit_logs VALUES('visa_management','visa_letter_extraction_retired',?,?)",(str(x['id']),json.dumps({'visa_case_id':1})))
        self.assertIsNone(self.state()['status'])

    def legacy_manual_source(self,event_id=3,*,registered=False):
        source=self.f.f.linked('Visa Result / S56','IMMI Grant Letter / IMMI Refusal Letter',material.GRANT,source_type='VisaMaterialChecklist')
        d=None
        if registered:
            d=documents.register_document(self.c,1,student_document_id=source,file_path=f'/synthetic/{source}.pdf',original_name=f'{source}.pdf',version=1,sha256='c'*64,**self.f.f.actor)
            from visa_document_metadata import edit_document_metadata
            edit_document_metadata(self.c,1,d['id'],category='grant_letter',**self.f.f.actor)
        self.manual(event_id)
        return source,d

    def test_case28_real_generic_checklist_registered_manual_category_and_event(self):
        self.legacy_manual_source(1,registered=True);self.assertEqual('granted',self.state()['status'])

    def test_case13_real_generic_checklist_combined_slot_and_manual_event(self):
        self.legacy_manual_source(3);self.assertEqual('granted',self.state()['status'])

    def test_case31_real_generic_checklist_combined_slot_and_manual_event(self):
        self.legacy_manual_source(5);self.assertEqual('granted',self.state()['status'])

    def test_manual_combined_slot_removed_source_cannot_grant(self):
        self.legacy_manual_source();self.c.execute("UPDATE visa_case_document_links SET status='Removed' WHERE visa_case_id=1")
        self.assertIsNone(self.state()['status'])

    def test_manual_combined_slot_unused_source_cannot_grant(self):
        self.legacy_manual_source();self.c.execute('UPDATE visa_case_document_links SET used_in_this_case=0 WHERE visa_case_id=1')
        self.assertIsNone(self.state()['status'])

    def test_manual_combined_slot_inactive_source_cannot_grant(self):
        self.legacy_manual_source();self.c.execute('UPDATE student_documents SET is_active=0')
        self.assertIsNone(self.state()['status'])

    def test_manual_combined_slot_voided_source_cannot_grant(self):
        self.legacy_manual_source();self.c.execute("UPDATE student_documents SET document_status='voided'")
        self.assertIsNone(self.state()['status'])

    def test_manual_combined_slot_voided_at_alone_cannot_grant(self):
        self.legacy_manual_source();self.c.execute('ALTER TABLE student_documents ADD COLUMN voided_at TEXT')
        self.c.execute("UPDATE student_documents SET voided_at='2026-11-20'")
        self.assertIsNone(self.state()['status'])

    def test_manual_combined_slot_archived_registered_source_cannot_grant(self):
        source,d=self.legacy_manual_source(registered=True)
        documents.set_document_archived(self.c,1,d['id'],archived=True,**self.f.f.actor)
        self.assertIsNone(self.state()['status'])

    def test_manual_combined_slot_cross_student_source_cannot_grant(self):
        self.legacy_manual_source();self.c.execute("INSERT INTO students(student_id) VALUES('S2')")
        self.c.execute("UPDATE student_documents SET student_id='S2'")
        self.assertIsNone(self.state()['status'])

    def test_manual_combined_slot_cross_case_event_cannot_grant(self):
        self.legacy_manual_source()
        self.c.execute("INSERT INTO visa_result_status_events(visa_case_id,event_kind,effective_result,previous_effective_result,conflict,evidence_json,reason,actor_user_id,occurred_at) VALUES(2,'manual','Granted','Pending',0,'{}','Another case',1,?)",('2026-09-16T11:46:29+10:00',))
        other=self.c.execute('SELECT max(id) FROM visa_result_status_events').fetchone()[0]
        self.c.execute('UPDATE visa_result_status_current SET source_event_id=? WHERE visa_case_id=1',(other,))
        self.assertIsNone(self.state()['status'])

    def test_combined_slot_without_manual_decision_is_not_a_grant(self):
        self.f.f.linked('Visa Result / S56','IMMI Grant Letter / IMMI Refusal Letter',material.GRANT,source_type='VisaMaterialChecklist')
        self.assertIsNone(self.state()['status'])

    def test_old_manual_cannot_grant_new_generic_refusal_after_source_replacement(self):
        old,_=self.legacy_manual_source()
        self.assertEqual('Granted',self.state()['result'])
        self.c.execute('UPDATE student_documents SET is_active=0 WHERE id=?',(old,))
        self.assertEqual('Pending',self.state()['result'])
        new=self.f.f.linked('Visa Result / S56','IMMI Grant Letter / IMMI Refusal Letter','IMMI Refusal Notification',source_type='VisaMaterialChecklist')
        self.c.execute("UPDATE student_documents SET uploaded_at='2026-11-20T10:00:00+11:00' WHERE id=?",(new,))
        self.assertEqual('Pending',self.state()['result'])

    def test_old_manual_cannot_confirm_new_typed_grant_after_source_replacement(self):
        old,_=self.legacy_manual_source()
        self.c.execute('UPDATE student_documents SET is_active=0 WHERE id=?',(old,))
        new=self.f.f.linked('Visa Result / S56','IMMI Grant Letter',material.GRANT,source_type='VisaGrantLetter')
        self.c.execute("UPDATE student_documents SET uploaded_at='2026-11-20T10:00:00+11:00' WHERE id=?",(new,))
        self.assertEqual('Pending',self.state()['result'])

    def test_old_manual_cannot_confirm_old_upload_newly_linked_to_case(self):
        old,_=self.legacy_manual_source()
        self.c.execute('UPDATE student_documents SET is_active=0 WHERE id=?',(old,))
        new=self.f.f.linked('Visa Result / S56','IMMI Grant Letter / IMMI Refusal Letter',material.GRANT,source_type='VisaMaterialChecklist')
        self.c.execute('ALTER TABLE visa_case_document_links ADD COLUMN created_at TEXT')
        self.c.execute("UPDATE visa_case_document_links SET created_at='2026-11-20T10:00:00+11:00' WHERE student_document_id=?",(new,))
        self.assertEqual('Pending',self.state()['result'])

    def test_legacy_manual_does_not_confirm_source_with_unknown_upload_time(self):
        source,_=self.legacy_manual_source()
        self.c.execute('UPDATE student_documents SET uploaded_at=NULL WHERE id=?',(source,))
        self.assertEqual('Pending',self.state()['result'])

    def test_old_manual_cannot_confirm_later_reclassified_link(self):
        source,_=self.legacy_manual_source()
        self.c.execute('ALTER TABLE visa_case_document_links ADD COLUMN created_at TEXT')
        self.c.execute('ALTER TABLE visa_case_document_links ADD COLUMN updated_at TEXT')
        self.c.execute("UPDATE visa_case_document_links SET created_at='2026-09-07T12:00:00+10:00',updated_at='2026-11-20T10:00:00+11:00'")
        self.assertEqual('Pending',self.state()['result'])

    def test_same_second_source_cannot_inherit_old_manual(self):
        source,_=self.legacy_manual_source()
        self.c.execute("UPDATE student_documents SET uploaded_at='2026-09-16T11:46:29+10:00' WHERE id=?",(source,))
        self.assertEqual('Pending',self.state()['result'])

    def test_invalid_upload_timestamp_cannot_inherit_old_manual(self):
        source,_=self.legacy_manual_source()
        self.c.execute("UPDATE student_documents SET uploaded_at='invalid' WHERE id=?",(source,))
        self.assertEqual('Pending',self.state()['result'])

    def test_timestamp_comparison_uses_offsets_not_lexical_order(self):
        source,_=self.legacy_manual_source()
        self.c.execute("UPDATE student_documents SET uploaded_at='2026-09-16T12:16:29+11:00' WHERE id=?",(source,))
        self.assertEqual('Granted',self.state()['result'])

    def test_old_manual_cannot_confirm_later_reclassified_registered_material(self):
        source,d=self.legacy_manual_source(registered=True)
        from visa_document_metadata import edit_document_metadata
        actor={'actor_user_id':1,'occurred_at':'2026-11-20T10:00:00+11:00'}
        edit_document_metadata(self.c,1,d['id'],category='other',**actor)
        edit_document_metadata(self.c,1,d['id'],category='grant_letter',**actor)
        self.assertEqual('Pending',self.state()['result'])
