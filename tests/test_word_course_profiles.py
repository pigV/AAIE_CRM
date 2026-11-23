import unittest
from revenue.school_courses import read_course_fields, render_course_fields

class CourseProfilesTests(unittest.TestCase):
    def form(self,kind='language',sector='public'):
        return {'course_mode_0':kind,f'course_{kind}_school_0':'40',f'course_{kind}_name_0':'English' if kind=='language' else 'Secondary',
            f'course_{kind}_aud_0':'9250',f'course_{kind}_confirmed_0':'1',f'course_{kind}_reference_0':'page 1',
            f'course_{kind}_sector_0':sector,f'course_{kind}_start_date_0':'2027-02-03',
            f'course_{kind}_student_number_0':'2487510',f'course_{kind}_authority_state_0':'NSW',
            'course_language_offer_0':'85','course_main_year_0':'2027','course_main_grade_0':'Year 9'}

    def test_public_language_keeps_offer_school_id_and_whole_course_amount(self):
        entry=read_course_fields(self.form(),0)[0]
        self.assertEqual(('public','2027-02-03','2487510','NSW',925000),
            (entry['sector'],entry['start_date'],entry['student_number'],entry['authority_state'],entry['cents']))

    def test_private_language_does_not_store_public_authority_or_school_number(self):
        entry=read_course_fields(self.form(sector='private'),0)[0]
        self.assertEqual(('', ''),(entry['authority_state'],entry['student_number']))

    def test_invalid_start_and_missing_public_student_number_are_rejected(self):
        form=self.form();form['course_language_start_date_0']='2027-02-30'
        with self.assertRaisesRegex(ValueError,'入学日期'):read_course_fields(form,0)
        form=self.form();form['course_language_student_number_0']=''
        with self.assertRaisesRegex(ValueError,'学号'):read_course_fields(form,0)

    def test_main_has_first_year_amount_and_independent_school_number(self):
        entry=read_course_fields(self.form('main'),0)[0]
        self.assertEqual((2027,'Year 9',925000),(entry['year'],entry['grade'],entry['cents']))
        self.assertEqual('2487510',entry['student_number'])
