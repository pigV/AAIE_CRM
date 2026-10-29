import sqlite3
import unittest
from datetime import datetime, timezone

from contract_student_selector import (
    consume_handoff_token,
    contract_tab_url,
    issue_handoff_token,
    recommend_students,
    search_students,
    student_confirmation,
)


class SqliteHandle:
    """The small all/one protocol accepted by the selector module."""

    def __init__(self, connection):
        self.connection = connection
        self.connection.row_factory = sqlite3.Row

    def all(self, sql, params=()):
        return self.connection.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        return self.connection.execute(sql, params).fetchone()


class ContractStudentSelectorTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.db = SqliteHandle(self.connection)
        self.connection.executescript(
            """
            CREATE TABLE students (
                student_id TEXT PRIMARY KEY,
                passport_first_name TEXT NOT NULL,
                passport_last_name TEXT NOT NULL,
                english_nickname TEXT,
                source_token TEXT,
                created_date TEXT NOT NULL,
                deleted_at TEXT
            );
            CREATE TABLE parents (
                id INTEGER PRIMARY KEY,
                student_id TEXT NOT NULL,
                passport_first_name TEXT,
                passport_last_name TEXT,
                mobile TEXT
            );
            CREATE TABLE contracts (
                id INTEGER PRIMARY KEY,
                student_id TEXT NOT NULL,
                contract_category TEXT NOT NULL,
                contract_status TEXT NOT NULL
            );
            """
        )
        self.add_student("101", "Parent", "Priority", "2026-08-08", source_token="form-1")
        self.add_student("102", "Recent", "Plain", "2026-08-09")
        self.add_student("103", "Older", "Student", "2026-07-09")
        self.add_student("104", "Contract", "Holder", "2026-08-09", source_token="form-2")
        self.add_student("105", "Deleted", "Student", "2026-08-09", deleted_at="2026-08-09")
        self.connection.execute(
            "INSERT INTO contracts(student_id, contract_category, contract_status) VALUES(?,?,?)",
            ("104", "留学合同", "Draft"),
        )
        self.connection.commit()

    def tearDown(self):
        self.connection.close()

    def add_student(self, student_id, first, last, created_date, *, nickname=None, source_token=None, deleted_at=None):
        self.connection.execute(
            """INSERT INTO students(
                    student_id, passport_first_name, passport_last_name, english_nickname,
                    source_token, created_date, deleted_at
                ) VALUES(?,?,?,?,?,?,?)""",
            (student_id, first, last, nickname, source_token, created_date, deleted_at),
        )

    def test_recommendations_prioritize_parent_forms_and_exclude_ineligible_students(self):
        candidates = recommend_students(self.db, now="2026-08-09T12:00:00+00:00")

        self.assertEqual([item.student_id for item in candidates], ["101", "102"])

    def test_recommendations_are_newest_first_within_priority_group_and_limited_to_five(self):
        for number, created_date in enumerate(
            ("2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06"),
            start=201,
        ):
            self.add_student(str(number), "Recent", str(number), created_date, source_token="parent-form")
        self.connection.commit()

        candidates = recommend_students(
            self.db, now=datetime(2026, 8, 9, tzinfo=timezone.utc), limit=99
        )

        self.assertEqual([item.student_id for item in candidates], ["101", "206", "205", "204", "203"])

    def test_recommendations_use_the_exact_thirty_day_timestamp_cutoff(self):
        self.add_student("106", "Too", "Old", "2026-07-10T11:59:59+00:00")
        self.add_student("107", "At", "Cutoff", "2026-07-10T12:00:00+00:00")
        self.connection.commit()

        candidates = recommend_students(self.db, now="2026-08-09T12:00:00+00:00")

        self.assertIn("107", [item.student_id for item in candidates])
        self.assertNotIn("106", [item.student_id for item in candidates])

    def test_recommendations_can_be_empty(self):
        self.connection.execute("UPDATE students SET deleted_at='2026-08-09'")
        self.connection.commit()

        self.assertEqual(recommend_students(self.db, now="2026-08-09"), [])

    def test_searches_student_and_parent_supported_fields_with_default_bound(self):
        self.add_student("106", "Alice", "Passport", "2026-08-08", nickname="Ali")
        self.connection.execute(
            "INSERT INTO parents(student_id, passport_first_name, passport_last_name, mobile) VALUES(?,?,?,?)",
            ("106", "Mum", "Smith", "0400123456"),
        )
        for number in range(300, 310):
            self.add_student(str(number), "Result", "Student", "2026-08-08")
        self.connection.commit()

        self.assertEqual([item.student_id for item in search_students(self.db, "Alice")], ["106"])
        self.assertEqual([item.student_id for item in search_students(self.db, "Passport")], ["106"])
        self.assertEqual([item.student_id for item in search_students(self.db, "Ali")], ["106"])
        self.assertEqual([item.student_id for item in search_students(self.db, "Mum")], ["106"])
        self.assertEqual([item.student_id for item in search_students(self.db, "Smith")], ["106"])
        self.assertEqual([item.student_id for item in search_students(self.db, "0400123456")], ["106"])
        self.assertEqual([item.student_id for item in search_students(self.db, "106")], ["106"])
        self.assertEqual(len(search_students(self.db, "Result", limit=99)), 8)

    def test_confirmation_reports_matching_non_deleted_contract_count(self):
        self.connection.executemany(
            "INSERT INTO contracts(student_id, contract_category, contract_status) VALUES(?,?,?)",
            [("101", "留学合同", "Draft"), ("101", "服务合同", "Signed"), ("101", "留学合同", "Deleted")],
        )
        self.connection.execute(
            "INSERT INTO parents(student_id, passport_first_name, passport_last_name, mobile) VALUES(?,?,?,?)",
            ("101", "Parent", "One", "0400000000"),
        )
        self.connection.commit()

        confirmation = student_confirmation(self.db, "101")

        self.assertEqual(confirmation.student_id, "101")
        self.assertEqual(confirmation.display_name, "Parent Priority")
        self.assertEqual(confirmation.parent_name, "Parent One")
        self.assertEqual(confirmation.contract_count, 2)

    def test_confirmation_fails_closed_for_invalid_or_deleted_student_ids(self):
        self.assertIsNone(student_confirmation(self.db, ""))
        self.assertIsNone(student_confirmation(self.db, "ABC"))
        self.assertIsNone(student_confirmation(self.db, "105"))

    def test_handoff_url_and_token_are_student_specific_and_single_use(self):
        self.assertEqual(contract_tab_url("101"), "/students/101?tab=contracts#contracts-section")
        self.assertIsNone(contract_tab_url("not-a-student"))

        token = issue_handoff_token("101")
        self.assertTrue(consume_handoff_token(token, "101"))
        self.assertFalse(consume_handoff_token(token, "101"))
        self.assertFalse(consume_handoff_token(token, "102"))


if __name__ == "__main__":
    unittest.main()
