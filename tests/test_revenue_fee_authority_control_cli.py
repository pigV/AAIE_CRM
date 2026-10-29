from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import revenue_fee_authority_control as command
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueFeeAuthorityControlCommandTests(RevenueDatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with self.connect() as connection:
            connection.execute("CREATE TABLE roles(id INTEGER PRIMARY KEY,name TEXT NOT NULL)")
            connection.execute("ALTER TABLE users ADD COLUMN role_id INTEGER")
            connection.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
            connection.execute("ALTER TABLE users ADD COLUMN deleted_at TEXT")
            connection.execute("INSERT INTO roles VALUES(1,'Admin'),(2,'Manager')")
            connection.execute("UPDATE users SET role_id=1 WHERE id=1")
            connection.execute("UPDATE users SET role_id=2 WHERE id=2")

    def test_production_mutation_is_locked_without_separate_confirmation(self):
        with self.assertRaisesRegex(PermissionError, "separate user approval"):
            command._require_mutation_authority(command.CANONICAL_PRODUCTION_DB, None)

    def test_preview_is_read_only_sha_bound_and_non_overwrite(self):
        output = Path(self.temp_dir.name) / "proposal.json"
        args = SimpleNamespace(
            database=str(self.database_path),
            output=str(output),
            admin_user_id=1,
            source_label="isolated-test",
            hash_source=True,
        )
        with patch.object(command, "ROOT", Path(self.temp_dir.name)):
            result = command.preview(args)
            self.assertEqual(64, len(result["proposal_sha256"]))
            self.assertTrue(output.is_file())
            with self.assertRaises(FileExistsError):
                command.preview(args)

    def test_exact_admin_authority_is_resolved_from_database(self):
        with self.connect() as connection:
            self.assertEqual("Admin", command._require_exact_admin(connection, 1))
            with self.assertRaisesRegex(PermissionError, "exact active Admin"):
                command._require_exact_admin(connection, 2)
            connection.execute("UPDATE users SET is_active=0 WHERE id=1")
            with self.assertRaisesRegex(PermissionError, "exact active Admin"):
                command._require_exact_admin(connection, 1)

    def test_preview_rejects_non_admin_database_identity(self):
        args = SimpleNamespace(
            database=str(self.database_path),
            output=str(Path(self.temp_dir.name) / "manager-proposal.json"),
            admin_user_id=2,
            source_label="isolated-test",
            hash_source=False,
        )
        with patch.object(command, "ROOT", Path(self.temp_dir.name)):
            with self.assertRaisesRegex(PermissionError, "exact active Admin"):
                command.preview(args)


if __name__ == "__main__":
    unittest.main()
