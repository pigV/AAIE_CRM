"""A migrated empty parallel HE shell must not hide the live school Visa."""
import unittest

from application_cycle import create_student_cycle
from student_workflow_projection import load_student_workflow_projection_map
from tests import test_business_change_school_chain as fixture


class MigratedHEShellVisaTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.SchoolMaterialChainTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.db = self.f.db
        self.school = self.f.cycle
        self.case = self.db.execute("""INSERT INTO visa_cases(
            student_id,application_cycle_id,visa_case_name,visa_type,applicant_name,
            application_type,status,result_status,actual_lodgement_date,
            lodgement_recorded_at,lodged_by_user_id,created_by,created_at,updated_at)
            VALUES(?,?,'Synthetic live school Visa','500 Student Visa','Synthetic',
                   'New Application','Lodged','Pending','2026-11-01',?,1,1,?,?)""",
            (self.f.sid,self.school,self.f.stamp,self.f.stamp,self.f.stamp))
        with self.db.transaction() as c:
            self.he = create_student_cycle(c,self.f.sid,'initial',self.f.stamp,1,
                                          education_stream='higher_education').id
            c.execute("""INSERT INTO higher_education_audit_log(
                application_cycle_id,entity_type,entity_id,action,actor_user_id,occurred_at)
                VALUES(?,'application_cycle',?,'legacy_student_migrated',1,?)""",
                (self.he,self.he,self.f.stamp))

    def profile(self, *, cycle_id=None):
        with self.db.read_snapshot() as c:
            return load_student_workflow_projection_map(c,[self.f.sid],cycle_id=cycle_id)[self.f.sid]

    def assert_shell_retained(self):
        result = self.profile()
        self.assertEqual(self.he,result.cycle_id)
        self.assertNotEqual('11_visa_result',result.stage_key)

    def test_migrated_empty_parallel_shell_keeps_formally_lodged_school_visa(self):
        before = self.db.one('SELECT count(*) FROM business_change_events')[0]
        for _ in range(2):
            result = self.profile()
            self.assertEqual((self.school,'11_visa_result'),(result.cycle_id,result.stage_key))
        self.assertEqual(before,self.db.one('SELECT count(*) FROM business_change_events')[0])
        self.assertEqual(2,self.db.one("SELECT count(*) FROM application_cycles WHERE student_id=? AND status='active'",(self.f.sid,))[0])

    def test_explicit_he_cycle_stays_exact(self):
        result = self.profile(cycle_id=self.he)
        self.assertEqual(self.he,result.cycle_id)
        self.assertNotEqual('11_visa_result',result.stage_key)

    def test_migrated_shell_keeps_confirmed_grant_with_stale_active_cycle_flag(self):
        import hashlib
        from visa_documents_candidate import register_document, save_extraction, confirm_letter
        path = self.f.student_root / 'synthetic-current-grant.pdf'
        data = self.f.pdf('SYNTHETIC ONLY - reviewed grant material')
        path.write_bytes(data)
        with self.db.transaction() as c:
            document = register_document(c,self.case,file_path=str(path),original_name=path.name,
                version=1,sha256=hashlib.sha256(data).hexdigest(),category='grant_letter',
                actor_user_id=1,occurred_at=self.f.stamp)
            extraction = save_extraction(c,self.case,document['id'],text='SYNTHETIC reviewed grant',
                actor_user_id=1,occurred_at=self.f.stamp)
            confirm_letter(c,self.case,extraction['id'],values={'document_kind':'grant',
                'decision_type':'grant','decision_date':'2026-11-01'},
                actor_user_id=1,occurred_at=self.f.stamp)
            # Historical fixture: the owning cycle flag has not reconciled yet.
            c.execute("UPDATE application_cycles SET status='active',outcome=NULL,closed_at=NULL WHERE id=?",
                      (self.school,))
        result = self.profile()
        self.assertEqual((self.school,'visa_granted'),(result.cycle_id,result.stage_key))
        self.assertEqual('active',self.db.one('SELECT status FROM application_cycles WHERE id=?',(self.school,))[0])

    def test_unmarked_empty_he_cycle_is_not_a_migration_shell(self):
        self.db.execute('DELETE FROM higher_education_audit_log WHERE application_cycle_id=?',(self.he,))
        self.assert_shell_retained()

    def test_migrated_shell_with_a_draft_plan_is_real_new_work(self):
        self.db.execute("""INSERT INTO higher_education_plans(application_cycle_id,status,
            created_by_user_id,created_at,updated_by_user_id,updated_at)
            VALUES(?,'Draft',1,?,1,?)""",(self.he,self.f.stamp,self.f.stamp))
        self.assert_shell_retained()

    def test_migrated_he_cycle_with_its_own_contract_is_not_empty(self):
        self.db.execute("""INSERT INTO contracts(student_id,application_cycle_id,
            contract_category,contract_type,contract_title,contract_status,generated_at,updated_at)
            VALUES(?,?,'留学合同','StudyAbroadContract','Synthetic HE work','Draft',?,?)""",
            (self.f.sid,self.he,self.f.stamp,self.f.stamp))
        self.assert_shell_retained()

    def test_migrated_he_cycle_with_staff_stage_confirmation_is_not_empty(self):
        self.f.h.form = {'kind':'contract','decision':'optional','reason':'Synthetic new HE decision'}
        reply = self.f.h.student_cycle_stage_evidence('POST',self.f.sid,self.he)
        self.assertEqual('success',reply[2],reply)
        self.assert_shell_retained()

    def test_closed_school_cycle_cannot_supply_a_live_visa(self):
        self.db.execute("""UPDATE application_cycles SET status='closed',outcome='withdrawn',closed_at=?
            WHERE id=?""",(self.f.stamp,self.school))
        self.assert_shell_retained()

    def test_old_refusal_and_withdrawal_do_not_replace_the_shell(self):
        from visa_result_status import record_manual_result
        for outcome in ('Refused','Withdrawn'):
            with self.subTest(outcome=outcome):
                with self.db.transaction() as c:
                    record_manual_result(c,self.case,outcome,actor_user_id=1,
                        occurred_at=self.f.stamp,reason='Synthetic reviewed result')
                self.assert_shell_retained()

    def test_unconfirmed_granted_label_does_not_replace_the_shell(self):
        self.db.execute("""UPDATE visa_cases SET status='Granted',result_status='Granted',
            actual_lodgement_date=NULL,lodgement_recorded_at=NULL,lodged_by_user_id=NULL
            WHERE id=?""",(self.case,))
        self.assert_shell_retained()

    def test_newer_preparing_visa_case_still_supersedes_old_lodgement(self):
        self.db.execute("""INSERT INTO visa_cases(student_id,application_cycle_id,visa_case_name,
            visa_type,applicant_name,application_type,status,result_status,created_by,created_at,updated_at)
            VALUES(?,?,'Synthetic newer preparation','500 Student Visa','Synthetic','New Application',
                'Information Collection','Pending',1,?,?)""",(self.f.sid,self.he,self.f.stamp,self.f.stamp))
        self.assert_shell_retained()
