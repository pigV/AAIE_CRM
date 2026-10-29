import sqlite3
import unittest

from tools import transfer_contract_ownership_migration_acceptance as acceptance


class TransferContractOwnershipMigrationAcceptanceTests(unittest.TestCase):
    def test_protected_fingerprints_cover_f2_and_longitudinal_boundaries(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        representative_tables = (
            "contracts",
            "offers",
            "coe_oshc",
            "visas",
            "school_plan_reports",
            "application_cycles",
            "student_cases",
            "transfer_cases",
            "progression_cases",
            "student_school_enrolments",
            "reminders",
            "content_scripts",
            "contract_template_identities",
            "contract_template_versions",
            "contract_template_clause_blocks",
            "contract_template_service_items",
            "contract_template_simulations",
            "contract_template_validations",
            "contract_template_audit_events",
        )
        for table in representative_tables:
            connection.execute(
                f'CREATE TABLE "{table}"(id INTEGER PRIMARY KEY,value TEXT)'
            )
            connection.execute(
                f'INSERT INTO "{table}"(id,value) VALUES(1,?)', (table,)
            )
        connection.execute(
            "CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT)"
        )
        connection.executemany(
            "INSERT INTO system_settings(key,value) VALUES(?,?)",
            (
                ("contract_generation_authority", "published_v2"),
                ("unrelated_setting", "ignored"),
            ),
        )

        fingerprint_builder = getattr(
            acceptance, "protected_business_fingerprints", None
        )
        self.assertIsNotNone(fingerprint_builder)
        fingerprints = fingerprint_builder(connection)
        expected_families = {
            "contracts",
            "offers",
            "coe_oshc",
            "visa",
            "school_plan",
            "cycles",
            "transfer_progression_cases",
            "school_enrolments",
            "reminders",
            "drive_content_library",
            "contract_template_v1",
            "contract_generation_authority",
        }
        self.assertEqual(expected_families, set(fingerprints))

        for table, family in (
            ("contract_template_versions", "contract_template_v1"),
            ("student_school_enrolments", "school_enrolments"),
            ("content_scripts", "drive_content_library"),
        ):
            before = fingerprints[family]
            connection.execute(f'UPDATE "{table}" SET value=value || "-changed"')
            after = fingerprint_builder(connection)
            self.assertNotEqual(before, after[family], table)
            fingerprints = after

        authority_before = fingerprints["contract_generation_authority"]
        connection.execute(
            "UPDATE system_settings SET value='changed' WHERE key='unrelated_setting'"
        )
        self.assertEqual(
            authority_before,
            fingerprint_builder(connection)["contract_generation_authority"],
        )
        connection.execute(
            "UPDATE system_settings SET value='legacy_v1' "
            "WHERE key='contract_generation_authority'"
        )
        self.assertNotEqual(
            authority_before,
            fingerprint_builder(connection)["contract_generation_authority"],
        )

    def test_protected_fingerprints_project_pre_migration_columns(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE TABLE transfer_cases(id INTEGER PRIMARY KEY,status TEXT)"
        )
        connection.execute(
            "INSERT INTO transfer_cases(id,status) VALUES(1,'active')"
        )
        column_capture = getattr(
            acceptance, "protected_fingerprint_columns", None
        )
        self.assertIsNotNone(column_capture)
        before_columns = column_capture(connection)
        before = acceptance.protected_business_fingerprints(
            connection, before_columns
        )

        connection.execute(
            "ALTER TABLE transfer_cases ADD COLUMN source_contract_id INTEGER"
        )
        after_schema = acceptance.protected_business_fingerprints(
            connection, before_columns
        )
        self.assertEqual(
            before["transfer_progression_cases"],
            after_schema["transfer_progression_cases"],
        )

        connection.execute("UPDATE transfer_cases SET status='completed'")
        after_data = acceptance.protected_business_fingerprints(
            connection, before_columns
        )
        self.assertNotEqual(
            before["transfer_progression_cases"],
            after_data["transfer_progression_cases"],
        )

    def test_accepts_current_production_already_at_reminder_026(self):
        authority_check = getattr(acceptance, "_reminder_authority_accepted", None)
        self.assertIsNotNone(authority_check)
        self.assertTrue(
            authority_check(
                production_latest="20260820_026",
                reminder_applied=[],
                reminder_pending=[],
                before_latest="20260820_026",
            )
        )

    def test_accepts_legacy_ordered_rehearsal_from_025(self):
        authority_check = getattr(acceptance, "_reminder_authority_accepted", None)
        self.assertIsNotNone(authority_check)
        self.assertTrue(
            authority_check(
                production_latest="20260818_025",
                reminder_applied=["20260820_026"],
                reminder_pending=[],
                before_latest="20260820_026",
            )
        )

    def test_rejects_unexpected_registry_or_pending_state(self):
        authority_check = getattr(acceptance, "_reminder_authority_accepted", None)
        self.assertIsNotNone(authority_check)
        self.assertFalse(
            authority_check(
                production_latest="20260820_026",
                reminder_applied=["20260820_026"],
                reminder_pending=[],
                before_latest="20260820_026",
            )
        )
        self.assertFalse(
            authority_check(
                production_latest="20260820_026",
                reminder_applied=[],
                reminder_pending=["20260820_027"],
                before_latest="20260820_026",
            )
        )


if __name__ == "__main__":
    unittest.main()
