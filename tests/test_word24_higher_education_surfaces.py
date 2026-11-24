"""Synthetic, isolated university and visa-only facts on shared read surfaces."""
import unittest

from higher_education_repository import HigherEducationRepository
from tests import test_higher_education_waiting_plan_surfaces as surface_fixture


class Word24HigherEducationSurfaces(unittest.TestCase):
    setUp = surface_fixture.HigherEducationWaitingPlanSurfaces.setUp
    prepare = surface_fixture.HigherEducationWaitingPlanSurfaces.prepare
    read = surface_fixture.HigherEducationWaitingPlanSurfaces.read
    assert_surfaces = surface_fixture.HigherEducationWaitingPlanSurfaces.assert_surfaces

    def test_direct_application_offer_and_live_coe_need_no_contract_or_plan(self):
        self.prepare()
        self.assert_surfaces('06_school_application_submission', '准备学校申请')
        with self.db.transaction() as conn:
            repo = HigherEducationRepository(conn)
            provider = repo.create_provider('Synthetic University', 'University', 1,
                                            self.f.stamp, review_status='Approved')
            application = repo.create_application(self.f.cycle, provider, 'Bachelor',
                'Bachelor of Science', '2027-02', 'Direct', 1, self.f.stamp)
        self.assert_surfaces('07_school_response', '等待学校发 Offer')
        with self.db.transaction() as conn:
            offer = HigherEducationRepository(conn).record_offer(application, self.f.stamp)
        self.assert_surfaces('08_offer_acceptance', '等待家长接受 Offer')
        with self.db.transaction() as conn:
            HigherEducationRepository(conn).accept_offer(offer, 1, self.f.stamp)
        path = self.f.student_root / 'synthetic-university-coe.pdf'
        path.write_bytes(self.f.pdf('SYNTHETIC MAIN COURSE COE'))
        self.db.execute('''INSERT INTO student_documents(student_id,folder_category,document_type,
            original_name,stored_name,file_path,file_size,uploaded_by,uploaded_at)
            VALUES(?,'COE + OSHC','COE',?,?,?, ?,1,?)''',
            (self.f.sid,path.name,path.name,str(path),path.stat().st_size,self.f.stamp))
        self.db.execute('''INSERT INTO coe_oshc(student_id,application_cycle_id,document_kind,
            file_path,school_name,coe_provider,coe_course,coe_start_date,coe_end_date,created_at)
            VALUES(?,?,'COE',?,'Synthetic University','Synthetic University',
                'Bachelor of Science','2027-02-01','2099-12-31',?)''',
            (self.f.sid,self.f.cycle,str(path),self.f.stamp))
        self.assert_surfaces('10_visa_submission', '等待递交 Visa')
        self.db.execute("UPDATE student_documents SET is_active=0,document_status='voided' WHERE file_path=?", (str(path),))
        self.assertNotEqual('10_visa_submission', self.f.stage())

    def test_current_visa_only_service_ignores_old_school_submission(self):
        self.db.execute("UPDATE students SET form_data_json=? WHERE student_id=?",
            ('{"application_service":"visa_only"}',self.f.sid))
        self.db.execute('''INSERT INTO private_school_applications(application_cycle_id,student_id,
            school_name_snapshot,status,submitted_at,created_at,updated_at)
            VALUES(?,?,'Historical school','submitted',?,?,?)''',
            (self.f.cycle,self.f.sid,self.f.stamp,self.f.stamp,self.f.stamp))
        self.assert_surfaces('10_visa_submission', '等待递交 Visa')

    def test_owned_legacy_signed_offer_with_main_coe_progresses_without_fabricated_acceptance(self):
        self.prepare()
        paths = {}
        for kind in ('SignedOffer', 'COE'):
            path = self.f.student_root / (kind+'.pdf')
            path.write_bytes(self.f.pdf('SYNTHETIC LEGACY '+kind))
            paths[kind] = str(path)
            self.db.execute('''INSERT INTO student_documents(student_id,folder_category,document_type,
                original_name,stored_name,file_path,file_size,uploaded_by,uploaded_at)
                VALUES(?,'Synthetic',?,?,?,?,?,1,?)''',
                (self.f.sid,kind,path.name,path.name,str(path),path.stat().st_size,self.f.stamp))
        self.db.execute('''INSERT INTO offers(student_id,application_cycle_id,school_name,
            document_type,offer_status,offer_file_path,signed_offer_file_path,offer_expiry_date,created_at)
            VALUES(?,?,'QUT','SignedOffer','accepted',?,?,'2099-01-01',?)''',
            (self.f.sid,self.f.cycle,paths['SignedOffer'],paths['SignedOffer'],self.f.stamp))
        self.db.execute('''INSERT INTO coe_oshc(student_id,application_cycle_id,document_kind,
            file_path,school_name,coe_provider,coe_course,coe_start_date,coe_end_date,created_at)
            VALUES(?,?,'COE',?,'Queensland University of Technology (QUT) [00213J]',
                'Queensland University of Technology (QUT) [00213J]',
                'Bachelor of Science','2027-02-01','2099-12-31',?)''',
            (self.f.sid,self.f.cycle,paths['COE'],self.f.stamp))
        self.assert_surfaces('10_visa_submission', '等待递交 Visa')
        self.assertEqual(0,self.db.one('SELECT count(*) FROM cycle_final_acceptances')[0])
        self.db.execute("UPDATE student_documents SET is_active=0,document_status='voided' WHERE file_path=?", (paths['SignedOffer'],))
        self.assertNotEqual('10_visa_submission',self.f.stage())

    def test_closed_completed_transfer_keeps_detail_label_and_leaves_active_distribution(self):
        from student_detail_summary_cards import load_student_detail_card_summary
        from student_application_stage_distribution import load_application_stage_distribution
        case = self.db.execute('''INSERT INTO student_cases(student_id,case_type,sequence_no,
            status,terminal_result,active_slot,created_by,created_at,updated_by,updated_at)
            VALUES(?,'transfer',1,'completed','completed',0,1,?,1,?)''',
            (self.f.sid,self.f.stamp,self.f.stamp))
        self.db.execute("UPDATE application_cycles SET cycle_type='transfer',source_student_case_id=?,status='closed',outcome='completed',closed_at=? WHERE id=?",
            (case,self.f.stamp,self.f.cycle))
        with self.db.read_snapshot() as conn:
            summary = load_student_detail_card_summary(conn,self.f.sid)
            self.assertEqual('closed',summary.stage_key)
            self.assertEqual('申请已结案',summary.stage_label)
            self.assertFalse(load_application_stage_distribution(conn))
