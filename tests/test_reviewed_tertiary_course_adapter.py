import unittest
from tests import test_reviewed_tertiary_course as fixture
from higher_education_workflow_adapter import legacy_higher_education_coe_schools


class ReviewedCourseAdapterTests(unittest.TestCase):
    setUp=fixture.ReviewedTertiaryCourseTests.setUp
    tearDown=fixture.ReviewedTertiaryCourseTests.tearDown
    confirm=fixture.ReviewedTertiaryCourseTests.confirm

    def test_verified_foundation_coe_advances_only_its_accepted_course(self):
        self.c.execute('BEGIN');self.confirm()
        self.assertEqual({10:'Example University'},legacy_higher_education_coe_schools(self.c,[10]))
        self.c.execute("UPDATE coe_oshc SET coe_course='Bachelor Course [115421A]'")
        self.assertFalse(legacy_higher_education_coe_schools(self.c,[10]))

    def test_unreviewed_foundation_does_not_acquire_main_course_authority(self):
        self.assertFalse(legacy_higher_education_coe_schools(self.c,[10]))

    def test_expired_course_review_cannot_advance_current_visa_stage(self):
        self.c.execute("UPDATE coe_oshc SET coe_start_date='2020-02-01',coe_end_date='2021-01-28'")
        self.c.execute("UPDATE offers SET offer_date='2019-09-16',course_start_date='2020-02-01'")
        self.c.execute("UPDATE student_documents SET document_date='2019-09-17' WHERE id=40")
        with self.assertRaises(ValueError):
            self.confirm(course_start_date='2020-02-01',course_end_date='2021-01-28',signed_date='2019-09-17')
        self.assertFalse(legacy_higher_education_coe_schools(self.c,[10]))

    def add_bachelor_coe(self):
        self.c.execute("INSERT INTO coe_oshc VALUES(31,'S',10,'COE',?,'Example University [00026A]','Example University','Bachelor Course [115421A]','2028-02-21','2031-01-21')",(str(self.coe),))

    def test_stale_course_link_never_falls_back_to_other_course_same_university(self):
        self.c.execute('BEGIN');self.confirm();self.add_bachelor_coe()
        self.c.execute("UPDATE coe_oshc SET coe_course='Changed Foundation [022310D]' WHERE id=30")
        self.assertFalse(legacy_higher_education_coe_schools(self.c,[10]))
