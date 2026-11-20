import sqlite3
import unittest
from visa_business_state import unassigned_student_case_ids


class UnassignedVisaCaseTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.addCleanup(self.c.close)
        self.c.executescript('''CREATE TABLE students(student_id TEXT PRIMARY KEY,deleted_at TEXT);
            INSERT INTO students VALUES('S1',NULL),('S2',NULL),('D1','2026-11-20');
            CREATE TABLE visa_cases(id INTEGER PRIMARY KEY,student_id TEXT,application_cycle_id INTEGER,archived_at TEXT);
            INSERT INTO visa_cases VALUES(1,'S1',NULL,NULL),(2,'S1',7,NULL),(3,'S1',NULL,'archived'),
              (4,'S2',NULL,NULL),(5,'D1',NULL,NULL),(6,NULL,NULL,NULL);''')
        self.c.execute('PRAGMA query_only=ON')

    def test_exact_student_only_excludes_bound_archived_deleted_and_independent_cases(self):
        before=self.c.total_changes
        self.assertEqual((1,),unassigned_student_case_ids(self.c,'S1'))
        self.assertEqual((4,),unassigned_student_case_ids(self.c,'S2'))
        self.assertEqual((),unassigned_student_case_ids(self.c,'D1'))
        self.assertEqual((),unassigned_student_case_ids(self.c,''))
        self.assertEqual(before,self.c.total_changes)
