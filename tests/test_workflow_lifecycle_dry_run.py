import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WorkflowLifecycleDryRunTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "dry-run.db"
        connection = sqlite3.connect(self.path)
        connection.executescript(
            """
            CREATE TABLE students(student_id TEXT PRIMARY KEY,deleted_at TEXT);
            CREATE TABLE parent_form_tokens(token TEXT PRIMARY KEY,student_id TEXT,submitted_at TEXT);
            CREATE TABLE application_cycles(id INTEGER PRIMARY KEY,student_id TEXT,status TEXT);
            CREATE TABLE contracts(id INTEGER PRIMARY KEY,student_id TEXT,application_cycle_id INTEGER,contract_status TEXT,deleted_at TEXT);
            CREATE TABLE school_plan_reports(id INTEGER PRIMARY KEY,student_id TEXT,application_cycle_id INTEGER,cycle_is_current INTEGER,status TEXT,invalidated_at TEXT);
            CREATE TABLE school_plan_report_confirmations(id INTEGER PRIMARY KEY,report_id INTEGER,student_id TEXT,confirmed_at TEXT,status TEXT);
            CREATE TABLE school_plan_confirmed_schools(id INTEGER PRIMARY KEY,confirmation_id INTEGER,report_id INTEGER,student_id TEXT,plan_sector TEXT,submission_status TEXT);
            CREATE TABLE school_plan_school_submission_events(id INTEGER PRIMARY KEY,confirmed_school_id INTEGER,student_id TEXT,report_id INTEGER,new_submission_status TEXT);
            CREATE TABLE student_cases(id INTEGER PRIMARY KEY,student_id TEXT,case_type TEXT,active_slot INTEGER);
            INSERT INTO students VALUES('STUDENT-A',NULL),('STUDENT-B',NULL),('OTHER',NULL),('DELETED','2026-01-01');
            INSERT INTO parent_form_tokens VALUES
              ('a','STUDENT-A','2026-08-01'),('b','STUDENT-B','2026-08-02'),
              ('c','OTHER',NULL),('d','DELETED','2026-08-03');
            INSERT INTO contracts VALUES(126,'STUDENT-A',NULL,'FullyExecuted',NULL);
            INSERT INTO school_plan_reports VALUES(140,'STUDENT-A',NULL,0,'active',NULL);
            INSERT INTO school_plan_report_confirmations VALUES(43,140,'STUDENT-A','2026-08-12','confirmed');
            INSERT INTO school_plan_confirmed_schools VALUES
              (65,43,140,'STUDENT-A','public','submitted'),
              (66,43,140,'STUDENT-A','public','submitted');
            INSERT INTO school_plan_school_submission_events VALUES
              (59,65,'STUDENT-A',140,'submitted'),
              (60,66,'STUDENT-A',140,'submitted');
            INSERT INTO student_cases VALUES(9,'STUDENT-B','transfer',1);
            """
        )
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cli_is_repeatable_read_only_and_reports_exact_candidates(self):
        command = [
            sys.executable,
            str(ROOT / "tools" / "workflow_lifecycle_dry_run.py"),
            "--database",
            str(self.path),
        ]
        before = self.path.read_bytes()
        first = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
        second = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)

        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(before, self.path.read_bytes())
        payload = json.loads(first.stdout)
        self.assertEqual(2, payload["exact_candidate_count"])
        self.assertEqual(["STUDENT-A", "STUDENT-B"], [item["student_id"] for item in payload["exact_candidates"]])
        self.assertEqual([126], payload["exact_candidates"][0]["contract_ids"])
        self.assertEqual([140], payload["exact_candidates"][0]["school_plan_report_ids"])
        self.assertEqual([43], payload["exact_candidates"][0]["school_plan_confirmation_ids"])
        self.assertEqual([65, 66], payload["exact_candidates"][0]["submitted_confirmed_school_ids"])
        self.assertEqual([59, 60], payload["exact_candidates"][0]["school_submission_event_ids"])
        self.assertEqual([9], payload["exact_candidates"][1]["active_student_case_ids"])
        self.assertEqual("read_only_immutable", payload["mode"])


if __name__ == "__main__":
    unittest.main()
