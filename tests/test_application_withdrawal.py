import json
import sqlite3
import unittest

from application_withdrawal import (
    count_withdrawn_students,
    list_withdrawn_applications,
    reapplication_cycle,
    withdraw_application,
)


class ApplicationWithdrawalReportTests(unittest.TestCase):
    def test_withdrawal_is_scoped_and_repeatable(self):
        from tests.test_workflow_application_cycles_migration import (
            NOW, apply_workflow_migration, build_legacy_fixture,
        )
        from application_cycle import create_student_cycle, record_application_lifecycle_event
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            connection = build_legacy_fixture(Path(directory) / "withdrawal.sqlite")
            try:
                apply_workflow_migration(connection)
                connection.row_factory = sqlite3.Row
                cycle = create_student_cycle(connection, "S1", "initial", NOW, 1)
                connection.execute("ALTER TABLE students ADD deleted_at TEXT")
                connection.execute("ALTER TABLE students ADD passport_first_name TEXT")
                connection.execute("ALTER TABLE students ADD passport_last_name TEXT")
                connection.execute("ALTER TABLE contract_signing_links ADD invalidated_at TEXT")
                connection.execute("ALTER TABLE contract_signing_links ADD invalidated_reason TEXT")
                connection.execute("ALTER TABLE contract_signing_links ADD updated_at TEXT")
                connection.execute("ALTER TABLE school_plan_report_links ADD invalidated_at TEXT")
                connection.execute("ALTER TABLE school_plan_report_links ADD invalidated_reason TEXT")
                connection.executescript("""
                    CREATE TABLE reminders (student_id TEXT, related_record_id TEXT,
                        status TEXT, updated_by INTEGER, updated_at TEXT,
                        source_type TEXT, auto_generated INTEGER);
                    CREATE TABLE audit_logs (id INTEGER PRIMARY KEY, user_id INTEGER,
                        username TEXT, action_type TEXT, module_name TEXT, module_key TEXT,
                        student_id TEXT, record_id TEXT, status TEXT, new_value TEXT, created_at TEXT);
                    CREATE TABLE visa_information_collection_respondents (
                        visa_case_id INTEGER, token_status TEXT, invalidated_at TEXT, updated_at TEXT);
                    INSERT INTO visa_information_collection_respondents VALUES (50,'Active',NULL,NULL);
                    INSERT INTO contract_signing_links(id,contract_id,student_id,status,created_at)
                    VALUES (1,10,'S1','active','2026-11-01'),
                           (2,10,'S1','signed','2026-11-01');
                    INSERT INTO school_plan_report_links(id,report_id,status,created_at)
                    VALUES (1,20,'active','2026-11-01');
                    INSERT INTO reminders VALUES
                        ('S1','10','Pending',NULL,NULL,'contract',1),
                        ('S1','10','Pending',NULL,NULL,'manual',0),
                        ('S1','30','Snoozed',NULL,NULL,'offer',1),
                        ('S2','30','Pending',NULL,NULL,'offer',1);
                """)
                connection.execute("UPDATE contracts SET application_cycle_id=? WHERE id=10", (cycle.id,))
                connection.execute("UPDATE school_plan_reports SET application_cycle_id=? WHERE id=20", (cycle.id,))
                connection.execute("UPDATE offers SET application_cycle_id=? WHERE id=30", (cycle.id,))
                connection.execute("UPDATE visa_cases SET application_cycle_id=? WHERE id=50", (cycle.id,))
                connection.execute(
                    "UPDATE application_cycles SET parent_form_token='parent-token-1' WHERE id=?",
                    (cycle.id,),
                )
                args = dict(student_id="S1", cycle_id=cycle.id, actor_id=1,
                            actor_name="advisor", reason="家庭计划变更", stage="签约", timestamp=NOW)
                with connection:
                    self.assertTrue(withdraw_application(connection, **args))
                    self.assertFalse(withdraw_application(connection, **args))
                self.assertEqual(count_withdrawn_students(connection), 1)
                self.assertEqual(list_withdrawn_applications(connection)[0]["reason"], "家庭计划变更")
                self.assertEqual(
                    [row[0] for row in connection.execute("SELECT status FROM contract_signing_links ORDER BY id")],
                    ["revoked", "signed"],
                )
                self.assertEqual(connection.execute("SELECT status FROM school_plan_report_links").fetchone()[0], "revoked")
                self.assertEqual(connection.execute(
                    "SELECT token_status FROM visa_information_collection_respondents"
                ).fetchone()[0], "Manually Revoked")
                self.assertEqual(
                    [row[0] for row in connection.execute("SELECT status FROM reminders ORDER BY rowid")],
                    ["Cancelled", "Pending", "Cancelled", "Pending"],
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT invalidated_at FROM parent_form_tokens WHERE token='parent-token-1'"
                    ).fetchone()[0],
                    NOW,
                )
                with connection:
                    connection.execute(
                        "INSERT INTO parent_form_tokens(token, student_id) VALUES ('new-form', 'S1')"
                    )
                    new_cycle = record_application_lifecycle_event(
                        connection, "PARENT_FORM_CREATED", occurred_at=NOW,
                        actor_user_id=1, student_id="S1", parent_form_token="new-form",
                    )
                self.assertNotEqual(new_cycle.id, cycle.id)
                self.assertEqual(new_cycle.sequence_no, cycle.sequence_no + 1)
                self.assertEqual(new_cycle.parent_form_token, "new-form")
                self.assertEqual(reapplication_cycle(connection, "new-form", "S1"), new_cycle.id)
                self.assertIsNone(reapplication_cycle(connection, "new-form", "S2"))
                self.assertIsNone(reapplication_cycle(connection, "parent-token-1", "S1"))
                self.assertEqual(
                    connection.execute("SELECT application_cycle_id FROM contracts WHERE id=10").fetchone()[0],
                    cycle.id,
                )
                self.assertEqual(count_withdrawn_students(connection), 1)
            finally:
                connection.close()

    def test_counts_students_once_and_lists_each_explicit_withdrawal(self):
        with sqlite3.connect(":memory:") as connection:
            connection.row_factory = sqlite3.Row
            connection.executescript("""
                CREATE TABLE students (
                    student_id TEXT PRIMARY KEY, passport_first_name TEXT,
                    passport_last_name TEXT
                );
                CREATE TABLE application_cycles (
                    id INTEGER PRIMARY KEY, student_id TEXT, sequence_no INTEGER,
                    cycle_type TEXT, status TEXT, outcome TEXT, closed_at TEXT
                );
                CREATE TABLE audit_logs (
                    id INTEGER PRIMARY KEY, student_id TEXT, record_id TEXT,
                    module_name TEXT, action_type TEXT, status TEXT,
                    new_value TEXT, username TEXT
                );
                INSERT INTO students VALUES ('1','Alex','One'), ('2','Casey','Two');
                INSERT INTO application_cycles VALUES
                    (1,'1',1,'initial','closed','withdrawn','2026-11-01'),
                    (2,'1',2,'initial','closed','withdrawn','2026-11-02'),
                    (3,'1',3,'initial','active',NULL,NULL),
                    (4,'2',1,'initial','closed','withdrawn','2026-11-03'),
                    (5,'2',2,'initial','closed','refused','2026-11-04');
            """)
            connection.executemany(
                """INSERT INTO audit_logs(student_id,record_id,module_name,
                       action_type,status,new_value,username)
                   VALUES (?,?,'application_cycles','application_withdrawn',
                       'success',?,'advisor')""",
                [
                    ("1", "1", json.dumps({"reason": "Family plans", "stage": "选校"})),
                    ("1", "2", json.dumps({"reason": "Parent confirmed", "stage": "签约"})),
                    ("2", "5", json.dumps({"reason": "Refused", "stage": "签证"})),
                ],
            )
            self.assertEqual(count_withdrawn_students(connection), 1)
            records = list_withdrawn_applications(connection)
            self.assertEqual([record["cycle_id"] for record in records], [2, 1])
            self.assertEqual(records[0]["reason"], "Parent confirmed")
            self.assertEqual(records[0]["stage"], "签约")


if __name__ == "__main__":
    unittest.main()
