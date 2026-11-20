import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from application_cycle import CycleConflictError, attach_contract, record_application_lifecycle_event
from application_withdrawal import (
    ApplicationWithdrawalConflict,
    count_withdrawn_students,
    withdraw_application,
)
from database import Database
from workflow_facts import load_workflow_facts
from workflow_resolver import resolve_workflow_stage


class ParentReapplicationTests(unittest.TestCase):
    def test_fresh_submission_retains_student_identity_and_old_cycle(self):
        stamp = "2026-11-16T10:00:00+11:00"
        student_id = "20269901"
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "reapplication.sqlite")
            database.initialize()
            with database.transaction() as connection:
                connection.execute(
                    "CREATE TABLE student_cases (id INTEGER PRIMARY KEY, student_id TEXT, case_type TEXT)"
                )
                for migration in (
                    "20260805_012_parent_form_secure_draft.sql",
                    "20260811_017_workflow_application_cycles.sql",
                    "20260813_018_higher_education_application_module.sql",
                    "20260813_019_higher_education_module_v2.sql",
                ):
                    connection.executescript((Path(__file__).resolve().parents[1] / "migrations" / migration).read_text())
                actor_id = connection.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
                connection.execute(
                    """INSERT INTO students(student_id,passport_first_name,passport_last_name,
                                             created_date,updated_at,form_data_json)
                       VALUES (?,'Alex','Example',?,?,'{}')""",
                    (student_id, stamp, stamp),
                )
                for token in ("old-form", "fresh-form"):
                    connection.execute(
                        """INSERT INTO parent_form_tokens(token,student_id,expires_at,created_by,created_at)
                           VALUES (?,?,'2099-01-01T00:00:00+00:00',?,?)""",
                        (token, student_id, actor_id, stamp),
                    )
                old = record_application_lifecycle_event(
                    connection, "PARENT_FORM_CREATED", student_id=student_id,
                    parent_form_token="old-form", occurred_at=stamp, actor_user_id=actor_id,
                )
                connection.execute("UPDATE parent_form_tokens SET submitted_at=? WHERE token='old-form'", (stamp,))
                connection.execute(
                    """INSERT INTO contracts(student_id,contract_category,contract_type,contract_title,
                       contract_status,fully_executed_at,updated_at,application_cycle_id)
                       VALUES (?,'study','study','Original contract','Signed',?,?,?)""",
                    (student_id, stamp, stamp, old.id),
                )
                withdraw_application(
                    connection, student_id=student_id, cycle_id=old.id, actor_id=actor_id,
                    actor_name="admin", reason="家庭计划变更", stage="签约", timestamp=stamp,
                )
                new_contract_id = connection.execute(
                    """INSERT INTO contracts(student_id,contract_category,contract_type,contract_title,updated_at)
                       VALUES (?,'study','study','Fresh contract',?)""",
                    (student_id, stamp),
                ).lastrowid
                with self.assertRaises(CycleConflictError):
                    attach_contract(connection, new_contract_id, student_id, stamp, actor_id)
                fresh = record_application_lifecycle_event(
                    connection, "PARENT_FORM_CREATED", student_id=student_id,
                    parent_form_token="fresh-form", occurred_at=stamp, actor_user_id=actor_id,
                )
                connection.execute(
                    """INSERT INTO parent_form_drafts(token,payload_encrypted,revision,save_source,
                                                       last_saved_at,created_at,updated_at)
                       VALUES ('fresh-form','fixture-only',2,'manual',?,?,?)""",
                    (stamp, stamp, stamp),
                )
                fresh_facts = load_workflow_facts(connection, (fresh.id,))[fresh.id]
                self.assertFalse(fresh_facts.parent_form_submitted)
                self.assertFalse(fresh_facts.contract_signed)
                self.assertEqual(resolve_workflow_stage(fresh_facts).key, "01_parent_form")
                with self.assertRaises(CycleConflictError):
                    attach_contract(connection, new_contract_id, student_id, stamp, actor_id)
            form = {
                "passport_first_name": "Alex",
                "passport_last_name": "Example",
                "student_email": "fresh@example.invalid",
                "application_stream": "school_education",
            }
            token_row = database.one("SELECT * FROM parent_form_tokens WHERE token='fresh-form'")
            with patch.object(app, "db", database), patch.object(app, "is_workflow_v2_active", return_value=True):
                with self.assertRaises(ApplicationWithdrawalConflict):
                    app.submit_parent_reapplication(form, token_row, 1)
                self.assertEqual(app.submit_parent_reapplication(form, token_row, 2), student_id)
                with self.assertRaises(app.ParentFormSubmitted):
                    app.submit_parent_reapplication(form, token_row, 2)
            self.assertEqual(database.one("SELECT count(*) FROM students")[0], 1)
            self.assertEqual(database.one("SELECT student_email FROM students")[0], "fresh@example.invalid")
            self.assertEqual(database.one("SELECT submitted_at FROM parent_form_tokens WHERE token='old-form'")[0], stamp)
            self.assertIsNotNone(database.one("SELECT submitted_at FROM parent_form_tokens WHERE token='fresh-form'")[0])
            self.assertEqual(database.one("SELECT count(*) FROM parent_form_drafts")[0], 0)
            self.assertEqual(database.one("SELECT status FROM application_cycles WHERE id=?", (old.id,))[0], "closed")
            self.assertEqual(database.one("SELECT status FROM application_cycles WHERE id=?", (fresh.id,))[0], "active")
            with database.read_snapshot() as connection:
                self.assertEqual(count_withdrawn_students(connection), 1)
                facts = load_workflow_facts(connection, (old.id, fresh.id))
                self.assertTrue(facts[old.id].contract_signed)
                self.assertTrue(facts[fresh.id].parent_form_submitted)
                self.assertFalse(facts[fresh.id].contract_signed)
                self.assertEqual(resolve_workflow_stage(facts[fresh.id]).key, "02_contract_creation")
            with database.transaction() as connection:
                self.assertEqual(attach_contract(connection, new_contract_id, student_id, stamp, actor_id).id, fresh.id)
