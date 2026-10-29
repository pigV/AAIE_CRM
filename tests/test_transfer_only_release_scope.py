from pathlib import Path
import hashlib
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TransferOnlyReleaseScopeTests(unittest.TestCase):
    def test_transfer_only_candidate_contains_exact_041_042_without_revenue_040_dependency(self):
        required = [
            ROOT / "migrations/20260920_041_transfer_source_authority.sql",
            ROOT / "migrations/20260920_042_transfer_source_verified_lifecycle.sql",
            ROOT / "migrations/checks/20260920_041_transfer_source_authority_forward_check.sql",
            ROOT / "migrations/checks/20260920_042_transfer_source_verified_lifecycle_forward_check.sql",
            ROOT / "migrations/rollback/20260920_041_transfer_source_authority_rollback.sql",
            ROOT / "migrations/rollback/20260920_042_transfer_source_verified_lifecycle_rollback.sql",
            ROOT / "student_lifecycle/transfer_source_authority.py",
        ]
        missing = [str(path.relative_to(ROOT)) for path in required if not path.is_file()]
        assert not missing, f"missing Transfer-only artifacts: {missing}"
        for path in required[:6]:
            text = path.read_text()
            self.assertNotIn("20260920_040", text)
            self.assertIsNone(
                __import__("re").search(r"\b(?:revenue|visa)_[a-z0-9_]+", text, __import__("re").I),
                f"cross-domain schema dependency in {path.relative_to(ROOT)}",
            )

        from migration_runner import discover_migrations
        migration_041 = next(
            item for item in discover_migrations(ROOT / "migrations")
            if item.version == "20260920_041"
        )
        check_042 = required[3].read_text()
        self.assertIn(migration_041.checksum_sha256, check_042)
    
    def test_transfer_only_candidate_excludes_revenue_and_visa_migrations(self):
        self.assertFalse((ROOT / "migrations/20260920_040_revenue_financial_authority.sql").exists())
        visa_039 = ROOT / "migrations/20260914_039_visa_result_status_authority.sql"
        self.assertTrue(visa_039.is_file())
        self.assertEqual(hashlib.sha256(visa_039.read_bytes()).hexdigest(), "c9c4438670ea0cfa965452e29381498031f5f0a9464d049a06df01956718afa1")
