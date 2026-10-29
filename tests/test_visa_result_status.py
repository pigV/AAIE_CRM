import sqlite3
import unittest
from pathlib import Path

import visa_result_status as service


ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-09-14T10:30:00+10:00"


class VisaResultStatusTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(
            """
            CREATE TABLE users(id INTEGER PRIMARY KEY);
            INSERT INTO users VALUES(1);
            CREATE TABLE visa_cases(
              id INTEGER PRIMARY KEY,
              status TEXT DEFAULT 'Draft',
              decision_status TEXT DEFAULT 'Pending',
              result_status TEXT DEFAULT 'Pending',
              updated_at TEXT,
              archived_at TEXT
            );
            INSERT INTO visa_cases(id) VALUES(1),(2);
            CREATE TABLE visa_case_documents(
              id INTEGER PRIMARY KEY,
              visa_case_id INTEGER NOT NULL REFERENCES visa_cases(id),
              archived_at TEXT,
              student_document_id INTEGER
            );
            INSERT INTO visa_case_documents VALUES(10,1,NULL,NULL),(11,1,NULL,NULL),(20,2,NULL,NULL),(21,2,NULL,NULL);
            CREATE TABLE visa_document_audit(id INTEGER PRIMARY KEY,visa_case_id INTEGER,document_id INTEGER,action TEXT,after_json TEXT);
            INSERT INTO visa_document_audit VALUES
              (1,1,10,'document_category_selected','{"category":"grant_letter","confirmed":true}'),
              (2,1,11,'document_category_selected','{"category":"grant_letter","confirmed":true}'),
              (3,2,20,'document_category_selected','{"category":"grant_letter","confirmed":true}'),
              (4,2,21,'document_category_selected','{"category":"grant_letter","confirmed":true}');
            """
        )
        self.conn.executescript(
            (ROOT / "migrations/20260914_039_visa_result_status_authority.sql").read_text()
        )
        self.addCleanup(self.conn.close)

    def auto(self, document_id, result, **evidence):
        body = {
            "deterministic": True,
            "document_role": "visa_result",
            "document_kind": "grant" if result == "Granted" else "refusal",
            **evidence,
        }
        return service.record_auto_result(
            self.conn,
            1,
            document_id,
            result,
            body,
            actor_user_id=1,
            occurred_at=STAMP,
        )

    def manual(self, result, reason="Staff verified the official outcome"):
        return service.record_manual_result(
            self.conn,
            1,
            result,
            actor_user_id=1,
            occurred_at=STAMP,
            reason=reason,
        )

    def test_first_deterministic_result_updates_current_and_case_projection(self):
        result = self.auto(10, "Granted")
        self.assertTrue(result["created"])
        self.assertEqual("Granted", result["effective_result"])
        self.assertEqual("auto", result["authority"])
        self.assertEqual(
            ("Granted", "Granted", "Granted"),
            tuple(
                self.conn.execute(
                    "SELECT status,decision_status,result_status FROM visa_cases WHERE id=1"
                ).fetchone()
            ),
        )

    def test_matching_evidence_is_idempotent(self):
        first = self.auto(10, "Granted")
        second = self.auto(10, "Granted")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(1, self.conn.execute("SELECT COUNT(*) FROM visa_result_status_events").fetchone()[0])

    def test_refusal_guess_cannot_change_confirmed_grant_result(self):
        self.auto(10, "Granted")
        result = self.auto(11, "Refused")
        self.assertFalse(result["created"])
        self.assertFalse(result["conflict"])
        self.assertEqual("Granted", result["effective_result"])
        self.assertEqual("Granted", result["auto_detected_result"])
        self.assertEqual("Granted", self.conn.execute("SELECT result_status FROM visa_cases WHERE id=1").fetchone()[0])
        self.assertEqual(1, self.conn.execute("SELECT COUNT(*) FROM visa_result_status_events").fetchone()[0])

    def test_manual_override_wins_across_later_automatic_evidence(self):
        self.auto(10, "Granted")
        manual = self.manual("Refused")
        self.assertTrue(manual["manual_override"])
        self.assertEqual("Refused", manual["effective_result"])
        later = self.auto(11, "Granted")
        self.assertTrue(later["created"])
        self.assertEqual("manual", later["authority"])
        self.assertEqual("Refused", later["effective_result"])
        self.assertEqual(3, self.conn.execute("SELECT COUNT(*) FROM visa_result_status_events").fetchone()[0])

    def test_staff_can_correct_either_automatic_outcome(self):
        for case_id, document_id, automatic, correction in ((1,10,'Granted','Refused'),(2,20,'Refused','Granted')):
            kind='grant' if automatic=='Granted' else 'refusal'
            service.record_auto_result(self.conn,case_id,document_id,automatic,{'deterministic':True,'document_role':'visa_result','document_kind':kind},actor_user_id=1,occurred_at=STAMP)
            result=service.record_manual_result(self.conn,case_id,correction,actor_user_id=1,occurred_at=STAMP,reason='Staff verified official result')
            self.assertEqual(correction,result['effective_result'])
            self.assertTrue(result['manual_override'])

    def test_manual_correction_appends_history_and_requires_reason(self):
        self.manual("Granted")
        current = self.manual("Withdrawn", reason="Applicant withdrew the application")
        self.assertEqual("Withdrawn", current["effective_result"])
        self.assertEqual(2, len(service.result_summary(self.conn, 1)["history"]))
        with self.assertRaises(ValueError):
            self.manual("Refused", reason="")

    def test_unknown_ambiguous_and_untrusted_auto_evidence_fail_closed(self):
        for result, evidence in (
            ("Pending", {"deterministic": True, "document_role": "visa_result", "document_kind": "grant"}),
            ("Granted", {"deterministic": False, "document_role": "visa_result", "document_kind": "grant"}),
            ("Granted", {"deterministic": True, "document_role": "other", "document_kind": "grant"}),
            ("Granted", {"deterministic": True, "document_role": "visa_result", "document_kind": "unknown"}),
        ):
            with self.subTest(result=result, evidence=evidence):
                with self.assertRaises(ValueError):
                    service.record_auto_result(
                        self.conn, 1, 10, result, evidence,
                        actor_user_id=1, occurred_at=STAMP,
                    )
        self.assertEqual(0, self.conn.execute("SELECT COUNT(*) FROM visa_result_status_events").fetchone()[0])
        self.assertEqual("Pending", self.conn.execute("SELECT result_status FROM visa_cases WHERE id=1").fetchone()[0])

    def test_cross_case_archived_and_missing_actor_are_rejected_before_write(self):
        for document_id in (20, 99):
            with self.assertRaises(ValueError):
                self.auto(document_id, "Granted")
        self.conn.execute("UPDATE visa_case_documents SET archived_at='now' WHERE id=10")
        with self.assertRaises(ValueError):
            self.auto(10, "Granted")
        with self.assertRaises(ValueError):
            service.record_manual_result(
                self.conn, 1, "Granted", actor_user_id=999,
                occurred_at=STAMP, reason="verified",
            )
        self.assertEqual(0, self.conn.execute("SELECT COUNT(*) FROM visa_result_status_events").fetchone()[0])

    def test_first_auto_evidence_does_not_overwrite_a_retained_legacy_result(self):
        self.conn.execute("UPDATE visa_cases SET status='Refused',decision_status='Refused',result_status='Refused' WHERE id=2")
        result = service.record_auto_result(
            self.conn, 2, 20, "Granted",
            {"deterministic": True, "document_role": "visa_result", "document_kind": "grant"},
            actor_user_id=1, occurred_at=STAMP,
        )
        self.assertEqual("Refused", result["effective_result"])
        self.assertEqual("legacy", result["authority"])
        self.assertTrue(result["conflict"])
        self.assertEqual("Refused", self.conn.execute("SELECT result_status FROM visa_cases WHERE id=2").fetchone()[0])

        later = service.record_auto_result(
            self.conn, 2, 21, "Granted",
            {"deterministic": True, "document_role": "visa_result", "document_kind": "grant"},
            actor_user_id=1, occurred_at=STAMP,
        )
        self.assertEqual("Refused", later["effective_result"])
        self.assertEqual("legacy", later["authority"])
        self.assertTrue(later["conflict"])
        self.assertEqual("Refused", self.conn.execute("SELECT result_status FROM visa_cases WHERE id=2").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
