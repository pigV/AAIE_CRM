import unittest
from tests import test_revenue_school_courses as course_fixture
from revenue.school_courses import course_details
import revenue.school_courses as courses

class CourseProfileRevisionTests(unittest.TestCase):
    def setUp(self):
        self.f=course_fixture.SchoolCourseTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db
    def test_profile_survives_save_read_and_correction_without_rewriting_financial_facts(self):
        self.db.execute("UPDATE schools SET sector_key='private' WHERE id=13")
        entry=self.f.entry('language',600000,13)
        entry.update(sector='private',start_date='2027-01-25',student_number='',authority_state='')
        self.db.execute("UPDATE coe_oshc SET coe_provider=(SELECT school_name FROM schools WHERE id=13)")
        self.f.save([entry])
        row=course_details(self.db,'TEST-PENINSULA-SCHEDULE')[0]
        self.assertEqual('2027-01-25',row['profile']['start_date'])
        before=[tuple(r) for r in self.db.execute('SELECT * FROM revenue_tuition_facts')]
        form={'revision':row['revision'],'idempotency_key':'profile-correction','start_date':'2027-02-01','reason':'Corrected against original CoE'}
        courses.correct_course_profile(self.db,'TEST-PENINSULA-SCHEDULE',row['uid'],form,1)
        changed=course_details(self.db,'TEST-PENINSULA-SCHEDULE')[0]
        self.assertEqual('2027-02-01',changed['profile']['start_date'])
        self.assertNotEqual(row['revision'],changed['revision'])
        self.assertEqual(before,[tuple(r) for r in self.db.execute('SELECT * FROM revenue_tuition_facts')])
        self.assertEqual('2027-01-25',row['profile']['start_date'])
        with self.assertRaises(ValueError):
            courses.correct_course_profile(self.db,'TEST-PENINSULA-SCHEDULE',row['uid'],{**form,'idempotency_key':'stale-copy'},1)
    def test_main_selected_offer_must_match_existing_enrolment_offer(self):
        entry=self.f.entry('main',1200000);entry['offer_id']=24
        with self.assertRaisesRegex(ValueError,'Offer'):
            self.f.save([entry])
    def test_moved_offer_does_not_appear_in_its_old_cycle_choices(self):
        self.db.execute('ALTER TABLE offers ADD COLUMN application_cycle_id INTEGER')
        self.db.execute('UPDATE offers SET application_cycle_id=125')
        self.db.execute('UPDATE offers SET application_cycle_id=999 WHERE id=22')
        ids=[r['id'] for r in courses.accepted_course_offers(self.db,'TEST-PENINSULA-SCHEDULE',125)]
        self.assertNotIn(22,ids)
    def test_historical_public_course_editor_shows_offer_school_number_and_authority(self):
        self.db.execute("UPDATE schools SET sector_key='public',state='VIC' WHERE id=16")
        self.f.save([self.f.entry('language',600000)])
        rows=course_details(self.db,'TEST-PENINSULA-SCHEDULE')
        html=courses.render_course_details(rows,'TEST-PENINSULA-SCHEDULE','csrf','edit-test')
        self.assertIn('name="student_number"',html);self.assertIn('name="authority_state"',html)
