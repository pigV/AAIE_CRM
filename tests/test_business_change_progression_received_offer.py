"""An owned reviewed progression Offer is received before it is accepted."""
from pathlib import Path
import unittest

from tests import test_business_change_school_chain as fixture


class ProgressionReceivedOfferTests(unittest.TestCase):
    def setUp(self):
        from application_withdrawal import withdraw_application
        from progression_case_service import ProgressionCaseService
        from student_lifecycle.enrolment import SchoolEnrolmentService
        from workflow_application_service import submit_private_application
        from student_case_permissions import PROGRESSION_CASE_PERMISSIONS
        self.f=fixture.SchoolMaterialChainTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db;self.sid=self.f.sid;self.permissions=set(PROGRESSION_CASE_PERMISSIONS)
        self.flags={'read_enabled':True,'progression_write_enabled':True}
        with self.db.transaction() as c:
            withdraw_application(c,student_id=self.sid,cycle_id=self.f.cycle,actor_id=1,actor_name='Synthetic',
                reason='Synthetic previous cycle',stage='Contract',timestamp=self.f.stamp)
        self.school=self.db.execute("INSERT INTO schools(school_name,sector_key,school_type,created_at,updated_at) VALUES('Synthetic University','private','University',?,?)",(self.f.stamp,self.f.stamp))
        SchoolEnrolmentService(self.db.path).confirm_current(student_id=self.sid,school_id=self.school,
            school_name_snapshot='Synthetic University',actor_user_id=1,reason='Synthetic foundation course')
        self.service=ProgressionCaseService(self.db.path)
        case=self.service.create_case(student_id=self.sid,actor_user_id=1,actor_permissions=self.permissions,flags=self.flags,
            progression_type='bachelor',current_school_id=self.school,current_school_name_snapshot='Synthetic University',
            current_course_name='Foundation',current_year_level='foundation',expected_graduation_date='2026-12-01',
            target_education_stage='Higher Education',target_course_type='bachelor',target_institution_id=self.school,
            target_institution_name_snapshot='Synthetic University',target_start_date='2027-02-01',owner_user_id=1)
        self.case=case.case_id
        self.cycle=self.db.one('SELECT id FROM application_cycles WHERE source_student_case_id=?',(self.case,))[0]
        self.target=self.service.get_case_data(self.case)['target_applications'][0]['id']
        self.service.select_final_application(self.case,self.target,actor_user_id=1,actor_permissions=self.permissions,
            flags=self.flags,canonical_application_kind='private')
        self.application=self.db.one('''SELECT component.canonical_application_id FROM progression_pathway_components component
            JOIN progression_pathway_acceptances pathway ON pathway.id=component.pathway_id WHERE pathway.progression_case_id=?''',(self.case,))[0]
        with self.db.transaction() as c:submit_private_application(c,self.application,actor_user_id=1,occurred_at=self.f.stamp)
        path=self.f.student_root/'synthetic-offer.pdf';path.write_bytes(self.f.pdf('SYNTHETIC ONLY Offer'))
        self.document=self.db.execute('''INSERT INTO student_documents(student_id,folder_category,document_type,
            original_name,stored_name,file_path,file_size,uploaded_at,is_active,document_status)
            VALUES(?,'Offer','Offer','synthetic-offer.pdf','synthetic-offer.pdf',?,?,?,1,'active')''',
            (self.sid,str(path),path.stat().st_size,self.f.stamp))
        self.offer=self.db.execute('''INSERT INTO offers(student_id,application_cycle_id,school_id,school_name,
            document_type,offer_status,offer_date,offer_expiry_date,offer_file_path,created_at)
            VALUES(?,?,?,'Synthetic University','Offer','Received',?,'2099-12-31',?,?)''',
            (self.sid,self.cycle,self.school,self.f.stamp[:10],str(path),self.f.stamp))

    def add(self):
        return self.service.add_offer(self.case,actor_user_id=1,actor_permissions=self.permissions,flags=self.flags,
            stage_id=None,existing_offer_id=self.offer,offer_type='unconditional',institution_id=self.school,
            institution_name_snapshot='Synthetic University',course_name='Bachelor',offer_date=self.f.stamp[:10],
            expiry_date='2099-12-31',conditions_text=None,student_document_id=self.document,notes='Synthetic reviewed Offer',
            target_application_id=self.target)

    def stage(self):
        from student_application_stage_distribution import load_application_stage_result_map,load_application_stage_distribution
        from student_management_summary import load_student_management_summary_map
        with self.db.read_snapshot() as c:
            stage=load_application_stage_result_map(c,[self.sid])[self.sid]
            summary=load_student_management_summary_map(c,[self.sid])[self.sid]
            member=[r for r in load_application_stage_distribution(c,cycles_only=True) if r.cycle_id==self.cycle]
        self.assertEqual(1,len(member));self.assertEqual(stage.key,member[0].stage.key)
        self.assertEqual(stage.key,summary.stage_key)
        return stage.key

    def test_reviewed_received_offer_advances_before_parent_acceptance(self):
        self.assertEqual('07_school_response',self.stage())
        self.add()
        self.assertEqual('08_offer_acceptance',self.stage())

    def test_archived_document_and_withdrawn_or_expired_offer_are_not_received_work(self):
        self.add()
        self.db.execute('UPDATE student_documents SET is_active=0 WHERE id=?',(self.document,))
        self.assertEqual('07_school_response',self.stage())
        self.db.execute('UPDATE student_documents SET is_active=1 WHERE id=?',(self.document,))
        self.db.execute("UPDATE offers SET offer_status='Withdrawn' WHERE id=?",(self.offer,))
        self.assertEqual('07_school_response',self.stage())
        self.db.execute("UPDATE offers SET offer_status='Received',offer_expiry_date='2020-01-01' WHERE id=?",(self.offer,))
        self.assertEqual('07_school_response',self.stage())

    def test_native_school_id_match_keeps_historical_name_snapshots_usable(self):
        self.db.execute("UPDATE offers SET school_name='Historical University Name' WHERE id=?",(self.offer,))
        self.add()
        self.assertEqual('08_offer_acceptance',self.stage())

    def test_native_unicode_name_match_without_an_offer_school_id_is_usable(self):
        self.db.execute("UPDATE progression_target_applications SET institution_name_snapshot='Straße University' WHERE id=?",(self.target,))
        self.db.execute("UPDATE offers SET school_id=NULL,school_name='STRASSE UNIVERSITY' WHERE id=?",(self.offer,))
        self.add()
        self.assertEqual('08_offer_acceptance',self.stage())

    def test_canonical_offer_void_cannot_be_resurrected_by_case_received_label(self):
        from workflow_offer_events import record_offer_received
        self.add()
        with self.db.transaction() as c:
            event=record_offer_received(c,self.cycle,external_offer_key='synthetic-received',school_name='Synthetic University',
                received_at=self.f.stamp,actor_user_id=1,private_application_id=self.application,legacy_offer_id=self.offer,school_id=self.school)
            c.execute('''INSERT INTO offer_received_event_state_events(offer_received_event_id,state,reason,occurred_at,
                actor_user_id,idempotency_key,created_at) VALUES(?,'VOID','Synthetic void',?,1,'synthetic-void',?)''',
                (event.event.id,self.f.stamp,self.f.stamp))
        self.assertEqual('07_school_response',self.stage())
