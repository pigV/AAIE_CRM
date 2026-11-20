import unittest
from unittest.mock import patch

from tests import test_business_change_higher_education as fixture
from business_changes.contracts import Subject
from business_changes.versions import version_of


class HigherEducationMasterChangeTests(unittest.TestCase):
    setUp=fixture.HigherEducationChangeTests.setUp

    def test_shared_provider_pathway_and_course_ports_publish_module_changes(self):
        university=self.repo.create_provider('SYNTHETIC MASTER UNIVERSITY','University',1,'t')
        self.repo.review_provider(university,'Approved',1,'t')
        college=self.repo.create_provider('SYNTHETIC MASTER COLLEGE','Pathway Provider',1,'t',review_status='Approved')
        self.repo.link_pathway(university,college,'Partner',1,'t')
        pathway=self.repo.create_pathway_combination(university,'synthetic','SYNTHETIC PATH','Bachelor',1,'t')
        self.repo.add_pathway_segment(pathway,1,university,'Bachelor',1,'t')
        course,_=self.repo.create_or_get_course(university,'SYNTHETIC DEGREE','Bachelor',1,'t')
        self.repo.set_course_status(course,'Inactive',1,'t')
        events=[row[0] for row in self.c.execute('SELECT event_type FROM business_change_events ORDER BY seq')]
        self.assertEqual(['higher_education.provider_created','higher_education.provider_reviewed',
            'higher_education.provider_created','higher_education.provider_relationship_created',
            'higher_education.pathway_created','higher_education.pathway_segment_added',
            'higher_education.course_created','higher_education.course_status_changed'],events)
        self.assertEqual(8,version_of(self.c,Subject('module','higher_education')))

    def test_course_status_invalidates_only_actual_cycle_dependants(self):
        course,_=self.repo.create_or_get_course(self.f.university,'SYNTHETIC OWNED COURSE','Bachelor',1,'t')
        plan=self.repo.create_plan(1,1,'t')
        item=self.repo.add_plan_item(plan,self.f.university,'Bachelor','SYNTHETIC OWNED COURSE','2027',1,'t',course_id=course)
        self.c.execute("INSERT INTO students(student_id,application_stream) VALUES('S2','higher_education')")
        before=version_of(self.c,Subject('student','S1'))
        plan_before=version_of(self.c,Subject('he_plan',str(plan)))
        self.repo.set_course_status(course,'Inactive',1,'t')
        self.assertGreater(version_of(self.c,Subject('student','S1')),before)
        self.assertGreater(version_of(self.c,Subject('he_plan',str(plan))),plan_before)
        self.assertEqual(0,version_of(self.c,Subject('student','S2')))
        self.assertEqual('Inactive',self.c.execute('SELECT active_status FROM higher_education_courses WHERE id=?',(course,)).fetchone()[0])

    def test_merge_failure_rolls_back_course_references_and_master_state(self):
        duplicate,_=self.repo.create_or_get_course(self.f.university,'SYNTHETIC DUPLICATE','Bachelor',1,'t')
        canonical,_=self.repo.create_or_get_course(self.f.university,'SYNTHETIC CANONICAL','Bachelor',1,'t')
        plan=self.repo.create_plan(1,1,'t')
        item=self.repo.add_plan_item(plan,self.f.university,'Bachelor','SYNTHETIC DUPLICATE','2027',1,'t',course_id=duplicate)
        before=self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0]
        with patch('business_changes.commands._append',side_effect=OSError('synthetic master event failure')):
            with self.assertRaises(OSError):self.repo.merge_course(duplicate,canonical,1,'t')
        self.assertEqual(duplicate,self.c.execute('SELECT course_id FROM higher_education_plan_items WHERE id=?',(item,)).fetchone()[0])
        self.assertEqual('Active',self.c.execute('SELECT active_status FROM higher_education_courses WHERE id=?',(duplicate,)).fetchone()[0])
        self.assertEqual(before,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])

    def test_reusing_course_does_not_publish_a_change(self):
        course,created=self.repo.create_or_get_course(self.f.university,'SYNTHETIC REUSED','Bachelor',1,'t')
        self.assertTrue(created)
        before=self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0]
        self.assertEqual((course,False),self.repo.create_or_get_course(self.f.university,'  synthetic reused  ','Bachelor',1,'t'))
        self.assertEqual(before,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])

    def test_package_segment_course_is_an_actual_application_dependency(self):
        pathway=self.repo.create_pathway_combination(self.f.university,'synthetic-package','SYNTHETIC Foundation + Bachelor','Bachelor',1,'t')
        self.repo.add_pathway_segment(pathway,1,self.f.college,'Foundation',1,'t')
        self.repo.add_pathway_segment(pathway,2,self.f.university,'Bachelor',1,'t')
        foundation,_=self.repo.create_or_get_course(self.f.university,'SYNTHETIC FOUNDATION','Foundation',1,'t')
        degree,_=self.repo.create_or_get_course(self.f.university,'SYNTHETIC DEGREE','Bachelor',1,'t')
        application=self.repo.create_application(1,self.f.university,'Package','SYNTHETIC DEGREE','2027','Direct',1,'t',
            pathway_combination_id=pathway,course_id=degree)
        self.c.execute('UPDATE higher_education_package_segments SET course_id=? WHERE application_id=? AND segment_order=1',(foundation,application))
        before=version_of(self.c,Subject('student','S1'))
        self.repo.set_course_status(foundation,'Inactive',1,'t')
        self.assertGreater(version_of(self.c,Subject('student','S1')),before)
        self.assertGreater(version_of(self.c,Subject('he_application',str(application))),1)
