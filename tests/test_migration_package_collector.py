import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest


EXPECTED = (
    "20260728_001",
    "20260729_002",
    "20260729_003",
    "20260729_004",
    "20260729_005",
    "20260730_006",
    "20260730_007",
    "20260801_008",
)


class MigrationPackageCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "migrations"
        self.checks = self.source / "checks"
        self.destination = self.root / "package"
        self.source.mkdir()
        self.checks.mkdir()
        self.entries = []
        for migration_id in EXPECTED:
            migration_name = f"{migration_id}_fixture.sql"
            check_name = f"{migration_id}_fixture_forward_check.sql"
            migration = self.source / migration_name
            check = self.checks / check_name
            migration.write_text(f"-- {migration_id}\nSELECT 1;\n", encoding="utf-8")
            check.write_text("SELECT 1;\n", encoding="utf-8")
            self.entries.append({
                "id": migration_id,
                "migration_file": migration_name,
                "migration_sha256": self.sha(migration),
                "forward_check_file": f"checks/{check_name}",
                "forward_check_sha256": self.sha(check),
            })
        self.manifest = self.root / "manifest.json"
        self.write_manifest(self.entries)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def sha(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def write_manifest(self, entries):
        self.manifest.write_text(json.dumps({
            "contract": "combined-migration-package-v1",
            "expected_ids": list(EXPECTED),
            "migrations": entries,
        }), encoding="utf-8")

    def collect(self, *, through_id=None):
        from tools.migration_package_collector import build_migration_package
        return build_migration_package(
            self.source, self.destination, self.manifest,
            through_id=through_id,
        )

    def test_collects_exact_standard_migrations_in_id_order(self):
        result = self.collect()
        self.assertEqual(result["migration_ids"], list(EXPECTED))
        self.assertEqual(
            sorted(path.name for path in self.destination.glob("*.sql")),
            [entry["migration_file"] for entry in self.entries],
        )
        self.assertEqual(len(list((self.destination / "checks").glob("*.sql"))), 8)

    def test_ignores_appledouble_files(self):
        (self.source / "._20260728_001_fixture.sql").write_bytes(b"appledouble")
        (self.checks / "._20260728_001_fixture_forward_check.sql").write_bytes(b"appledouble")
        self.collect()
        self.assertFalse(any(path.name.startswith("._") for path in self.destination.rglob("*")))

    def test_ignores_ds_store_and_non_sql_files(self):
        (self.source / ".DS_Store").write_bytes(b"metadata")
        (self.source / "notes.txt").write_text("not a migration", encoding="utf-8")
        self.collect()
        self.assertFalse((self.destination / ".DS_Store").exists())
        self.assertFalse((self.destination / "notes.txt").exists())

    def test_rejects_visible_nonstandard_sql_file(self):
        (self.source / "migration_backup.sql").write_text("SELECT 1;", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "non-standard SQL"):
            self.collect()

    def test_rejects_symlink_even_when_name_is_standard(self):
        target = self.root / "target.sql"
        target.write_text("SELECT 1;", encoding="utf-8")
        link = self.source / "20260802_009_symlink.sql"
        os.symlink(target, link)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.collect()

    def test_rejects_duplicate_migration_id(self):
        (self.source / "20260728_001_duplicate.sql").write_text("SELECT 1;", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate migration ID"):
            self.collect()

    def test_rejects_missing_001(self):
        (self.source / self.entries[0]["migration_file"]).unlink()
        with self.assertRaisesRegex(ValueError, "missing migration"):
            self.collect()

    def test_rejects_any_missing_002_through_008(self):
        (self.source / self.entries[4]["migration_file"]).unlink()
        with self.assertRaisesRegex(ValueError, "missing migration"):
            self.collect()

    def test_rejects_extra_009(self):
        (self.source / "20260802_009_extra.sql").write_text("SELECT 1;", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unexpected migration"):
            self.collect()

    def test_rejects_manifest_sha_mismatch(self):
        self.entries[2]["migration_sha256"] = "0" * 64
        self.write_manifest(self.entries)
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            self.collect()

    def test_rejects_manifest_with_missing_or_duplicate_ids(self):
        for entries in (self.entries[:-1], [*self.entries, self.entries[-1]]):
            with self.subTest(count=len(entries)):
                self.write_manifest(entries)
                with self.assertRaisesRegex(ValueError, "manifest migration IDs"):
                    self.collect()

    def test_stage_view_is_an_exact_manifest_prefix(self):
        result = self.collect(through_id="20260729_005")
        self.assertEqual(result["migration_ids"], list(EXPECTED[:5]))
        self.assertEqual(
            sorted(path.name for path in self.destination.glob("*.sql")),
            [entry["migration_file"] for entry in self.entries[:5]],
        )

    def test_rejects_unknown_stage_boundary(self):
        with self.assertRaisesRegex(ValueError, "stage boundary"):
            self.collect(through_id="20260802_009")


if __name__ == "__main__":
    unittest.main()
