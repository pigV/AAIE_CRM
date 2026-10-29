import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.crm_safe_box import SafeBoxManager


class SafeBoxManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._write_project_files()
        self._init_git_repo()

    def tearDown(self):
        self.tmp.cleanup()

    def _write_project_files(self):
        (self.root / "database").mkdir()
        db_path = self.root / "database" / "aaie_crm.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute("INSERT INTO students (name) VALUES ('Test Student')")

        (self.root / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
        (self.root / "adjustment.md").write_text("# Adjustments\n", encoding="utf-8")
        (self.root / ".env.production").write_text("CRM_TEST_ONLY=1\n", encoding="utf-8")
        (self.root / "upload_staging").mkdir()
        (self.root / "school_library_files").mkdir()
        (self.root / "secure_assets").mkdir()

    def _init_git_repo(self):
        subprocess.run(["git", "init"], cwd=self.root, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "AGENTS.md", "adjustment.md"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=self.root, check=True, stdout=subprocess.DEVNULL)

    def test_create_safe_point_writes_manifest_database_backup_config_and_restore_notes(self):
        manager = SafeBoxManager(self.root)

        safe_point = manager.create_safe_point(
            name="before offer fix",
            notes="Unit test safety point",
            timestamp="20260706_203000",
        )

        self.assertEqual(safe_point.path.name, "20260706_203000_before-offer-fix")
        manifest = json.loads((safe_point.path / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["name"], "before-offer-fix")
        self.assertEqual(manifest["notes"], "Unit test safety point")
        self.assertEqual(manifest["database"]["quick_check"], "ok")
        self.assertTrue(manifest["complete"])
        self.assertTrue(safe_point.manifest["complete"])
        self.assertTrue((safe_point.path / "aaie_crm.db").is_file())
        self.assertTrue((safe_point.path / "config" / "AGENTS.md").is_file())
        self.assertTrue((safe_point.path / "config" / "adjustment.md").is_file())
        self.assertTrue((safe_point.path / "restore_notes.md").is_file())
        self.assertEqual(manifest["upload_directories"]["upload_staging"]["exists"], True)
        self.assertIn("git checkout", manifest["restore"]["git_command"])

        with sqlite3.connect(safe_point.path / "aaie_crm.db") as conn:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_list_safe_points_reports_complete_points(self):
        manager = SafeBoxManager(self.root)
        manager.create_safe_point(name="first", timestamp="20260706_203000")
        manager.create_safe_point(name="second", timestamp="20260706_204000")

        points = manager.list_safe_points()

        self.assertEqual([point["name"] for point in points], ["first", "second"])
        self.assertTrue(all(point["complete"] for point in points))

    def test_restore_dry_run_reports_plan_without_modifying_database(self):
        manager = SafeBoxManager(self.root)
        safe_point = manager.create_safe_point(name="before change", timestamp="20260706_203000")
        before_size = (self.root / "database" / "aaie_crm.db").stat().st_size

        plan = manager.restore_plan("before-change")

        self.assertEqual((self.root / "database" / "aaie_crm.db").stat().st_size, before_size)
        self.assertEqual(plan["safe_point"], str(safe_point.path))
        self.assertIn("git checkout", plan["git_command"])
        self.assertIn("aaie_crm.db", plan["database_restore_command"])
        self.assertTrue(plan["pre_restore_backup_required"])
        self.assertIn("--confirm", plan["risk_warning"])

    def test_restore_requires_confirm_for_real_restore(self):
        manager = SafeBoxManager(self.root)
        manager.create_safe_point(name="before change", timestamp="20260706_203000")

        with self.assertRaises(ValueError):
            manager.restore("before-change", confirm=False)


if __name__ == "__main__":
    unittest.main()
