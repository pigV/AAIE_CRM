from contextlib import closing
import sqlite3
import tempfile
import unittest

from student_lifecycle.transfer_contract_gate import (
    evaluate_transfer_contract_gate,
)


class TransferContractGateTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(self.sandbox.cleanup)
        self.database_path = f"{self.sandbox.name}/gate.sqlite3"
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE contracts(
                  id INTEGER PRIMARY KEY,student_id TEXT,contract_type TEXT,
                  contract_status TEXT,deleted_at TEXT,voided_at TEXT,
                  application_cycle_id INTEGER,final_signed_file_path TEXT
                );
                INSERT INTO contracts VALUES
                  (1,'S1','TransferApplicationContract','FullyExecuted',NULL,NULL,NULL,'/final/1.pdf'),
                  (2,'S2','TransferApplicationContract','FullyExecuted',NULL,NULL,NULL,'/final/2.pdf'),
                  (3,'S1','NewInternationalStudentContract','FullyExecuted',NULL,NULL,NULL,'/final/3.pdf'),
                  (4,'S1','LocalStudentApplicationContract','FullyExecuted',NULL,NULL,NULL,'/final/4.pdf'),
                  (5,'S1','TransferApplicationContract','Draft',NULL,NULL,NULL,NULL),
                  (6,'S1','TransferApplicationContract','ParentSigned',NULL,NULL,NULL,NULL),
                  (7,'S1','TransferApplicationContract','FullyExecuted','x',NULL,NULL,'/final/7.pdf'),
                  (8,'S1','TransferApplicationContract','FullyExecuted',NULL,'x',NULL,'/final/8.pdf'),
                  (9,'S1','TransferApplicationContract','FullyExecuted',NULL,NULL,999,'/final/9.pdf'),
                  (10,'S1','TransferApplicationContract','FullyExecuted',NULL,NULL,NULL,NULL);
                """
            )

    def evaluate(self, contract_id):
        with closing(sqlite3.connect(self.database_path)) as connection:
            return evaluate_transfer_contract_gate(
                connection, student_id="S1", source_contract_id=contract_id,
                expected_cycle_id=None,
            )

    def test_exact_fully_executed_transfer_contract_allows(self):
        decision = self.evaluate(1)
        self.assertTrue(decision.allowed)
        self.assertEqual("complete", decision.status)
        self.assertEqual("/final/1.pdf", decision.final_pdf_path)

    def test_missing_draft_and_parent_signed_are_blocked_with_stable_status(self):
        self.assertEqual("waiting_contract_creation", self.evaluate(None).status)
        self.assertEqual("waiting_parent_signature", self.evaluate(5).status)
        self.assertEqual("waiting_aaie_execution", self.evaluate(6).status)
        self.assertEqual("waiting_final_pdf", self.evaluate(10).status)

    def test_wrong_student_type_deleted_voided_and_prebound_cycle_fail_closed(self):
        for contract_id in (2, 3, 4, 7, 8, 9, 999):
            with self.subTest(contract_id=contract_id):
                decision = self.evaluate(contract_id)
                self.assertFalse(decision.allowed)
                self.assertEqual("invalid_ownership", decision.status)


if __name__ == "__main__":
    unittest.main()
