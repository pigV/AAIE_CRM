import ast
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import database


ROOT = Path(__file__).resolve().parents[1]


class TransferStagingApplicationHookTests(unittest.TestCase):
    def test_database_checks_active_staging_authority_at_open_boundaries(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "STAGING_DATA") as directory:
            target = Path(directory) / "database" / "staging.db"
            authority = SimpleNamespace(check=mock.Mock(side_effect=lambda path: Path(path)))
            with mock.patch.object(database, "STAGING", authority, create=True):
                handle = database.Database(target)
                connection = handle.connect()
                connection.close()

            self.assertGreaterEqual(authority.check.call_count, 2)
            authority.check.assert_any_call(target)

    def test_app_uses_staging_paths_and_exposes_visible_health_identity(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        ast.parse(source)

        required = (
            "from staging_environment import ACTIVE as STAGING, SOURCE as SOURCE_DIR",
            'raise RuntimeError("Staging must be started through tools/staging_control.py")',
            "APP_DIR = STAGING.root if STAGING else Path(__file__).resolve().parent",
            "DB_PATH = STAGING.db if STAGING else APP_DIR / \"database\" / \"aaie_crm.db\"",
            "value = STAGING.scoped_set_cookie(value)",
            'self.headers["Cookie"] = STAGING.unscoped_cookie_header(',
            '"environment": "staging"',
            '"storage_path": str(STAGING.storage)',
        )
        for expression in required:
            with self.subTest(expression=expression):
                self.assertIn(expression, source)


if __name__ == "__main__":
    unittest.main()
