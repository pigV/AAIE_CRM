from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.transfer_existing_full_e2e_seed import (
    IsolationViolation,
    assert_precondition_only,
    validate_isolated_runtime,
)
from tests.transfer_existing_full_e2e_evidence import validate_two_source_authority_bindings


class ExistingTransferSeedBoundaryTests(unittest.TestCase):
    def test_two_authorities_bind_distinct_contracts_to_exact_two_cycles(self):
        cycles = [
            {"id": 10, "source_contract_id": 100},
            {"id": 11, "source_contract_id": 101},
        ]
        authorities = [
            {"id": 1, "application_cycle_id": 10, "transfer_contract_id": 100,
             "lifecycle_state": "consumed", "current_enrolment_id": 7,
             "ordinary_relationship_cycle_id": 8, "ordinary_relationship_contract_id": 9,
             "private_school_application_id": None, "public_application_group_id": None},
            {"id": 2, "application_cycle_id": 11, "transfer_contract_id": 101,
             "lifecycle_state": "consumed", "current_enrolment_id": 12,
             "ordinary_relationship_cycle_id": 8, "ordinary_relationship_contract_id": 9,
             "private_school_application_id": None, "public_application_group_id": None},
        ]
        result = validate_two_source_authority_bindings(authorities, cycles)
        self.assertEqual(set(result), {10, 11})
        authorities[1]["transfer_contract_id"] = 100
        with self.assertRaises(AssertionError):
            validate_two_source_authority_bindings(authorities, cycles)

    def test_rejects_live_ports_and_protected_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            database = root / "fixture.sqlite3"
            storage = root / "storage"
            storage.mkdir()
            database.touch()
            for ports in ((8880, 18981), (18980, 8881)):
                with self.assertRaises(IsolationViolation):
                    validate_isolated_runtime(root, database, storage, ports)
        with self.assertRaises(IsolationViolation):
            validate_isolated_runtime(
                Path("/Volumes/Zhang/AAIE CodeX/CRM"),
                Path("/Volumes/Zhang/AAIE CodeX/CRM/database/aaie_crm.db"),
                Path("/Volumes/Server/Students"),
                (18980, 18981),
            )

    def test_rejects_symlink_escape_and_database_outside_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            root = parent / "runtime"
            root.mkdir()
            storage = root / "storage"
            storage.symlink_to(parent, target_is_directory=True)
            database = parent / "outside.sqlite3"
            database.touch()
            with self.assertRaises(IsolationViolation):
                validate_isolated_runtime(root, database, storage, (18980, 18981))

    def test_precondition_assertion_rejects_new_transfer_facts(self):
        with sqlite3.connect(":memory:") as connection:
            connection.executescript(
                """
                CREATE TABLE transfer_source_authorities(id INTEGER,student_id TEXT);
                CREATE TABLE student_cases(id INTEGER,student_id TEXT,case_type TEXT);
                CREATE TABLE application_cycles(id INTEGER,student_id TEXT,cycle_type TEXT);
                CREATE TABLE transfer_offer_bindings(id INTEGER,student_id TEXT);
                CREATE TABLE transfer_payment_confirmations(id INTEGER,student_id TEXT);
                CREATE TABLE transfer_coe_confirmations(id INTEGER,student_id TEXT);
                CREATE TABLE school_enrolment_transition_intents(id INTEGER,student_id TEXT);
                """
            )
            assert_precondition_only(connection, ("99197001", "99197002"))
            connection.execute(
                "INSERT INTO application_cycles VALUES(1,'99197001','transfer')"
            )
            with self.assertRaises(AssertionError):
                assert_precondition_only(connection, ("99197001", "99197002"))

    def test_boundary_source_does_not_seed_new_transfer_business_facts(self):
        source = Path(__file__).with_name("transfer_existing_full_e2e_seed.py").read_text(
            encoding="utf-8"
        )
        forbidden = (
            "INSERT INTO transfer_source_authorities",
            "INSERT INTO transfer_offer_bindings",
            "INSERT INTO transfer_payment_confirmations",
            "INSERT INTO transfer_coe_confirmations",
            "INSERT INTO school_enrolment_transition_intents",
        )
        for statement in forbidden:
            self.assertNotIn(statement, source)

    def test_fixture_uses_formal_admin_fee_authority_service(self):
        source = Path(__file__).with_name("transfer_existing_full_e2e_seed.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("FeeRuleAuthorityService", source)
        self.assertIn("initialize_2026", source)
        self.assertNotIn("INSERT INTO revenue_fee_rule_versions", source)
        self.assertNotIn("UPDATE revenue_fee_rule_versions", source)

    def test_fixture_grants_payment_proof_actions_without_revenue_access(self):
        source = Path(__file__).with_name("transfer_existing_full_e2e_seed.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"can_upload": 1 if module == "payments" else 0', source)
        self.assertIn(
            '"can_confirm_upload_review": 1 if module == "payments" else 0', source
        )
        self.assertIn('"revenue_commission"', source)


if __name__ == "__main__":
    unittest.main()
