import unittest

import revenue.school_courses as courses


class CoeCourseIdentityTests(unittest.TestCase):
    def test_identifies_school_course_stages_without_date(self):
        cases = {
            "English language (ELICOS)": "language",
            "Secondary Years 7–10 [123456A]": "secondary_7_10",
            "Junior Secondary (7-10) [123456A]": "secondary_7_10",
            "Senior Secondary Years 11–12": "secondary_11_12",
            "Secondary Year 10": "secondary_7_10",
            "Secondary Year 11": "secondary_11_12",
            "Secondary (7-12)": "secondary",
            "Bachelor of Year 12 Education": "",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(expected, courses.school_coe_course_stage(name))

    def test_language_courses_at_same_provider_keep_course_identity(self):
        from current_coe_evidence import course_identity

        def identity(course):
            return course_identity({
                "coe_provider": "Synthetic English Centre",
                "school_name": "",
                "education_stream": "school_education",
                "coe_course": course,
                "coe_start_date": "2027-01-01",
            })

        self.assertNotEqual(identity("General English"), identity("English for Academic Purposes"))
        self.assertNotEqual(identity("General English [12345A]"), identity("General English [12345B]"))
        self.assertEqual(identity("General English [12345A]"), identity("General English (ELICOS) [12345A]"))
        self.assertEqual(identity("English Language"), identity("English Language (ELICOS)"))
