from contextlib import closing
import sqlite3
import unittest

from revenue.facts import RevenueFactAdapter


SCHEMA = """
CREATE TABLE students(student_id TEXT PRIMARY KEY);
CREATE TABLE application_cycles(
  id INTEGER PRIMARY KEY,
  cycle_uid TEXT UNIQUE NOT NULL,
  student_id TEXT NOT NULL,
  cycle_type TEXT NOT NULL,
  status TEXT NOT NULL,
  outcome TEXT,
  education_stream TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE contracts(
  id INTEGER PRIMARY KEY,
  application_cycle_id INTEGER,
  student_id TEXT NOT NULL,
  contract_status TEXT NOT NULL,
  contract_date TEXT,
  fully_executed_at TEXT
);
CREATE TABLE public_application_groups(
  id INTEGER PRIMARY KEY,
  application_cycle_id INTEGER,
  status TEXT,
  submitted_at TEXT
);
CREATE TABLE private_school_applications(
  id INTEGER PRIMARY KEY,
  application_cycle_id INTEGER,
  status TEXT,
  submitted_at TEXT
);
CREATE TABLE transfer_school_applications(
  id INTEGER PRIMARY KEY,
  application_cycle_id INTEGER,
  application_status TEXT
);
"""


class RevenueFactsTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.execute("INSERT INTO students VALUES('S1')")
        self.connection.execute(
            "INSERT INTO application_cycles VALUES(20,'current','S1','transfer','active',NULL,'school_education','2026-08-29')"
        )
        self.adapter = RevenueFactAdapter(self.connection)

    def tearDown(self):
        self.connection.close()

    def prior_cycle(self, *, cycle_id=10, stream="school_education", outcome="withdrawn"):
        self.connection.execute(
            "INSERT INTO application_cycles VALUES(?,?,'S1','initial','closed',?,?,?)",
            (cycle_id, f"prior-{cycle_id}", outcome, stream, "2025-02-01"),
        )

    def contract(self, cycle_id=10):
        self.connection.execute(
            "INSERT INTO contracts VALUES(100,?,'S1','FullyExecuted','2025-02-02','2025-02-03T00:00:00Z')",
            (cycle_id,),
        )

    def test_prior_with_fully_executed_contract_and_submission_is_existing(self):
        self.prior_cycle(outcome="cancelled")
        self.contract()
        self.connection.execute(
            "INSERT INTO private_school_applications VALUES(200,10,'withdrawn','2025-02-10')"
        )
        decision = self.adapter.resolve_existing_aaie_k12(20)
        self.assertTrue(decision.is_existing)
        self.assertEqual(10, decision.prior_cycle_id)
        self.assertEqual(100, decision.contract_id)
        self.assertEqual("private_school_application", decision.submission_kind)
        self.assertEqual(200, decision.submission_id)

    def test_fully_executed_contract_without_submission_is_not_existing(self):
        self.prior_cycle()
        self.contract()
        decision = self.adapter.resolve_existing_aaie_k12(20)
        self.assertFalse(decision.is_existing)
        self.assertEqual("no_qualifying_prior_cycle", decision.reason)

    def test_submission_without_fully_executed_contract_is_not_existing(self):
        self.prior_cycle()
        self.connection.execute(
            "INSERT INTO public_application_groups VALUES(201,10,'closed','2025-02-10')"
        )
        self.assertFalse(self.adapter.resolve_existing_aaie_k12(20).is_existing)

    def test_prior_higher_education_cycle_does_not_qualify(self):
        self.prior_cycle(stream="higher_education")
        self.contract()
        self.connection.execute(
            "INSERT INTO private_school_applications VALUES(202,10,'submitted','2025-02-10')"
        )
        self.assertFalse(self.adapter.resolve_existing_aaie_k12(20).is_existing)

    def test_cancelled_transfer_without_submission_evidence_does_not_qualify(self):
        self.prior_cycle()
        self.contract()
        self.connection.execute(
            "INSERT INTO transfer_school_applications VALUES(203,10,'cancelled')"
        )
        self.assertFalse(self.adapter.resolve_existing_aaie_k12(20).is_existing)

    def test_post_submission_transfer_state_is_durable_evidence(self):
        self.prior_cycle()
        self.contract()
        self.connection.execute(
            "INSERT INTO transfer_school_applications VALUES(204,10,'waiting_offer')"
        )
        decision = self.adapter.resolve_existing_aaie_k12(20)
        self.assertTrue(decision.is_existing)
        self.assertEqual("transfer_school_application", decision.submission_kind)

    def test_current_or_later_cycle_never_qualifies_as_prior(self):
        self.prior_cycle(cycle_id=30)
        self.connection.execute(
            "UPDATE application_cycles SET created_at='2027-01-01' WHERE id=30"
        )
        self.connection.execute(
            "INSERT INTO contracts VALUES(101,30,'S1','FullyExecuted','2027-01-02','2027-01-02')"
        )
        self.connection.execute(
            "INSERT INTO private_school_applications VALUES(205,30,'submitted','2027-01-03')"
        )
        self.assertFalse(self.adapter.resolve_existing_aaie_k12(20).is_existing)


if __name__ == "__main__":
    unittest.main()
