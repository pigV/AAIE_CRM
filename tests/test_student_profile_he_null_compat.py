import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import app as app_module
from database import Database


ROOT = Path(__file__).resolve().parents[1]
LEGAL_HE_COURSE_TYPES = (
    "Foundation",
    "Diploma",
    "Bachelor",
    "Postgraduate",
    "Package",
)


class StudentProfileHigherEducationNullCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=ROOT)
        self.database = Database(Path(self.temp_dir.name) / "aaie_crm.db")
        self.database.initialize()
        with self.database.transaction() as conn:
            conn.execute(
                """ALTER TABLE students ADD COLUMN application_stream TEXT
                   CHECK(application_stream IS NULL OR application_stream IN ('school_education','higher_education'))"""
            )
            conn.execute(
                """ALTER TABLE students ADD COLUMN higher_education_course_type TEXT
                   CHECK(higher_education_course_type IS NULL OR higher_education_course_type IN
                   ('Foundation','Diploma','Bachelor','Postgraduate','Package'))"""
            )
            conn.execute(
                "ALTER TABLE students ADD COLUMN higher_education_package_combination TEXT"
            )
            conn.execute(
                "ALTER TABLE students ADD COLUMN parent_requested_pathway TEXT"
            )
        self.old_database = app_module.db
        app_module.db = self.database
        self.admin = dict(
            self.database.one(
                """SELECT u.*,r.name role_name,r.permissions_json
                     FROM users u JOIN roles r ON r.id=u.role_id
                    WHERE u.username='admin'"""
            )
        )

    def tearDown(self):
        app_module.db = self.old_database
        self.temp_dir.cleanup()

    def student_data(self, **overrides):
        data = {
            "passport_last_name": "Student",
            "passport_first_name": "K12",
            "application_stream": "school_education",
            "higher_education_course_type": "",
            "higher_education_package_combination": "",
            "preferred_intake_year": "2027",
            "preferred_intake_term": "10",
            "applying_year_level": "Year 9",
            "preferred_cities": ["melbourne"],
            "account_password": "",
        }
        data.update(overrides)
        return data

    def insert_student(self, student_id="K12-NULL", **overrides):
        data = self.student_data(**overrides)
        stored_data = dict(data)
        stored_data.pop("account_password", None)
        self.database.execute(
            """INSERT INTO students(
                   student_id,passport_last_name,passport_first_name,current_status,
                   created_date,updated_at,form_data_json,application_stream,
                   higher_education_course_type,higher_education_package_combination,
                   parent_requested_pathway,preferred_intake_year,preferred_intake_term,
                   applying_year_level,preferred_cities_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                student_id,
                data["passport_last_name"],
                data["passport_first_name"],
                "Active",
                "2026-08-18T00:00:00+10:00",
                "2026-08-18T00:00:00+10:00",
                json.dumps(stored_data, ensure_ascii=False),
                data["application_stream"],
                None,
                data["higher_education_package_combination"] or None,
                None,
                data["preferred_intake_year"],
                data["preferred_intake_term"],
                data["applying_year_level"],
                json.dumps(data["preferred_cities"], ensure_ascii=False),
            ),
        )
        return self.database.one("SELECT * FROM students WHERE student_id=?", (student_id,))

    def update(self, student, data):
        return app_module.update_student_profile(
            student,
            data,
            self.admin,
            "127.0.0.1",
            "StudentProfileNullCompatibilityTest",
        )

    def test_k12_null_course_type_renders_edit_profile(self):
        student = self.insert_student()

        values = app_module.student_form_values(student)
        html = app_module.student_edit_form("/students/K12-NULL/edit", "csrf", values)

        self.assertEqual(values["higher_education_course_type"], "")
        self.assertIn('name="higher_education_course_type"', html)
        self.assertIn('<option value="">请选择</option>', html)

    def test_k12_year_update_keeps_course_type_null(self):
        student = self.insert_student()
        submitted = app_module.normalize_form(
            self.student_data(preferred_intake_year="2028")
        )

        self.update(student, submitted)

        refreshed = self.database.one(
            "SELECT preferred_intake_year,higher_education_course_type FROM students WHERE student_id=?",
            (student["student_id"],),
        )
        self.assertEqual(refreshed["preferred_intake_year"], "2028")
        self.assertIsNone(refreshed["higher_education_course_type"])

    def test_k12_term_update_keeps_course_type_null(self):
        student = self.insert_student()
        submitted = app_module.normalize_form(
            self.student_data(preferred_intake_term="02")
        )

        self.update(student, submitted)

        refreshed = self.database.one(
            "SELECT preferred_intake_term,higher_education_course_type FROM students WHERE student_id=?",
            (student["student_id"],),
        )
        self.assertEqual(refreshed["preferred_intake_term"], "02")
        self.assertIsNone(refreshed["higher_education_course_type"])

    def test_post_empty_course_type_normalizes_to_none(self):
        submitted = app_module.normalize_form(
            {"higher_education_course_type": ""}
        )

        self.assertIsNone(submitted["higher_education_course_type"])

    def test_post_whitespace_and_placeholder_course_types_normalize_to_none(self):
        for value in (" ", "请选择", "未选择", None):
            with self.subTest(value=value):
                submitted = app_module.normalize_form(
                    {"higher_education_course_type": value}
                )
                self.assertIsNone(submitted["higher_education_course_type"])

    def test_all_legal_higher_education_course_types_still_save(self):
        student = self.insert_student(student_id="HE-LEGAL")
        for course_type in LEGAL_HE_COURSE_TYPES:
            with self.subTest(course_type=course_type):
                submitted = app_module.normalize_form(
                    self.student_data(
                        application_stream="higher_education",
                        higher_education_course_type=course_type,
                        higher_education_package_combination=(
                            "Foundation + Bachelor" if course_type == "Package" else ""
                        ),
                    )
                )
                self.update(
                    self.database.one(
                        "SELECT * FROM students WHERE student_id=?", (student["student_id"],)
                    ),
                    submitted,
                )
                stored = self.database.one(
                    "SELECT higher_education_course_type FROM students WHERE student_id=?",
                    (student["student_id"],),
                )
                self.assertEqual(stored["higher_education_course_type"], course_type)

    def test_illegal_higher_education_course_type_is_still_rejected(self):
        submitted = app_module.normalize_form(
            self.student_data(
                application_stream="higher_education",
                higher_education_course_type="ABC",
            )
        )

        self.assertIn(
            "higher_education_course_type",
            app_module.missing_student_fields(submitted),
        )

    def test_constraint_failure_rolls_back_every_profile_write(self):
        student = self.insert_student(student_id="K12-ROLLBACK")
        before = dict(
            self.database.one(
                "SELECT * FROM students WHERE student_id=?", (student["student_id"],)
            )
        )
        submitted = self.student_data(
            preferred_intake_year="2029",
            higher_education_course_type="ABC",
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.update(student, submitted)

        after = dict(
            self.database.one(
                "SELECT * FROM students WHERE student_id=?", (student["student_id"],)
            )
        )
        self.assertEqual(after, before)
        self.assertEqual(
            self.database.one(
                "SELECT COUNT(*) total FROM student_change_logs WHERE student_id=?",
                (student["student_id"],),
            )["total"],
            0,
        )


if __name__ == "__main__":
    unittest.main()
