import sqlite3
import unittest

from visa_cycle_assignment import link_recorded_visa_grant


class RecordedVisaGrantLinkTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE application_cycles(id, student_id, status, sequence_no, cycle_type, education_stream);
            CREATE TABLE visa_cases(id, student_id, application_cycle_id, archived_at,
                                    lodgement_recorded_at, actual_lodgement_date, visa_type);
            CREATE TABLE visa_result_status_current(visa_case_id, effective_result, conflict);
            CREATE TABLE audit_logs(user_id, username, action_type, module_name, module_key,
                                    record_id, student_id, old_value, new_value, status, created_at);
            INSERT INTO application_cycles VALUES(129,'S1','active',1,'initial','higher_education'),
                (130,'S2','active',1,'initial','higher_education'),(131,'S1','closed',2,'initial','higher_education');
            INSERT INTO visa_cases VALUES(45,'S1',NULL,NULL,'2026-10-06','2026-09-22','500 Student Visa');
            INSERT INTO visa_result_status_current VALUES(45,'Granted',0);
        """)

    def link(self, cycle_id):
        with self.connection:
            return link_recorded_visa_grant(
                self.connection, "S1", 45, cycle_id, actor={"id": 1, "username": "staff"},
                timestamp="2026-11-18",
            )

    def test_only_cycle_link_changes_and_is_audited_once(self):
        for invalid_cycle in ("", 130, 131):
            with self.subTest(cycle_id=invalid_cycle), self.assertRaises(ValueError):
                self.link(invalid_cycle)
        self.assertEqual(129, self.link(129))
        with self.assertRaises(ValueError):
            self.link(129)
        row = self.connection.execute("SELECT * FROM visa_cases WHERE id=45").fetchone()
        self.assertEqual((45, "S1", 129, None, "2026-10-06", "2026-09-22", "500 Student Visa"), tuple(row))
        audit = self.connection.execute("SELECT action_type, record_id, student_id, new_value FROM audit_logs").fetchall()
        self.assertEqual([("visa_application_cycle_linked", 45, "S1", "129")], [tuple(row) for row in audit])

    def test_conflicting_grant_cannot_be_linked(self):
        with self.connection:
            self.connection.execute("UPDATE visa_result_status_current SET conflict=1")
        with self.assertRaises(ValueError):
            self.link(129)
        self.assertIsNone(self.connection.execute("SELECT application_cycle_id FROM visa_cases").fetchone()[0])
        self.assertEqual(0, self.connection.execute("SELECT count(*) FROM audit_logs").fetchone()[0])
