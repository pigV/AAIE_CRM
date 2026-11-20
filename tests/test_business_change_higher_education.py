import unittest
from unittest.mock import patch

from tests import test_higher_education_v2 as fixture
from business_changes.schema import install
from business_changes.versions import version_of
from business_changes.contracts import Subject


class HigherEducationChangeTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.HigherEducationV2RepositoryTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.c=self.f.conn;self.repo=self.f.repo
        self.c.commit();self.c.execute('PRAGMA foreign_keys=ON');self.c.execute('BEGIN');install(self.c)
        self.c.execute('CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT)')
        self.c.execute("INSERT INTO system_settings VALUES('business_change_sync_v1','enabled')")
        self.c.commit();self.c.execute('BEGIN IMMEDIATE')

    def test_new_plan_publishes_only_its_owned_student_and_cycle(self):
        plan=self.repo.create_plan(1,1,'2026-11-20T03:00:00+11:00')
        event=self.c.execute('SELECT event_type,source_kind,source_id FROM business_change_events').fetchone()
        self.assertIsNotNone(event)
        self.assertEqual(('higher_education.plan_created','he_plan',str(plan)),tuple(event))
        self.assertEqual(1,version_of(self.c,Subject('student','S1')))
        self.assertEqual(1,version_of(self.c,Subject('application_cycle','1')))
        self.assertEqual(1,version_of(self.c,Subject('dashboard','overview')))
        self.assertEqual(0,version_of(self.c,Subject('student','FOREIGN')))
        self.c.rollback()
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM higher_education_plans').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])

    def test_failed_event_does_not_leave_a_saved_plan_when_the_caller_catches_it(self):
        with patch('business_changes.commands._append',side_effect=OSError('synthetic publication failure')):
            with self.assertRaises(OSError):self.repo.create_plan(1,1,'2026-11-20T03:00:00+11:00')
        self.c.commit()
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM higher_education_plans').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM business_change_commands').fetchone()[0])

    def test_plan_item_confirmation_and_removal_share_the_original_cycle(self):
        plan=self.repo.create_plan(1,1,'t')
        item=self.repo.add_plan_item(plan,self.f.university,'Bachelor','SYNTHETIC COURSE','February 2027',1,'t')
        self.repo.remove_draft_plan_item(item)
        second=self.repo.add_plan_item(plan,self.f.university,'Bachelor','SYNTHETIC COURSE TWO','February 2027',1,'t')
        self.repo.confirm_plan(plan,1,'t')
        events=[row[0] for row in self.c.execute('SELECT event_type FROM business_change_events ORDER BY seq')]
        self.assertEqual(['higher_education.plan_created','higher_education.plan_item_added',
            'higher_education.plan_item_removed','higher_education.plan_item_added','higher_education.plan_confirmed'],events)
        self.assertEqual('Confirmed',self.c.execute('SELECT status FROM higher_education_plans WHERE id=?',(plan,)).fetchone()[0])
        self.assertEqual(5,version_of(self.c,Subject('student','S1')))
        self.assertEqual(5,version_of(self.c,Subject('application_cycle','1')))

    def test_failed_plan_item_publication_rolls_back_course_usage_as_well_as_the_item(self):
        plan=self.repo.create_plan(1,1,'t')
        before=self.c.execute('SELECT count(*) FROM higher_education_course_names').fetchone()[0]
        with patch('business_changes.commands._append',side_effect=OSError('synthetic publication failure')):
            with self.assertRaises(OSError):
                self.repo.add_plan_item(plan,self.f.university,'Bachelor','SYNTHETIC FAIL COURSE','February 2027',1,'t')
        self.c.commit()
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM higher_education_plan_items').fetchone()[0])
        self.assertEqual(before,self.c.execute('SELECT count(*) FROM higher_education_course_names').fetchone()[0])
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])

    def test_application_offer_acceptance_and_coe_link_publish_the_same_cycle_scope(self):
        plan=self.repo.create_plan(1,1,'t')
        item=self.repo.add_plan_item(plan,self.f.university,'Bachelor','SYNTHETIC DEGREE','2027-02',1,'t')
        self.repo.confirm_plan(plan,1,'t')
        application=self.repo.submit_plan_item(item,'Direct','2027-02',1,'t')
        offer=self.repo.record_offer(application,'t',actor_user_id=1)
        self.repo.accept_offer(offer,1,'t')
        self.c.execute('INSERT INTO coe_oshc(id,application_cycle_id) VALUES(7,1)')
        link=self.repo.link_coe(application,7,1,'t')
        events=[tuple(row) for row in self.c.execute('SELECT event_type,source_kind,source_id FROM business_change_events ORDER BY seq')]
        self.assertEqual([
            ('higher_education.application_submitted','he_application',str(application)),
            ('higher_education.offer_recorded','he_offer',str(offer)),
            ('higher_education.offer_accepted','he_offer',str(offer)),
            ('higher_education.coe_linked','he_coe_link',str(link)),
        ],events[-4:])
        self.assertEqual(7,version_of(self.c,Subject('student','S1')))
        self.assertEqual(7,version_of(self.c,Subject('application_cycle','1')))

    def test_failed_application_publication_rolls_back_the_original_application(self):
        with patch('business_changes.commands._append',side_effect=OSError('synthetic publication failure')):
            with self.assertRaises(OSError):
                self.repo.create_application(1,self.f.university,'Bachelor','SYNTHETIC DEGREE','2027-02','Direct',1,'t')
        self.c.commit()
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM higher_education_applications').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])

    def test_application_cannot_reuse_another_students_plan_item(self):
        from higher_education_repository import HigherEducationValidationError,HigherEducationConflict
        self.c.execute("INSERT INTO students(student_id,application_stream) VALUES('S2','higher_education')")
        self.c.execute("""INSERT INTO application_cycles(id,cycle_uid,student_id,cycle_type,sequence_no,status,
            opened_at,created_at,updated_at,education_stream) VALUES(2,'he-second','S2','initial',1,'active','t','t','t','higher_education')""")
        plan=self.repo.create_plan(1,1,'t')
        item=self.repo.add_plan_item(plan,self.f.university,'Bachelor','SYNTHETIC OWNED DEGREE','2027-02',1,'t')
        self.repo.confirm_plan(plan,1,'t')
        before=self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0]
        with self.assertRaises((HigherEducationValidationError,HigherEducationConflict)):
            self.repo.create_application(2,self.f.university,'Bachelor','SYNTHETIC FOREIGN DEGREE','2027-02',
                'Direct',1,'t',source_plan_item_id=item)
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM higher_education_applications').fetchone()[0])
        self.assertEqual(before,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
        self.assertEqual(0,version_of(self.c,Subject('student','S2')))

    def test_linked_application_cannot_bypass_draft_plan_confirmation(self):
        from higher_education_repository import HigherEducationValidationError
        plan=self.repo.create_plan(1,1,'t')
        item=self.repo.add_plan_item(plan,self.f.university,'Bachelor','SYNTHETIC DRAFT DEGREE','2027-02',1,'t')
        with self.assertRaises(HigherEducationValidationError):
            self.repo.create_application(1,self.f.university,'Bachelor','SYNTHETIC DRAFT DEGREE','2027-02',
                'Direct',1,'t',source_plan_item_id=item)
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM higher_education_applications').fetchone()[0])
