from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.test_revenue_migration_032 import MIGRATION as MIGRATION_032
from tests.test_revenue_migration_032 import build_legacy_database, fingerprint
from tests.test_revenue_migration_033 import MIGRATION as MIGRATION_033


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations/20260901_034_revenue_client_payment_receipt.sql"
FORWARD_CHECK = ROOT / "migrations/checks/20260901_034_revenue_client_payment_receipt_forward_check.sql"
ROLLBACK = ROOT / "migrations/rollback/20260901_034_revenue_client_payment_receipt_rollback.sql"

NEW_TABLES = {
    "revenue_fee_item_contract_links",
    "revenue_client_payment_allocations",
    "revenue_receipt_snapshots",
}


class RevenueMigration034Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "revenue-034.sqlite3"
        build_legacy_database(self.database_path)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(MIGRATION_032.read_text(encoding="utf-8"))
            connection.execute(
                """CREATE TABLE student_documents(
                     id INTEGER PRIMARY KEY,
                     student_id TEXT NOT NULL REFERENCES students(student_id),
                     document_type TEXT NOT NULL,
                     file_path TEXT NOT NULL
                   )"""
            )
            connection.executescript(MIGRATION_033.read_text(encoding="utf-8"))
            connection.commit()
            self.before = {
                table: fingerprint(connection, table)
                for table in (
                    "students",
                    "application_cycles",
                    "contracts",
                    "revenue_payments",
                    "revenue_payment_allocations",
                    "revenue_receipts",
                    "revenue_commission_lines",
                )
            }

    def apply(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(MIGRATION.read_text(encoding="utf-8"))
            connection.commit()

    def seed_client_fee(self, connection, *, contract_id=70, cycle_id=100):
        connection.execute(
            "INSERT INTO contracts VALUES(?,?,?,?,?,?)",
            (contract_id, "S-REVENUE", cycle_id, "FullyExecuted", "2026-09-01", "2026-09-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO revenue_commercial_cases VALUES(1,'case-1',100,'S-REVENUE','open',0,'{}',1,'2026-09-01T00:00:00Z',NULL)"
        )
        connection.execute(
            "INSERT INTO revenue_billing_accounts VALUES(1,'account-1','individual','S-REVENUE','AUD','active',1,'2026-09-01T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO revenue_rule_snapshots VALUES(1,'snapshot-1','fee','rule-1','{}',?,1,'2026-09-01T00:00:00Z')",
            ("a" * 64,),
        )
        connection.execute(
            """INSERT INTO revenue_fee_items(
                 id,uid,commercial_case_id,billing_account_id,student_id,rule_snapshot_id,
                 milestone_code,description,net_cents,gst_cents,gross_cents,currency_code,status,created_at
               ) VALUES(1,'fee-1',1,1,'S-REVENUE',1,'contract_execution','Client service fee',
                        240000,0,240000,'AUD','unbilled','2026-09-01T00:00:00Z')"""
        )

    def seed_method_and_payment(self, connection, *, amount=120000):
        connection.execute(
            "INSERT INTO revenue_payment_methods VALUES(1,'method-1','au_personal_account','澳洲个人账户','active')"
        )
        connection.execute(
            "INSERT INTO revenue_payment_method_rule_versions VALUES(1,'method-rule-1',1,'2026-01-01',NULL,0,0,'active','2026-09-01T00:00:00Z')"
        )
        connection.execute(
            """INSERT INTO revenue_payments(
                 id,uid,payment_direction,counterparty_kind,billing_account_id,payment_method_id,
                 external_reference,received_date,amount_cents,allocated_cents,currency_code,
                 status,confirmed_by,confirmed_at
               ) VALUES(1,'payment-1','received','client',1,1,'TX-1','2026-09-01',?,0,'AUD',
                        'confirmed',1,'2026-09-01T00:00:00Z')""",
            (amount,),
        )

    def test_package_uses_034_slot_and_does_not_rebuild_existing_financial_tables(self):
        self.assertTrue(MIGRATION.is_file())
        self.assertTrue(FORWARD_CHECK.is_file())
        self.assertTrue(ROLLBACK.is_file())
        self.apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(self.before, {table: fingerprint(connection, table) for table in self.before})
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?)",
                tuple(sorted(NEW_TABLES)),
            )}
            self.assertEqual(NEW_TABLES, tables)
            self.assertEqual("ok", connection.execute("PRAGMA quick_check").fetchone()[0])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())

    def test_columns_are_structured_integer_cents_and_receipt_is_unique_per_payment(self):
        self.apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            allocation = {row[1]: row[2].upper() for row in connection.execute(
                'PRAGMA table_info("revenue_client_payment_allocations")'
            )}
            for name in (
                "uid", "payment_id", "fee_item_id", "allocated_base_cents", "gst_cents",
                "gross_charge_aud_cents", "payment_method_rule_version_id",
                "method_code_snapshot", "gst_rate_basis_points", "created_by", "created_at",
            ):
                self.assertIn(name, allocation)
            for name in ("allocated_base_cents", "gst_cents", "gross_charge_aud_cents", "gst_rate_basis_points"):
                self.assertEqual("INTEGER", allocation[name])
            snapshot = {row[1] for row in connection.execute(
                'PRAGMA table_info("revenue_receipt_snapshots")'
            )}
            self.assertTrue({
                "receipt_id", "receipt_number", "issuer_name", "issuer_abn",
                "student_name", "student_number",
                "application_cycle_id", "application_cycle_label", "contract_id",
                "contract_reference", "fee_item_id", "fee_type", "payment_date",
                "payment_method_code", "payment_method_label", "actual_currency_code",
                "actual_amount_cents", "allocated_base_cents", "gst_cents",
                "gross_charge_aud_cents", "transaction_reference", "issued_at",
                "snapshot_sha256",
            }.issubset(snapshot))
            indexes = {row[1]: row[2] for row in connection.execute('PRAGMA index_list("revenue_receipts")')}
            self.assertEqual(1, indexes["uq_revenue_receipts_payment"])

    def test_contract_link_requires_same_student_cycle_case_fee_and_is_unique(self):
        self.apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            self.seed_client_fee(connection)
            connection.execute(
                """INSERT INTO revenue_fee_item_contract_links(
                     uid,fee_item_id,contract_id,commercial_case_id,created_by,created_at
                   ) VALUES('link-1',1,70,1,1,'2026-09-01T00:00:00Z')"""
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO revenue_fee_item_contract_links(
                         uid,fee_item_id,contract_id,commercial_case_id,created_by,created_at
                       ) VALUES('link-2',1,70,1,1,'2026-09-01T00:00:00Z')"""
                )
            connection.execute(
                "INSERT INTO students VALUES('S-OTHER')"
            )
            connection.execute(
                "INSERT INTO application_cycles VALUES(101,'cycle-101','S-OTHER','initial','active','school_education','2026-09-01')"
            )
            connection.execute(
                "INSERT INTO contracts VALUES(71,'S-OTHER',101,'FullyExecuted','2026-09-01','2026-09-01T00:00:00Z')"
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "student/cycle mismatch"):
                connection.execute(
                    """INSERT INTO revenue_fee_item_contract_links(
                         uid,fee_item_id,contract_id,commercial_case_id,created_by,created_at
                       ) VALUES('link-bad',1,71,1,1,'2026-09-01T00:00:00Z')"""
                )

    def test_allocation_enforces_one_payment_one_fee_gross_equality_and_overallocation(self):
        self.apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            self.seed_client_fee(connection)
            self.seed_method_and_payment(connection)
            connection.execute(
                "INSERT INTO revenue_fee_item_contract_links VALUES(1,'link-1',1,70,1,1,'2026-09-01T00:00:00Z')"
            )
            connection.execute(
                """INSERT INTO revenue_client_payment_allocations(
                     uid,payment_id,fee_item_id,allocated_base_cents,gst_cents,
                     gross_charge_aud_cents,payment_method_rule_version_id,
                     method_code_snapshot,gst_rate_basis_points,created_by,created_at
                   ) VALUES('allocation-1',1,1,120000,0,120000,1,'au_personal_account',0,1,
                            '2026-09-01T00:00:00Z')"""
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO revenue_client_payment_allocations(
                         uid,payment_id,fee_item_id,allocated_base_cents,gst_cents,
                         gross_charge_aud_cents,payment_method_rule_version_id,
                         method_code_snapshot,gst_rate_basis_points,created_by,created_at
                       ) VALUES('allocation-duplicate',1,1,1,0,1,1,'au_personal_account',0,1,
                                '2026-09-01T00:00:00Z')"""
                )
            connection.execute(
                """INSERT INTO revenue_payments(
                     id,uid,payment_direction,counterparty_kind,billing_account_id,payment_method_id,
                     received_date,amount_cents,allocated_cents,currency_code,status,confirmed_by,confirmed_at
                   ) VALUES(2,'payment-2','received','client',1,1,'2026-09-01',130000,0,'AUD',
                            'confirmed',1,'2026-09-01T00:00:00Z')"""
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "exceeds fee item final base"):
                connection.execute(
                    """INSERT INTO revenue_client_payment_allocations(
                         uid,payment_id,fee_item_id,allocated_base_cents,gst_cents,
                         gross_charge_aud_cents,payment_method_rule_version_id,
                         method_code_snapshot,gst_rate_basis_points,created_by,created_at
                       ) VALUES('allocation-over',2,1,130000,0,130000,1,'au_personal_account',0,1,
                                '2026-09-01T00:00:00Z')"""
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO revenue_client_payment_allocations(
                         uid,payment_id,fee_item_id,allocated_base_cents,gst_cents,
                         gross_charge_aud_cents,payment_method_rule_version_id,
                         method_code_snapshot,gst_rate_basis_points,created_by,created_at
                       ) VALUES('allocation-bad-gross',2,1,120000,0,120001,1,'au_personal_account',0,1,
                                '2026-09-01T00:00:00Z')"""
                )

    def test_new_financial_authorities_are_append_only(self):
        self.apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            self.seed_client_fee(connection)
            self.seed_method_and_payment(connection)
            connection.execute(
                "INSERT INTO revenue_fee_item_contract_links VALUES(1,'link-1',1,70,1,1,'2026-09-01T00:00:00Z')"
            )
            connection.execute(
                """INSERT INTO revenue_client_payment_allocations VALUES(
                     1,'allocation-1',1,1,120000,0,120000,1,'au_personal_account',0,1,
                     '2026-09-01T00:00:00Z')"""
            )
            for table in ("revenue_fee_item_contract_links", "revenue_client_payment_allocations"):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    connection.execute(f'UPDATE "{table}" SET created_at="changed"')
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    connection.execute(f'DELETE FROM "{table}"')

    def test_rollback_is_allowed_when_empty_and_refuses_financial_data_loss(self):
        self.apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(ROLLBACK.read_text(encoding="utf-8"))
            names = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index','trigger')"
            )}
            self.assertTrue(NEW_TABLES.isdisjoint(names))
            self.assertNotIn("uq_revenue_receipts_payment", names)
            self.assertIn("revenue_payments", names)
            self.assertIn("revenue_payment_allocations", names)
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())

        self.apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            self.seed_client_fee(connection)
            connection.execute(
                "INSERT INTO revenue_fee_item_contract_links VALUES(1,'link-1',1,70,1,1,'2026-09-01T00:00:00Z')"
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "034 rollback blocked"):
                connection.executescript(ROLLBACK.read_text(encoding="utf-8"))
            self.assertEqual(1, connection.execute(
                "SELECT COUNT(*) FROM revenue_fee_item_contract_links"
            ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
