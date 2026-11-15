import sqlite3
import unittest

from visa_cycle_assignment import resolve_visa_cycle


class VisaCycleAssignmentTests(unittest.TestCase):
    def test_selects_only_an_owned_active_cycle(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript("""
            CREATE TABLE application_cycles(
                id INTEGER, student_id TEXT, status TEXT, sequence_no INTEGER,
                cycle_type TEXT, education_stream TEXT);
            INSERT INTO application_cycles VALUES
                (1,'S1','active',1,'initial','school_education'),
                (2,'S1','closed',2,'initial','higher_education'),
                (3,'S2','active',1,'initial','school_education');
        """)
        self.assertEqual(resolve_visa_cycle(connection, "S1"), 1)
        self.assertIsNone(resolve_visa_cycle(connection, "S3"))
        for invalid in ("2", "3", "invalid"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                resolve_visa_cycle(connection, "S1", invalid)
        with self.assertRaises(ValueError):
            resolve_visa_cycle(connection, "S1", infer_single=False)
        connection.execute("UPDATE application_cycles SET status='active' WHERE id=2")
        with self.assertRaises(ValueError):
            resolve_visa_cycle(connection, "S1")
        self.assertEqual(resolve_visa_cycle(connection, "S1", "2"), 2)
