import sqlite3
import unittest

from student_lifecycle.case_enrolment import (
    bind_case_current_enrolment,
    validate_case_current_enrolment,
)
from student_lifecycle.errors import LifecycleIntegrityError


class CaseCurrentEnrolmentAdapterTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript(
            """
            CREATE TABLE student_cases(id INTEGER PRIMARY KEY,student_id TEXT,case_type TEXT);
            CREATE TABLE student_school_enrolments(
              id INTEGER PRIMARY KEY,student_id TEXT,school_id INTEGER,
              school_name_snapshot TEXT,enrolment_role TEXT,status TEXT);
            CREATE TABLE transfer_cases(
              case_id INTEGER PRIMARY KEY,current_school_id INTEGER,
              current_school_name_snapshot TEXT,current_school_enrolment_id INTEGER,
              upcoming_school_enrolment_id INTEGER,relationship_status TEXT);
            CREATE TABLE progression_cases(
              case_id INTEGER PRIMARY KEY,current_school_id INTEGER,
              current_school_name_snapshot TEXT,current_school_enrolment_id INTEGER);
            INSERT INTO student_cases VALUES(1,'S1','transfer'),(2,'S1','progression');
            INSERT INTO student_school_enrolments
              VALUES(50,'S1',9,'Caulfield Grammar School','current','active');
            INSERT INTO transfer_cases VALUES(1,NULL,'legacy',NULL,NULL,'needs_repair');
            INSERT INTO progression_cases VALUES(2,NULL,'legacy',NULL);
            """
        )
        self.addCleanup(self.connection.close)

    def test_binds_transfer_and_progression_to_same_authoritative_current(self):
        self.assertEqual(50, bind_case_current_enrolment(
            self.connection, case_id=1, lifecycle_type="transfer"
        ))
        self.assertEqual(50, bind_case_current_enrolment(
            self.connection, case_id=2, lifecycle_type="progression"
        ))
        self.assertEqual((9, "Caulfield Grammar School", 50, "current_ready"),
            self.connection.execute(
                "SELECT current_school_id,current_school_name_snapshot,"
                "current_school_enrolment_id,relationship_status FROM transfer_cases"
            ).fetchone())
        self.assertEqual((9, "Caulfield Grammar School", 50),
            self.connection.execute(
                "SELECT current_school_id,current_school_name_snapshot,"
                "current_school_enrolment_id FROM progression_cases"
            ).fetchone())

    def test_activation_revalidation_fails_if_canonical_current_changes(self):
        bind_case_current_enrolment(
            self.connection, case_id=1, lifecycle_type="transfer"
        )
        self.connection.execute(
            "UPDATE student_school_enrolments SET status='historical' WHERE id=50"
        )
        self.connection.execute(
            "INSERT INTO student_school_enrolments VALUES(51,'S1',10,'New Current','current','active')"
        )
        with self.assertRaises(LifecycleIntegrityError):
            validate_case_current_enrolment(
                self.connection, case_id=1, lifecycle_type="transfer"
            )


if __name__ == "__main__":
    unittest.main()
