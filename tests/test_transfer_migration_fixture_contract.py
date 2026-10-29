from contextlib import closing
import importlib
import unittest


transfer_fixture_module = importlib.import_module(
    "tests.test_student_case_phase2_transfer"
)


class TransferMigrationFixtureContractTests(unittest.TestCase):
    def test_legacy_fixture_contains_content_tables_required_by_migration_028(self):
        fixture = transfer_fixture_module.TransferPhase2MigrationTests(
            methodName="test_production_package_is_discoverable_with_stable_checksum"
        )
        fixture.setUp()
        try:
            with closing(fixture.connect(fixture.database_path)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
        finally:
            fixture.tearDown()

        self.assertIn("content_scripts", tables)
        self.assertIn("content_materials", tables)


if __name__ == "__main__":
    unittest.main()
