"""Operational progression completion must retain its separate live Visa work."""
import unittest
from tests import test_business_change_progression_received_offer as fixture

class CompletedProgressionVisaTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ProgressionReceivedOfferTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db;self.cycle=self.f.cycle;self.sid=self.f.sid
        self.db.execute("UPDATE student_cases SET status='completed',active_slot=0,terminal_result='completed' WHERE id=?",(self.f.case,))

    def visa(self):
        return self.db.execute("""INSERT INTO visa_cases(student_id,application_cycle_id,visa_case_name,
            visa_type,applicant_name,application_type,status,result_status,actual_lodgement_date,
            lodgement_recorded_at,lodged_by_user_id,created_by,created_at,updated_at)
            VALUES(?,?,'SYNTHETIC progression Visa','500 Student Visa','Synthetic','New Application',
                'Lodged','Pending','2026-11-01',?,1,1,?,?)""",(self.sid,self.cycle,self.f.f.stamp,self.f.f.stamp,self.f.f.stamp))

    def test_completed_education_still_counts_its_formally_lodged_visa(self):
        self.visa()
        self.assertEqual('11_visa_result',self.f.stage())

    def test_completed_education_without_a_visa_is_excluded(self):
        from student_application_stage_distribution import load_application_stage_distribution
        with self.db.read_snapshot() as c:
            self.assertFalse([r for r in load_application_stage_distribution(c,cycles_only=True) if r.cycle_id==self.cycle])

    def test_paused_case_does_not_resume_because_a_visa_exists(self):
        from dashboard_confirmed_stages import held_case_cycles
        self.visa();self.db.execute("UPDATE student_cases SET status='paused',active_slot=1,terminal_result=NULL WHERE id=?",(self.f.case,))
        with self.db.read_snapshot() as c:self.assertEqual('paused',held_case_cycles(c,(self.cycle,))[self.cycle])

    def held(self):
        from dashboard_confirmed_stages import held_case_cycles
        with self.db.read_snapshot() as c:
            return held_case_cycles(c,(self.cycle,))

    def test_preparing_visa_keeps_education_open_without_claiming_lodgement(self):
        case=self.visa()
        self.db.execute("""UPDATE visa_cases SET status='Information Collection',
            actual_lodgement_date=NULL,lodgement_recorded_at=NULL,lodged_by_user_id=NULL
            WHERE id=?""",(case,))
        self.assertNotIn(self.cycle,self.held())
        self.assertEqual('07_school_response',self.f.stage())

    def test_archived_student_visa_cannot_reopen_completed_education(self):
        case=self.visa()
        self.db.execute('UPDATE visa_cases SET archived_at=? WHERE id=?',(self.f.f.stamp,case))
        self.assertEqual('completed',self.held()[self.cycle])

    def test_guardian_visa_cannot_reopen_completed_student_education(self):
        case=self.visa()
        self.db.execute("UPDATE visa_cases SET visa_type='590 Guardian Visa' WHERE id=?",(case,))
        self.assertEqual('completed',self.held()[self.cycle])

    def test_another_owned_cycle_visa_cannot_reopen_this_progression(self):
        case=self.visa()
        self.db.execute('UPDATE visa_cases SET application_cycle_id=? WHERE id=?',(self.f.f.cycle,case))
        self.assertEqual('completed',self.held()[self.cycle])

    def test_formally_closed_progression_stays_held_despite_lodged_visa(self):
        self.visa()
        self.db.execute("""UPDATE application_cycles SET status='closed',outcome='completed',closed_at=?
            WHERE id=?""",(self.f.f.stamp,self.cycle))
        self.assertEqual('completed',self.held()[self.cycle])

    def test_terminal_case_states_stay_held_despite_lodged_visa(self):
        self.visa()
        for status in ('cancelled','failed','archived'):
            with self.subTest(status=status):
                self.db.execute('UPDATE student_cases SET status=?,terminal_result=? WHERE id=?',
                    (status,'completed' if status=='archived' else status,self.f.case))
                self.assertEqual(status,self.held()[self.cycle])

    def test_explicit_refusal_or_withdrawal_cannot_reopen_completed_education(self):
        from visa_result_status import record_manual_result
        case=self.visa()
        for status in ('Refused','Withdrawn'):
            with self.subTest(status=status):
                with self.db.transaction() as c:
                    record_manual_result(c,case,status,actor_user_id=1,
                        occurred_at=self.f.f.stamp,reason='Synthetic reviewed terminal result')
                self.assertEqual('completed',self.held()[self.cycle])

    def test_latest_closed_student_visa_cannot_fall_back_to_an_older_lodged_case(self):
        self.visa()
        latest=self.visa()
        self.db.execute("UPDATE visa_cases SET status='Closed' WHERE id=?",(latest,))
        self.assertEqual('completed',self.held()[self.cycle])
