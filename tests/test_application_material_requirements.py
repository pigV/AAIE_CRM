import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from database import Database

from application_material_requirements import (
    missing_required_application_document_types,
    required_application_document_types,
)


class ApplicationMaterialRequirementsPolicyTests(unittest.TestCase):
    def test_birth_certificate_is_optional_only_for_confirmed_exempt_levels(self):
        for level in ("预科+本科", "本科", "研究生", " 本科 "):
            with self.subTest(level=level):
                required = required_application_document_types(level)
                self.assertNotIn("BirthCertificate", required)
                self.assertTrue(
                    {"StudentPassport", "Transcripts", "Vaccination"}
                    .issubset(required)
                )

    def test_birth_certificate_remains_required_for_every_fallback_value(self):
        for level in (
            "Year 1", "Year 6", "Year 12", "不确定", "", None,
            "预科", "Diploma", "future-level",
        ):
            with self.subTest(level=level):
                self.assertIn(
                    "BirthCertificate",
                    required_application_document_types(level),
                )

    def test_missing_result_preserves_catalog_order_and_ignores_optional_birth(self):
        statuses = (
            ("StudentPassport", 0),
            ("BirthCertificate", 0),
            ("Transcripts", 0),
            ("Vaccination", 1),
            ("AEAS", 0),
        )
        self.assertEqual(
            missing_required_application_document_types("本科", statuses),
            ("StudentPassport", "Transcripts"),
        )
        self.assertEqual(
            missing_required_application_document_types("Year 9", statuses),
            ("StudentPassport", "BirthCertificate", "Transcripts"),
        )


class ApplicationMaterialRequirementsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.old_db = app_module.db
        self.temp = tempfile.TemporaryDirectory(dir="tmp")
        self.db = Database(Path(self.temp.name) / "aaie_crm.db")
        self.db.initialize()
        self.db.execute(
            """CREATE TABLE application_cycles(
                   id INTEGER PRIMARY KEY,
                   student_id TEXT,
                   parent_form_token TEXT,
                   cycle_type TEXT,
                   status TEXT
               )"""
        )
        app_module.db = self.db

    def tearDown(self):
        app_module.db = self.old_db
        self.temp.cleanup()

    def add_student(self, student_id, applying_year_level):
        stamp = "2026-08-06T12:00:00+10:00"
        self.db.execute(
            """INSERT INTO students(
                   student_id,passport_last_name,passport_first_name,
                   applying_year_level,current_status,nas_status,
                   created_date,updated_at,form_data_json
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                student_id, "Student", student_id, applying_year_level,
                "Active", "ready", stamp[:10], stamp, "{}",
            ),
        )
        self.db.execute(
            "INSERT INTO workflow_status(student_id,current_stage,materials_checked) VALUES(?,?,0)",
            (student_id, "建档"),
        )
        token = f"parent-{student_id}"
        self.db.execute(
            """INSERT INTO parent_form_tokens(
                   token,student_id,expires_at,submitted_at,created_by,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (token, student_id, stamp, stamp, 1, stamp),
        )
        self.db.execute(
            """INSERT INTO application_cycles(
                   student_id,parent_form_token,cycle_type,status
               ) VALUES(?,?,?,?)""",
            (student_id, token, "initial", "active"),
        )

    def add_required_non_birth_documents(self, student_id):
        for document_type in ("StudentPassport", "Transcripts", "Vaccination"):
            self.db.execute(
                """INSERT INTO student_documents(
                       student_id,folder_category,document_type,original_name,
                       stored_name,file_path,version,file_size,uploaded_at,is_active
                   ) VALUES(?,?,?,?,?,?,?,?,?,1)""",
                (
                    student_id, "Applications", document_type,
                    f"{document_type}.pdf", f"{document_type}_V1.pdf",
                    f"/isolated/{student_id}/{document_type}.pdf",
                    1, 1, "2026-08-06T12:00:00+10:00",
                ),
            )

    def handler(self):
        handler = object.__new__(app_module.CRMHandler)
        handler.user = {"id": 1, "username": "admin", "role_name": "Admin"}
        handler.csrf = "csrf-token"
        handler.client_address = ("127.0.0.1", 10000)
        handler.headers = {}
        return handler

    def application_html(self, student_id):
        student = dict(
            self.db.one(
                """SELECT s.*,w.materials_checked,w.materials_checked_at,
                          u.display_name materials_checker_name
                   FROM students s
                   JOIN workflow_status w ON w.student_id=s.student_id
                   LEFT JOIN users u ON u.id=w.materials_checked_by
                   WHERE s.student_id=?""",
                (student_id,),
            )
        )
        with mock.patch.object(app_module.nas, "status", return_value={"ready": True}), \
             mock.patch.object(app_module, "can_review_module_action", return_value=True):
            return self.handler().student_detail_tab_content(student, "applications")

    def test_application_checklist_marks_birth_optional_only_for_exempt_level(self):
        self.add_student("HIGHER001", "本科")
        exempt_html = self.application_html("HIGHER001")
        birth_row = exempt_html.split("Birth Certificate", 1)[1].split("</div>", 1)[0]
        self.assertNotIn("*", birth_row)
        self.assertIn("学生护照 *", exempt_html)
        self.assertNotIn("缺少：Birth Certificate", exempt_html)

        self.add_student("SCHOOL001", "Year 9")
        school_html = self.application_html("SCHOOL001")
        birth_row = school_html.split("Birth Certificate", 1)[1].split("</div>", 1)[0]
        self.assertIn("*", birth_row)

    def test_material_completion_allows_exempt_level_and_blocks_year_level(self):
        redirects = []
        for student_id, level in (("HIGHER002", "研究生"), ("SCHOOL002", "Year 10")):
            self.add_student(student_id, level)
            self.add_required_non_birth_documents(student_id)

        handler = self.handler()
        handler.flash_redirect = lambda url, message, kind="ok": redirects.append(
            (url, message, kind)
        )
        with mock.patch.object(app_module, "can_review_module_action", return_value=True), \
             mock.patch.object(app_module, "advance_workflow_stage_to"):
            handler.materials_complete("POST", "HIGHER002")
            handler.materials_complete("POST", "SCHOOL002")

        self.assertEqual(
            self.db.one("SELECT materials_checked FROM workflow_status WHERE student_id='HIGHER002'")["materials_checked"],
            1,
        )
        self.assertEqual(
            self.db.one("SELECT materials_checked FROM workflow_status WHERE student_id='SCHOOL002'")["materials_checked"],
            0,
        )
        self.assertIn("缺少：Birth Certificate", redirects[-1][1])

    def test_workflow_birth_alert_follows_application_level_policy(self):
        base = {
            "materials_checked": 0,
            "form_data_json": "{}",
            "current_year_level": "Year 9",
        }
        exempt = {
            **base,
            "student_id": "HIGHER003",
            "applying_year_level": "预科+本科",
        }
        school = {
            **base,
            "student_id": "SCHOOL003",
            "applying_year_level": "Year 9",
        }
        exempt_keys = {
            item["key"]
            for item in app_module.workflow_alert_tags(
                exempt, {"StudentPassport"}
            )
        }
        school_keys = {
            item["key"]
            for item in app_module.workflow_alert_tags(
                school, {"StudentPassport"}
            )
        }
        self.assertNotIn("missing_birth", exempt_keys)
        self.assertIn("missing_birth", school_keys)

    def test_missing_docs_filter_excludes_exempt_birth_only_student(self):
        self.add_student("HIGHER004", "本科")
        self.add_student("SCHOOL004", "Year 11")
        for student_id in ("HIGHER004", "SCHOOL004"):
            self.db.execute(
                """INSERT INTO student_documents(
                       student_id,folder_category,document_type,original_name,
                       stored_name,file_path,version,file_size,uploaded_at,is_active
                   ) VALUES(?,?,?,?,?,?,?,?,?,1)""",
                (
                    student_id, "Applications", "StudentPassport",
                    "passport.pdf", "StudentPassport_V1.pdf",
                    f"/isolated/{student_id}/passport.pdf",
                    1, 1, "2026-08-06T12:00:00+10:00",
                ),
            )

        admin = dict(
            self.db.one(
                """SELECT u.*,r.name role_name,r.permissions_json
                   FROM users u JOIN roles r ON r.id=u.role_id
                   WHERE u.username='admin'"""
            )
        )
        admin["module_permissions_json"] = app_module.modules_json_for_user(
            admin["id"]
        )
        handler = self.handler()
        handler.user = admin
        handler.query = {"alert": ["missing_docs"], "sort": ["waiting"]}
        handler.require = lambda permission: True
        captured = {}
        handler.send_html = lambda html, status=200, headers=None: captured.update(
            {"html": html, "status": status}
        )
        handler.error_page = lambda message, status=400: captured.update(
            {"error": message, "status": status}
        )

        with mock.patch.object(
            app_module.nas, "status", return_value={"ready": True}
        ):
            handler.students("GET")

        self.assertEqual(captured["status"], 200)
        table_html = captured["html"].split(
            '<table class="student-management-table">', 1
        )[1]
        table_body = table_html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
        self.assertNotIn("HIGHER004 Student", table_body)
        self.assertIn("SCHOOL004 Student", table_body)
        self.assertNotIn("缺少出生证明", app_module.workflow_alerts_html(
            app_module.workflow_alert_tags(
                {
                    "materials_checked": 0,
                    "form_data_json": "{}",
                    "applying_year_level": "本科",
                },
                {"StudentPassport"},
            )
        ))


if __name__ == "__main__":
    unittest.main()
