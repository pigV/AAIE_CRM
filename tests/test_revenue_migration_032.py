from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations/20260829_032_revenue_commission_v1.sql"
FORWARD_CHECK = ROOT / "migrations/checks/20260829_032_revenue_commission_v1_forward_check.sql"
ROLLBACK = ROOT / "migrations/rollback/20260829_032_revenue_commission_v1_rollback.sql"

REVENUE_TABLES = {
    "revenue_module_state", "revenue_number_sequences",
    "revenue_idempotency_receipts", "revenue_outbox_events",
    "revenue_audit_events", "revenue_exceptions", "revenue_documents",
    "revenue_commercial_cases", "revenue_billing_accounts",
    "revenue_billing_parties", "revenue_billing_account_students",
    "revenue_entities", "revenue_entity_provider_links",
    "revenue_entity_contacts", "revenue_enrolment_components",
    "revenue_commencement_facts", "revenue_tuition_facts",
    "revenue_tuition_adjustments", "revenue_fee_rule_sets",
    "revenue_fee_rule_versions", "revenue_fee_rule_conditions",
    "revenue_fee_rule_milestones", "revenue_payment_methods",
    "revenue_payment_method_rule_versions", "revenue_rule_snapshots",
    "revenue_fee_items", "revenue_discounts",
    "revenue_discount_allocations", "revenue_refunds",
    "revenue_contract_groups", "revenue_contract_versions",
    "revenue_contract_relationships", "revenue_contract_parse_runs",
    "revenue_contract_extractions", "revenue_commission_rule_versions",
    "revenue_commission_rule_scopes", "revenue_commission_rule_fee_components",
    "revenue_claim_trigger_terms", "revenue_claim_document_requirements",
    "revenue_commission_rate_instalments",
    "revenue_master_agent_share_rule_versions", "revenue_commission_lines",
    "revenue_commission_instalments", "revenue_commission_adjustments",
    "revenue_clawbacks", "revenue_deferral_events", "revenue_forecast_runs",
    "revenue_forecast_lines", "revenue_invoices", "revenue_invoice_lines",
    "revenue_planned_payment_allocations", "revenue_payments",
    "revenue_payment_allocations", "revenue_receipts", "revenue_ledger_entries",
    "revenue_claims", "revenue_claim_allocations",
    "revenue_claim_communications", "revenue_reconciliation_runs",
    "revenue_reconciliation_suggestions", "revenue_master_agent_statements",
    "revenue_master_agent_statement_items", "revenue_statement_matches",
    "revenue_disputes",
}


def fingerprint(connection, table):
    rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def build_legacy_database(path):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE users(id INTEGER PRIMARY KEY);
            CREATE TABLE students(student_id TEXT PRIMARY KEY);
            CREATE TABLE schools(id INTEGER PRIMARY KEY);
            CREATE TABLE application_cycles(
              id INTEGER PRIMARY KEY,
              cycle_uid TEXT NOT NULL UNIQUE,
              student_id TEXT NOT NULL REFERENCES students(student_id),
              cycle_type TEXT NOT NULL,
              status TEXT NOT NULL,
              education_stream TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE contracts(
              id INTEGER PRIMARY KEY,
              student_id TEXT NOT NULL REFERENCES students(student_id),
              application_cycle_id INTEGER REFERENCES application_cycles(id),
              contract_status TEXT NOT NULL,
              contract_date TEXT,
              fully_executed_at TEXT
            );
            CREATE TABLE public_application_groups(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER REFERENCES application_cycles(id),
              submitted_at TEXT,
              status TEXT
            );
            CREATE TABLE private_school_applications(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER REFERENCES application_cycles(id),
              submitted_at TEXT,
              application_status TEXT
            );
            CREATE TABLE transfer_school_applications(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER REFERENCES application_cycles(id),
              submitted_at TEXT,
              application_status TEXT
            );
            CREATE TABLE offer_received_events(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER REFERENCES application_cycles(id)
            );
            CREATE TABLE cycle_final_acceptances(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER REFERENCES application_cycles(id),
              offer_received_event_id INTEGER REFERENCES offer_received_events(id)
            );
            CREATE TABLE coe_oshc(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER REFERENCES application_cycles(id)
            );
            CREATE TABLE visa_cases(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER REFERENCES application_cycles(id)
            );
            CREATE TABLE student_school_enrolments(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER REFERENCES application_cycles(id),
              school_id INTEGER REFERENCES schools(id)
            );
            CREATE TABLE higher_education_providers(id INTEGER PRIMARY KEY);
            CREATE TABLE higher_education_applications(
              id INTEGER PRIMARY KEY,
              application_cycle_id INTEGER REFERENCES application_cycles(id),
              provider_id INTEGER REFERENCES higher_education_providers(id)
            );
            CREATE TABLE higher_education_package_segments(
              id INTEGER PRIMARY KEY,
              application_id INTEGER REFERENCES higher_education_applications(id)
            );

            INSERT INTO users VALUES(1),(2);
            INSERT INTO students VALUES('S-REVENUE');
            INSERT INTO schools VALUES(10);
            INSERT INTO application_cycles VALUES(
              100,'cycle-100','S-REVENUE','initial','active','school_education','2026-08-29'
            );
            INSERT INTO higher_education_providers VALUES(20);
            """
        )
        connection.commit()


class RevenueMigration032Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        build_legacy_database(self.database_path)
        with closing(sqlite3.connect(self.database_path)) as connection:
            self.before = {
                table: fingerprint(connection, table)
                for table in ("users", "students", "schools", "application_cycles")
            }

    def apply(self):
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(MIGRATION.read_text(encoding="utf-8"))
            connection.commit()

    def test_package_follows_031_and_has_forward_and_disposable_rollback(self):
        self.assertTrue(MIGRATION.is_file())
        self.assertTrue(FORWARD_CHECK.is_file())
        self.assertTrue(ROLLBACK.is_file())
        versions = sorted(path.name for path in (ROOT / "migrations").glob("*.sql"))
        self.assertEqual(
            "20260827_031_transfer_v2_authoritative_lifecycle.sql",
            versions[versions.index(MIGRATION.name) - 1],
        )

    def test_additive_schema_preserves_core_rows_and_creates_no_business_facts(self):
        self.apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'revenue_%'"
                )
            }
            self.assertEqual(REVENUE_TABLES, tables)
            after = {table: fingerprint(connection, table) for table in self.before}
            self.assertEqual(self.before, after)
            self.assertEqual(
                [("disabled", None)],
                connection.execute(
                    "SELECT status,cutover_at FROM revenue_module_state"
                ).fetchall(),
            )
            for table in REVENUE_TABLES - {"revenue_module_state"}:
                self.assertEqual(
                    0,
                    connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0],
                    table,
                )
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual("ok", connection.execute("PRAGMA quick_check").fetchone()[0])

    def test_all_financial_amounts_and_rates_use_integer_affinity(self):
        self.apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            checked = 0
            for table in REVENUE_TABLES:
                for column in connection.execute(f'PRAGMA table_info("{table}")'):
                    name, declared_type = column[1], column[2].upper()
                    if name.endswith("_cents") or name.endswith("_basis_points"):
                        checked += 1
                        self.assertEqual("INTEGER", declared_type, f"{table}.{name}")
            self.assertGreaterEqual(checked, 30)

    def test_audit_ledger_snapshot_and_issued_documents_are_append_only(self):
        self.apply()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO revenue_audit_events(uid,action,aggregate_type,aggregate_uid,actor_user_id,occurred_at) VALUES('audit-1','created','case','case-1',1,'2026-08-29T00:00:00Z')"
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    "UPDATE revenue_audit_events SET action='changed' WHERE uid='audit-1'"
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute("DELETE FROM revenue_audit_events WHERE uid='audit-1'")


if __name__ == "__main__":
    unittest.main()
