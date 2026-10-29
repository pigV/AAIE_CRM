import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from database import Database


class ApplicationMaterialCompletionResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir="tmp")
        self.database_path = Path(self.temp.name) / "completion.sqlite3"
        self.connection = sqlite3.connect(self.database_path)
        self.connection.executescript(
            """
            CREATE TABLE students(
              student_id TEXT PRIMARY KEY,
              applying_year_level TEXT
            );
            CREATE TABLE parent_form_tokens(
              token TEXT PRIMARY KEY,
              student_id TEXT,
              submitted_at TEXT
            );
            CREATE TABLE application_cycles(
              id INTEGER PRIMARY KEY,
              student_id TEXT,
              parent_form_token TEXT,
              cycle_type TEXT,
              status TEXT
            );
            CREATE TABLE workflow_status(
              student_id TEXT PRIMARY KEY,
              materials_checked INTEGER NOT NULL DEFAULT 0,
              materials_checked_by INTEGER,
              materials_checked_at TEXT
            );
            CREATE TABLE student_documents(
              id INTEGER PRIMARY KEY,
              student_id TEXT,
              folder_category TEXT,
              document_type TEXT,
              is_active INTEGER NOT NULL DEFAULT 1
            );
            INSERT INTO students VALUES('S1','Year 9');
            INSERT INTO parent_form_tokens VALUES(
              'parent-s1','S1','2026-08-28T09:00:00+10:00'
            );
            INSERT INTO application_cycles VALUES(
              41,'S1','parent-s1','initial','active'
            );
            INSERT INTO workflow_status(student_id) VALUES('S1');
            """
        )

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def load(self):
        from application_material_completion import (
            load_application_material_completion,
        )

        return load_application_material_completion(self.connection, "S1")

    def upload(self, *document_types):
        self.connection.executemany(
            """INSERT INTO student_documents(
                   student_id,folder_category,document_type,is_active
               ) VALUES('S1','Applications',?,1)""",
            ((document_type,) for document_type in document_types),
        )
        self.connection.commit()

    def complete_review(self):
        self.connection.execute(
            """UPDATE workflow_status
                  SET materials_checked=1,
                      materials_checked_by=7,
                      materials_checked_at='2026-08-28T10:00:00+10:00'
                WHERE student_id='S1'"""
        )
        self.connection.commit()

    def test_parent_form_with_all_required_materials_missing_is_not_completed(self):
        status = self.load()

        self.assertTrue(status.parent_form_submitted)
        self.assertEqual(
            (
                "StudentPassport",
                "BirthCertificate",
                "Transcripts",
                "Vaccination",
            ),
            status.missing_required_document_types,
        )
        self.assertFalse(status.review_completed)
        self.assertFalse(status.completed)
        self.assertFalse(status.can_complete_review)

    def test_partially_uploaded_required_materials_are_not_completed(self):
        self.upload("StudentPassport", "BirthCertificate")

        status = self.load()

        self.assertEqual(
            ("Transcripts", "Vaccination"),
            status.missing_required_document_types,
        )
        self.assertFalse(status.completed)
        self.assertFalse(status.can_complete_review)

    def test_all_required_materials_without_staff_review_are_not_completed(self):
        self.upload(
            "StudentPassport",
            "BirthCertificate",
            "Transcripts",
            "Vaccination",
        )

        status = self.load()

        self.assertEqual((), status.missing_required_document_types)
        self.assertFalse(status.review_completed)
        self.assertFalse(status.completed)
        self.assertTrue(status.can_complete_review)

    def test_staff_review_is_the_final_completed_fact(self):
        self.upload(
            "StudentPassport",
            "BirthCertificate",
            "Transcripts",
            "Vaccination",
        )
        self.complete_review()

        status = self.load()

        self.assertTrue(status.review_completed)
        self.assertEqual(7, status.review_completed_by)
        self.assertEqual(
            "2026-08-28T10:00:00+10:00", status.review_completed_at
        )
        self.assertTrue(status.completed)
        self.assertFalse(status.can_complete_review)

    def test_optional_materials_do_not_block_staff_review_completion(self):
        self.upload(
            "StudentPassport",
            "BirthCertificate",
            "Transcripts",
            "Vaccination",
        )
        self.complete_review()

        status = self.load()

        self.assertNotIn("AEAS", status.required_document_types)
        self.assertNotIn("IDAT", status.required_document_types)
        self.assertNotIn("IELTS", status.required_document_types)
        self.assertNotIn("TOEFL", status.required_document_types)
        self.assertNotIn("Award", status.required_document_types)
        self.assertNotIn("Others", status.required_document_types)
        self.assertTrue(status.completed)

    def test_completed_fact_survives_a_fresh_database_connection(self):
        self.upload(
            "StudentPassport",
            "BirthCertificate",
            "Transcripts",
            "Vaccination",
        )
        self.complete_review()
        self.connection.close()
        self.connection = sqlite3.connect(self.database_path)

        status = self.load()

        self.assertTrue(status.completed)
        self.assertEqual(7, status.review_completed_by)

    def test_existing_review_fact_is_not_auto_revoked_by_later_document_loss(self):
        self.upload(
            "StudentPassport",
            "BirthCertificate",
            "Transcripts",
            "Vaccination",
        )
        self.complete_review()
        self.connection.execute(
            """UPDATE student_documents SET is_active=0
                  WHERE student_id='S1' AND document_type='Vaccination'"""
        )
        self.connection.commit()

        status = self.load()

        self.assertEqual(("Vaccination",), status.missing_required_document_types)
        self.assertTrue(status.review_completed)
        self.assertTrue(status.completed)


class ApplicationMaterialCompletionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.old_db = app_module.db
        self.temp = tempfile.TemporaryDirectory(dir="tmp")
        self.db = Database(Path(self.temp.name) / "app.sqlite3")
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

    def add_student(self, student_id="S1", *, parent_submitted=True):
        stamp = "2026-08-28T09:00:00+10:00"
        self.db.execute(
            """INSERT INTO students(
                   student_id,passport_last_name,passport_first_name,
                   applying_year_level,current_status,nas_status,
                   created_date,updated_at,form_data_json,source_token
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                student_id,
                "Student",
                student_id,
                "Year 9",
                "Active",
                "ready",
                stamp[:10],
                stamp,
                "{}",
                f"parent-{student_id}",
            ),
        )
        self.db.execute(
            """INSERT INTO workflow_status(
                   student_id,current_stage,materials_checked
               ) VALUES(?,?,0)""",
            (student_id, "建档"),
        )
        self.db.execute(
            """INSERT INTO parent_form_tokens(
                   token,student_id,expires_at,submitted_at,
                   created_by,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                f"parent-{student_id}",
                student_id,
                "2026-09-30T00:00:00+10:00",
                stamp if parent_submitted else None,
                1,
                stamp,
            ),
        )
        self.db.execute(
            """INSERT INTO application_cycles(
                   id,student_id,parent_form_token,cycle_type,status
               ) VALUES(41,?,?,?,?)""",
            (student_id, f"parent-{student_id}", "initial", "active"),
        )

    def upload(self, *document_types, student_id="S1"):
        for document_type in document_types:
            self.db.execute(
                """INSERT INTO student_documents(
                       student_id,folder_category,document_type,original_name,
                       stored_name,file_path,version,file_size,uploaded_at,is_active
                   ) VALUES(?,?,?,?,?,?,?,?,?,1)""",
                (
                    student_id,
                    "Applications",
                    document_type,
                    f"{document_type}.pdf",
                    f"{document_type}_V1.pdf",
                    f"/isolated/{student_id}/{document_type}.pdf",
                    1,
                    1,
                    "2026-08-28T09:30:00+10:00",
                ),
            )

    def upload_all_required(self, *, student_id="S1"):
        self.upload(
            "StudentPassport",
            "BirthCertificate",
            "Transcripts",
            "Vaccination",
            student_id=student_id,
        )

    def handler(self):
        handler = object.__new__(app_module.CRMHandler)
        handler.user = {"id": 1, "username": "admin", "role_name": "Admin"}
        handler.csrf = "csrf-token"
        handler.client_address = ("127.0.0.1", 10000)
        handler.headers = {}
        return handler

    def application_html(self, student_id="S1"):
        student = dict(
            self.db.one(
                """SELECT s.*,w.materials_checked,w.materials_checked_at,
                          u.display_name materials_checker_name
                     FROM students AS s
                     JOIN workflow_status AS w ON w.student_id=s.student_id
                     LEFT JOIN users AS u ON u.id=w.materials_checked_by
                    WHERE s.student_id=?""",
                (student_id,),
            )
        )
        with mock.patch.object(
            app_module.nas, "status", return_value={"ready": True}
        ), mock.patch.object(
            app_module, "can_review_module_action", return_value=True
        ):
            return self.handler().student_detail_tab_content(
                student, "applications"
            )

    def workflow_applications(self, student_id="S1"):
        modules = app_module.student_detail_workflow_file_module_statuses(
            student_id
        )
        return next(item for item in modules if item["key"] == "applications")

    def test_parent_form_generated_file_does_not_complete_application_materials(self):
        self.add_student()
        self.upload("StudentInfo")

        application_module = self.workflow_applications()

        self.assertFalse(application_module["completed"])
        self.assertNotEqual("已完成", application_module["status"])

    def test_all_consumers_change_only_after_staff_review(self):
        self.add_student()
        self.upload("StudentInfo")
        self.upload_all_required()

        before_module = self.workflow_applications()
        before_progress = app_module.student_detail_workflow_file_progress_html(
            app_module.student_detail_workflow_file_module_statuses("S1")
        )
        before_checklist = self.application_html()

        self.assertFalse(before_module["completed"])
        self.assertIn('data-module-key="applications"', before_progress)
        self.assertNotIn(
            'workflow-step completed" data-module-key="applications"',
            before_progress,
        )
        self.assertIn(">材料审核完成</button>", before_checklist)
        self.assertNotIn("审核已完成</strong>", before_checklist)

        redirects = []
        handler = self.handler()
        handler.flash_redirect = lambda url, message, kind="ok": redirects.append(
            (url, message, kind)
        )
        with mock.patch.object(
            app_module, "can_review_module_action", return_value=True
        ), mock.patch.object(app_module, "advance_workflow_stage_to"):
            handler.materials_complete("POST", "S1")

        after_module = self.workflow_applications()
        after_progress = app_module.student_detail_workflow_file_progress_html(
            app_module.student_detail_workflow_file_module_statuses("S1")
        )
        after_checklist = self.application_html()

        self.assertTrue(after_module["completed"])
        self.assertIn(
            'workflow-step completed" data-module-key="applications"',
            after_progress,
        )
        self.assertIn("材料审核已完成", after_checklist)
        self.assertIn("审核已完成</strong>", after_checklist)
        self.assertEqual("材料审核已完成", redirects[-1][1])

    def test_server_rejects_review_before_current_parent_form_is_submitted(self):
        self.add_student(parent_submitted=False)
        self.upload_all_required()
        checklist = self.application_html()
        workflow_module = self.workflow_applications()
        redirects = []
        handler = self.handler()
        handler.flash_redirect = lambda url, message, kind="ok": redirects.append(
            (url, message, kind)
        )

        with mock.patch.object(
            app_module, "can_review_module_action", return_value=True
        ), mock.patch.object(app_module, "advance_workflow_stage_to"):
            handler.materials_complete("POST", "S1")

        stored = self.db.one(
            "SELECT materials_checked FROM workflow_status WHERE student_id='S1'"
        )
        review_button = checklist.split(
            'action="/students/S1/materials-complete"', 1
        )[1].split("</button>", 1)[0]
        self.assertEqual("等待 Parent Form 提交", workflow_module["status"])
        self.assertIn("disabled", review_button)
        self.assertIn("家长在线 Parent Form 尚未提交。", checklist)
        self.assertEqual(0, stored["materials_checked"])
        self.assertEqual("家长在线 Parent Form 尚未提交。", redirects[-1][1])
        self.assertEqual("error", redirects[-1][2])

    def test_legacy_review_flag_without_current_parent_submission_is_not_completed(self):
        self.add_student(parent_submitted=False)
        self.upload_all_required()
        self.db.execute(
            """UPDATE workflow_status
                  SET materials_checked=1,materials_checked_by=1,
                      materials_checked_at='2026-08-28T10:00:00+10:00'
                WHERE student_id='S1'"""
        )

        workflow_module = self.workflow_applications()
        checklist = self.application_html()

        self.assertFalse(workflow_module["completed"])
        self.assertNotIn("材料审核已完成", checklist)
        self.assertNotIn("审核已完成</strong>", checklist)


if __name__ == "__main__":
    unittest.main()
