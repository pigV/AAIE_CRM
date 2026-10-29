from contextlib import closing
import sqlite3
import unittest

from student_lifecycle.progression_contract_gate import (
    HIGHER_EDUCATION,
    SCHOOL_PROGRESSION,
    evaluate_progression_contract_gate,
    resolve_progression_entry_gate,
)


class ProgressionContractGateTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE students(student_id TEXT PRIMARY KEY);
            CREATE TABLE contracts(
              id INTEGER PRIMARY KEY,
              student_id TEXT,
              contract_type TEXT NOT NULL,
              contract_status TEXT NOT NULL,
              deleted_at TEXT,
              voided_at TEXT
            );
            CREATE TABLE application_cycles(
              id INTEGER PRIMARY KEY,
              student_id TEXT NOT NULL,
              education_stream TEXT NOT NULL,
              status TEXT NOT NULL
            );
            CREATE TABLE progression_cases(
              case_id INTEGER PRIMARY KEY,
              source_contract_id INTEGER
            );
            INSERT INTO students VALUES('S1'),('S2');
            INSERT INTO contracts VALUES
              (1,'S1','LocalStudentApplicationContract','Draft',NULL,NULL),
              (2,'S1','LocalStudentApplicationContract','Sent',NULL,NULL),
              (3,'S1','LocalStudentApplicationContract','ParentSigned',NULL,NULL),
              (4,'S1','LocalStudentApplicationContract','FullyExecuted',NULL,NULL),
              (5,'S1','NewInternationalStudentContract','FullyExecuted',NULL,NULL),
              (6,'S1','TransferApplicationContract','FullyExecuted',NULL,NULL),
              (7,'S2','LocalStudentApplicationContract','FullyExecuted',NULL,NULL),
              (8,'S1','LocalStudentApplicationContract','FullyExecuted','2026-08-18',NULL),
              (9,'S1','LocalStudentApplicationContract','FullyExecuted',NULL,'2026-08-18');
            """
        )

    def decision(self, business_type, contract_id=None, student_id="S1"):
        return evaluate_progression_contract_gate(
            self.connection,
            student_id=student_id,
            business_type=business_type,
            source_contract_id=contract_id,
        )

    def test_higher_education_requires_no_contract(self):
        decision = self.decision(HIGHER_EDUCATION)
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.contract_required)
        self.assertEqual("not_required", decision.status)
        self.assertEqual("无需合同", decision.label)

    def test_higher_education_rejects_contract_ownership(self):
        decision = self.decision(HIGHER_EDUCATION, 4)
        self.assertFalse(decision.allowed)
        self.assertEqual("invalid_ownership", decision.status)

    def test_unclassified_fails_closed(self):
        decision = self.decision(None)
        self.assertFalse(decision.allowed)
        self.assertEqual("unclassified", decision.status)
        self.assertIn("明确升学业务类型", decision.label)

    def test_school_without_contract_waits_for_creation(self):
        decision = self.decision(SCHOOL_PROGRESSION)
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.contract_required)
        self.assertEqual("waiting_contract_creation", decision.status)
        self.assertEqual("等待建立合同", decision.label)

    def test_draft_and_sent_wait_for_parent_signature(self):
        for contract_id in (1, 2):
            with self.subTest(contract_id=contract_id):
                decision = self.decision(SCHOOL_PROGRESSION, contract_id)
                self.assertFalse(decision.allowed)
                self.assertEqual("waiting_parent_signature", decision.status)
                self.assertEqual("等待家长签署", decision.label)

    def test_parent_signed_waits_for_aaie_execution(self):
        decision = self.decision(SCHOOL_PROGRESSION, 3)
        self.assertFalse(decision.allowed)
        self.assertEqual("waiting_aaie_execution", decision.status)
        self.assertEqual("等待AAIE签章", decision.label)

    def test_fully_executed_local_contract_allows_school_progression(self):
        decision = self.decision(SCHOOL_PROGRESSION, 4)
        self.assertTrue(decision.allowed)
        self.assertEqual("complete", decision.status)
        self.assertEqual("合同已完成", decision.label)
        self.assertEqual(4, decision.source_contract_id)

    def test_initial_transfer_and_other_student_contracts_fail_closed(self):
        for contract_id in (5, 6, 7):
            with self.subTest(contract_id=contract_id):
                decision = self.decision(SCHOOL_PROGRESSION, contract_id)
                self.assertFalse(decision.allowed)
                self.assertEqual("invalid_ownership", decision.status)

    def test_missing_deleted_and_voided_contracts_fail_closed(self):
        for contract_id in (999, 8, 9):
            with self.subTest(contract_id=contract_id):
                decision = self.decision(SCHOOL_PROGRESSION, contract_id)
                self.assertFalse(decision.allowed)
                self.assertEqual("invalid_ownership", decision.status)

    def test_mutated_owned_contract_is_revalidated_not_cached(self):
        self.assertTrue(self.decision(SCHOOL_PROGRESSION, 4).allowed)
        self.connection.execute(
            "UPDATE contracts SET contract_status='ParentSigned' WHERE id=4"
        )
        decision = self.decision(SCHOOL_PROGRESSION, 4)
        self.assertFalse(decision.allowed)
        self.assertEqual("waiting_aaie_execution", decision.status)

    def test_decision_does_not_search_for_an_arbitrary_student_contract(self):
        self.assertTrue(self.decision(SCHOOL_PROGRESSION, 4).allowed)
        decision = self.decision(SCHOOL_PROGRESSION, None)
        self.assertFalse(decision.allowed)
        self.assertEqual("waiting_contract_creation", decision.status)
        self.assertIsNone(decision.source_contract_id)

    def test_entry_gate_does_not_infer_business_type_from_education_stream(self):
        self.connection.executescript(
            """
            INSERT INTO application_cycles VALUES
              (1,'S1','school_education','closed'),
              (2,'S1','higher_education','active');
            """
        )
        decision = resolve_progression_entry_gate(self.connection, student_id="S1")
        self.assertIsNone(decision.business_type)
        self.assertFalse(decision.allowed)
        self.assertEqual("unclassified", decision.status)

    def test_entry_gate_does_not_search_student_contracts(self):
        self.connection.execute(
            "INSERT INTO application_cycles VALUES(1,'S1','school_education','closed')"
        )
        decision = resolve_progression_entry_gate(self.connection, student_id="S1")
        self.assertFalse(decision.allowed)
        self.assertEqual("unclassified", decision.status)
        self.assertIsNone(decision.source_contract_id)

    def test_entry_gate_without_canonical_stream_fails_closed(self):
        decision = resolve_progression_entry_gate(self.connection, student_id="S1")
        self.assertFalse(decision.allowed)
        self.assertEqual("unclassified", decision.status)


if __name__ == "__main__":
    unittest.main()
