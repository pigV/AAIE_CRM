import unittest
from unittest.mock import patch
from tests import test_student_detail_summary_cards as fixture
from student_detail_summary_cards import _load_school_plan_card_summary


class CanonicalSchoolSummaryTests(unittest.TestCase):
    setUp=fixture.StudentDetailSummaryCardTests.setUp
    tearDown=fixture.StudentDetailSummaryCardTests.tearDown

    def prepare(self):
        self.connection.executescript('''
            ALTER TABLE school_plan_reports ADD COLUMN application_cycle_id INTEGER;
            ALTER TABLE school_plan_reports ADD COLUMN plan_sector TEXT;
            ALTER TABLE school_plan_reports ADD COLUMN city_id INTEGER;
            ALTER TABLE school_plan_reports ADD COLUMN cycle_is_current INTEGER DEFAULT 0;
            ALTER TABLE school_plan_confirmed_schools ADD COLUMN result_offer_id INTEGER;
            CREATE INDEX idx_school_plan_reports_cycle ON school_plan_reports(application_cycle_id,plan_sector,city_id,cycle_is_current);
            CREATE TABLE application_cycles(id PRIMARY KEY,student_id,status,sequence_no);
            INSERT INTO application_cycles VALUES(101,'S001','active',1);
            CREATE TABLE cycle_final_acceptances(id PRIMARY KEY,application_cycle_id,school_name_snapshot);
            CREATE TABLE public_application_groups(id PRIMARY KEY,application_cycle_id,student_id,status,submitted_at);
            CREATE TABLE public_application_preferences(id PRIMARY KEY,public_application_group_id,confirmed_school_id);
            CREATE TABLE private_school_applications(id PRIMARY KEY,application_cycle_id,student_id,confirmed_school_id,status,submitted_at);
            INSERT INTO school_plan_reports VALUES(1,'S001',1,'confirmed','2026-08-08',NULL,101,'public',1,1);
            INSERT INTO school_plan_confirmed_schools VALUES(1,1,'S001','Synthetic School',1,1,'not_submitted','pending',NULL);
        ''')

    def summary(self):
        with patch('student_detail_summary_cards.is_workflow_v2_active',return_value=True):
            return _load_school_plan_card_summary(self.connection,'S001')

    def test_public_formal_submission_updates_count_and_waiting_list(self):
        self.prepare()
        self.connection.execute("INSERT INTO public_application_groups VALUES(1,101,'S001','submitted','2026-08-08')")
        self.connection.execute('INSERT INTO public_application_preferences VALUES(1,1,1)')
        result=self.summary()
        self.assertEqual(1,result.submitted_count)
        self.assertEqual(('Synthetic School',),result.waiting_result_names)
        self.assertEqual((),result.not_submitted_names)

    def test_public_retraction_overrides_old_legacy_submission(self):
        self.prepare()
        self.connection.execute("UPDATE school_plan_confirmed_schools SET submission_status='submitted'")
        self.connection.execute("INSERT INTO public_application_groups VALUES(1,101,'S001','draft',NULL)")
        self.connection.execute('INSERT INTO public_application_preferences VALUES(1,1,1)')
        self.assertEqual(0,self.summary().submitted_count)

    def test_foreign_cycle_and_student_cannot_supply_current_submission(self):
        self.prepare()
        self.connection.execute("INSERT INTO private_school_applications VALUES(1,999,'S001',1,'submitted','2026-08-08')")
        self.connection.execute("INSERT INTO public_application_groups VALUES(1,101,'FOREIGN','submitted','2026-08-08')")
        self.connection.execute('INSERT INTO public_application_preferences VALUES(1,1,1)')
        self.assertEqual(0,self.summary().submitted_count)

    def test_foreign_confirmed_school_row_is_never_shown(self):
        self.prepare()
        self.connection.execute("UPDATE school_plan_confirmed_schools SET student_id='FOREIGN'")
        self.assertEqual((),self.summary().names)
