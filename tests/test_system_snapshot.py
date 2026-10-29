import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import (
    daily_system_snapshot,
    hourly_system_snapshot,
    system_snapshot_manifest,
    system_snapshot_restore_plan,
)


MELBOURNE = ZoneInfo("Australia/Melbourne")


class SystemSnapshotTests(unittest.TestCase):
    def run_git(self, repo, *args):
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    def init_project(self, root):
        root.mkdir(parents=True)
        for directory in (
            "assets",
            "tools",
            "tests",
            "docs",
            "templates",
            "config",
            "database",
            "uploads",
            "upload_staging",
            "secure_assets",
            "backups",
            "logs",
            "__pycache__",
        ):
            (root / directory).mkdir(parents=True, exist_ok=True)
        (root / "app.py").write_text("print('crm')\n", encoding="utf-8")
        (root / "database.py").write_text("SCHEMA = 'v1'\n", encoding="utf-8")
        (root / "ui.py").write_text("UI = True\n", encoding="utf-8")
        (root / "assets" / "style.css").write_text("body { color: #123; }\n", encoding="utf-8")
        (root / "assets" / "app.js").write_text("console.log('crm');\n", encoding="utf-8")
        (root / "tools" / "safe_tool.py").write_text("print('tool')\n", encoding="utf-8")
        (root / "tests" / "test_core.py").write_text("def test_core(): pass\n", encoding="utf-8")
        (root / "docs" / "README.md").write_text("# docs\n", encoding="utf-8")
        (root / "templates" / "contract.html").write_text("<p>template</p>\n", encoding="utf-8")
        (root / "config" / "crm.example").write_text("setting=true\n", encoding="utf-8")
        (root / "uploads" / "student-upload.pdf").write_text("do not copy", encoding="utf-8")
        (root / "upload_staging" / "staged-upload.pdf").write_text("do not copy", encoding="utf-8")
        (root / "secure_assets" / "signature.png").write_text("do not copy", encoding="utf-8")
        (root / "backups" / "AAIE_CRM_Backup_keep.zip").write_text("do not copy", encoding="utf-8")
        (root / "logs" / "admin.log").write_text("do not copy", encoding="utf-8")
        (root / "__pycache__" / "app.pyc").write_bytes(b"cache")

        db_path = root / "database" / "aaie_crm.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE safebox_records (id INTEGER PRIMARY KEY, feature_name TEXT)")
            conn.execute("INSERT INTO safebox_records(feature_name) VALUES ('snapshot test')")
            conn.execute("CREATE TABLE visa_cases (id INTEGER PRIMARY KEY, student_id TEXT)")

        self.run_git(root, "init")
        self.run_git(root, "config", "user.email", "codex@example.test")
        self.run_git(root, "config", "user.name", "Codex Test")
        self.run_git(root, "add", "app.py", "database.py", "ui.py", "assets", "tools", "tests", "docs", "templates", "config")
        self.run_git(root, "commit", "-m", "initial crm system")
        return db_path

    def read_json(self, path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def test_hourly_snapshot_inside_active_window_creates_slot_with_database_manifest_and_exclusions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            snapshot_root = root / "backups" / "hourly-system-snapshots"
            now = datetime(2026, 7, 11, 9, 30, tzinfo=MELBOURNE)

            result = hourly_system_snapshot.create_hourly_snapshot(
                root=root,
                db_path=db_path,
                snapshot_root=snapshot_root,
                now=now,
            )

            slot = snapshot_root / "hourly_0"
            self.assertFalse(result["was_skipped"])
            self.assertTrue((slot / "snapshot.zip").is_file())
            self.assertTrue((slot / "database_backup.sqlite").is_file())
            self.assertTrue((slot / "manifest.json").is_file())
            self.assertTrue((slot / "git_status.txt").is_file())
            self.assertTrue((slot / "git_diff.patch").is_file())
            self.assertTrue((slot / "restore_instructions.txt").is_file())
            self.assertEqual((slot / "created_at.txt").read_text(encoding="utf-8").strip(), result["created_at"])

            manifest = self.read_json(slot / "manifest.json")
            self.assertEqual(manifest["backup_type"], "hourly")
            self.assertEqual(manifest["active_window_start"], 9)
            self.assertEqual(manifest["active_window_end"], 21)
            self.assertFalse(manifest["was_skipped"])
            self.assertFalse(manifest["force_used"])
            self.assertEqual(manifest["database_quick_check_result"], "ok")
            self.assertIn("app.py", manifest["included_paths"])
            self.assertIn("database.py", manifest["included_paths"])
            self.assertIn("assets/style.css", manifest["included_paths"])
            self.assertNotIn("uploads/student-upload.pdf", manifest["included_paths"])
            self.assertNotIn("backups/AAIE_CRM_Backup_keep.zip", manifest["included_paths"])

            with zipfile.ZipFile(slot / "snapshot.zip") as zf:
                names = set(zf.namelist())
            self.assertIn("system_files/app.py", names)
            self.assertIn("system_files/database.py", names)
            self.assertIn("system_files/assets/style.css", names)
            self.assertNotIn("system_files/uploads/student-upload.pdf", names)
            self.assertNotIn("system_files/backups/AAIE_CRM_Backup_keep.zip", names)
            self.assertFalse(any("/._" in name or name.split("/")[-1].startswith("._") for name in names))
            self.assertFalse(any("__pycache__" in name for name in names))

    def test_hourly_snapshot_outside_active_window_skips_and_does_not_overwrite_existing_slots(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            snapshot_root = root / "backups" / "hourly-system-snapshots"
            existing_slot = snapshot_root / "hourly_0"
            existing_slot.mkdir(parents=True)
            sentinel = existing_slot / "created_at.txt"
            sentinel.write_text("existing-slot\n", encoding="utf-8")
            now = datetime(2026, 7, 11, 22, 0, tzinfo=MELBOURNE)

            result = hourly_system_snapshot.create_hourly_snapshot(
                root=root,
                db_path=db_path,
                snapshot_root=snapshot_root,
                now=now,
            )

            self.assertTrue(result["was_skipped"])
            self.assertEqual(result["skip_reason"], "skipped_outside_active_window")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "existing-slot\n")
            skipped = self.read_json(snapshot_root / "last_skipped_manifest.json")
            self.assertTrue(skipped["was_skipped"])
            self.assertEqual(skipped["skip_reason"], "skipped_outside_active_window")
            self.assertIn("skipped_outside_active_window", (snapshot_root / "snapshot.log").read_text(encoding="utf-8"))

    def test_hourly_snapshot_force_outside_active_window_creates_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            snapshot_root = root / "backups" / "hourly-system-snapshots"
            now = datetime(2026, 7, 11, 22, 0, tzinfo=MELBOURNE)

            result = hourly_system_snapshot.create_hourly_snapshot(
                root=root,
                db_path=db_path,
                snapshot_root=snapshot_root,
                now=now,
                force=True,
            )

            slot = snapshot_root / "hourly_1"
            manifest = self.read_json(slot / "manifest.json")
            self.assertFalse(result["was_skipped"])
            self.assertTrue(manifest["force_used"])
            self.assertEqual(manifest["current_local_time"], "2026-07-11T22:00:00+10:00")
            self.assertIn("created_hourly_snapshot", (snapshot_root / "snapshot.log").read_text(encoding="utf-8"))

    def test_hourly_snapshot_keeps_six_slots_and_seventh_run_overwrites_oldest_slot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            snapshot_root = root / "backups" / "hourly-system-snapshots"
            for hour in range(9, 16):
                hourly_system_snapshot.create_hourly_snapshot(
                    root=root,
                    db_path=db_path,
                    snapshot_root=snapshot_root,
                    now=datetime(2026, 7, 11, hour, 0, tzinfo=MELBOURNE),
                )

            slots = sorted(path.name for path in snapshot_root.glob("hourly_*") if path.is_dir())
            self.assertEqual(slots, [f"hourly_{index}" for index in range(6)])
            overwritten = self.read_json(snapshot_root / "hourly_0" / "manifest.json")
            self.assertEqual(overwritten["current_local_time"], "2026-07-11T15:00:00+10:00")
            self.assertEqual(overwritten["slot_name"], "hourly_0")

    def test_hourly_snapshot_creates_missing_root_cleans_tmp_and_writes_complete_slot_without_sqlite_sidecars(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            snapshot_root = root / "backups" / "hourly-system-snapshots"
            shutil.rmtree(snapshot_root.parent)
            stale_tmp = snapshot_root / "hourly_5.tmp"
            stale_tmp.mkdir(parents=True)
            (stale_tmp / "stale.txt").write_text("stale temp", encoding="utf-8")
            stale_legacy_tmp = snapshot_root / ".hourly_5.tmp"
            stale_legacy_tmp.mkdir(parents=True)
            (stale_legacy_tmp / "stale.txt").write_text("legacy stale temp", encoding="utf-8")

            with sqlite3.connect(db_path) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("CREATE TABLE snapshot_blob_test (id INTEGER PRIMARY KEY, payload TEXT)")
                conn.executemany(
                    "INSERT INTO snapshot_blob_test(payload) VALUES (?)",
                    [("x" * 2048,) for _ in range(32)],
                )

            result = hourly_system_snapshot.create_hourly_snapshot(
                root=root,
                db_path=db_path,
                snapshot_root=snapshot_root,
                now=datetime(2026, 7, 11, 14, 0, tzinfo=MELBOURNE),
            )

            slot = snapshot_root / "hourly_5"
            self.assertFalse(result["was_skipped"])
            self.assertFalse(stale_tmp.exists())
            self.assertFalse(stale_legacy_tmp.exists())
            for filename in (
                "snapshot.zip",
                "database_backup.sqlite",
                "manifest.json",
                "git_status.txt",
                "git_diff.patch",
                "restore_instructions.txt",
                "created_at.txt",
            ):
                self.assertTrue((slot / filename).is_file(), filename)
            self.assertFalse((slot / "database_backup.sqlite-wal").exists())
            self.assertFalse((slot / "database_backup.sqlite-shm").exists())
            self.assertFalse(any(path.name.startswith("._") for path in slot.iterdir()))
            self.assertEqual(self.read_json(slot / "manifest.json")["database_quick_check_result"], "ok")

    def test_hourly_snapshot_failure_keeps_existing_slot_and_writes_failure_log(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            snapshot_root = root / "backups" / "hourly-system-snapshots"
            slot = snapshot_root / "hourly_5"
            slot.mkdir(parents=True)
            sentinel = slot / "created_at.txt"
            sentinel.write_text("existing stable slot\n", encoding="utf-8")
            original_payload = hourly_system_snapshot.snapshot.create_snapshot_payload

            def fail_payload(*args, **kwargs):
                raise RuntimeError("forced payload failure")

            try:
                hourly_system_snapshot.snapshot.create_snapshot_payload = fail_payload
                with self.assertRaises(RuntimeError):
                    hourly_system_snapshot.create_hourly_snapshot(
                        root=root,
                        db_path=db_path,
                        snapshot_root=snapshot_root,
                        now=datetime(2026, 7, 11, 14, 0, tzinfo=MELBOURNE),
                    )
            finally:
                hourly_system_snapshot.snapshot.create_snapshot_payload = original_payload

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "existing stable slot\n")
            self.assertFalse((snapshot_root / "hourly_5.tmp").exists())
            self.assertIn("failed_hourly_snapshot", (snapshot_root / "snapshot.log").read_text(encoding="utf-8"))

    def test_daily_snapshot_ignores_hourly_window_and_keeps_seven_archives_after_successful_new_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            daily_root = root / "backups" / "daily-system-snapshots"
            daily_root.mkdir(parents=True)
            protected_backup = root / "backups" / "AAIE_CRM_Backup_20260701_010101.zip"
            protected_backup.write_text("protected existing backup", encoding="utf-8")
            (root / "backups" / "safe-box").mkdir()
            existing = []
            for day in range(1, 8):
                stamp = f"2026070{day}_010000"
                zip_path = daily_root / f"daily_system_snapshot_{stamp}.zip"
                zip_path.write_text(f"daily {day}", encoding="utf-8")
                zip_path.with_name(zip_path.stem + ".manifest.json").write_text("{}", encoding="utf-8")
                existing.append(zip_path)

            result = daily_system_snapshot.create_daily_snapshot(
                root=root,
                db_path=db_path,
                snapshot_root=daily_root,
                now=datetime(2026, 7, 11, 23, 30, tzinfo=MELBOURNE),
            )

            self.assertTrue(Path(result["snapshot_path"]).is_file())
            self.assertTrue(Path(result["manifest_path"]).is_file())
            self.assertFalse(existing[0].exists())
            self.assertFalse(existing[0].with_name(existing[0].stem + ".manifest.json").exists())
            self.assertTrue(existing[-1].exists())
            self.assertEqual(len(list(daily_root.glob("daily_system_snapshot_*.zip"))), 7)
            self.assertTrue(protected_backup.exists())
            self.assertTrue((root / "backups" / "safe-box").is_dir())
            manifest = self.read_json(result["manifest_path"])
            self.assertEqual(manifest["backup_type"], "daily")
            self.assertFalse(manifest["was_skipped"])
            self.assertEqual(manifest["daily_archive_retention_count"], 7)
            self.assertEqual(manifest["database_quick_check_result"], "ok")
            self.assertIn("created_daily_snapshot", (daily_root / "snapshot.log").read_text(encoding="utf-8"))
            self.assertIn("rotated_daily_snapshot", (daily_root / "snapshot.log").read_text(encoding="utf-8"))

    def test_daily_snapshot_does_not_rotate_when_archive_count_is_seven_or_less(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            daily_root = root / "backups" / "daily-system-snapshots"
            daily_root.mkdir(parents=True)
            for day in range(1, 7):
                (daily_root / f"daily_system_snapshot_2026070{day}_010000.zip").write_text("daily", encoding="utf-8")

            result = daily_system_snapshot.create_daily_snapshot(
                root=root,
                db_path=db_path,
                snapshot_root=daily_root,
                now=datetime(2026, 7, 7, 23, 30, tzinfo=MELBOURNE),
            )

            self.assertTrue(Path(result["snapshot_path"]).is_file())
            self.assertEqual(len(list(daily_root.glob("daily_system_snapshot_*.zip"))), 7)
            self.assertEqual(result["removed_old_daily_snapshot_files"], [])

    def test_delete_daily_snapshot_requires_confirmation_and_safe_daily_zip_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            self.init_project(root)
            daily_root = root / "backups" / "daily-system-snapshots"
            hourly_root = root / "backups" / "hourly-system-snapshots"
            uploads_root = root / "uploads"
            system_restore_root = root / "system_restore_points"
            daily_root.mkdir(parents=True)
            hourly_root.mkdir(parents=True)
            system_restore_root.mkdir(parents=True)
            allowed_zip = daily_root / "daily_system_snapshot_20260711_233000.zip"
            allowed_zip.write_text("daily snapshot archive", encoding="utf-8")
            allowed_zip.with_name(allowed_zip.stem + ".manifest.json").write_text('{"created_at":"2026-07-11T23:30:00+10:00"}', encoding="utf-8")
            hourly_zip = hourly_root / "daily_system_snapshot_20260711_233000.zip"
            hourly_zip.write_text("hourly must stay", encoding="utf-8")
            upload_file = uploads_root / "student-upload.pdf"
            system_restore_file = system_restore_root / "restore.zip"
            system_restore_file.write_text("system restore must stay", encoding="utf-8")
            database_file = root / "database" / "aaie_crm.db"

            wrong_confirmation = system_snapshot_manifest.delete_daily_snapshot(
                root=root,
                requested_path=allowed_zip,
                confirmation_text="DELETE",
                operator="tester",
            )
            self.assertFalse(wrong_confirmation["success"])
            self.assertTrue(allowed_zip.exists())

            outside = system_snapshot_manifest.delete_daily_snapshot(
                root=root,
                requested_path=hourly_zip,
                confirmation_text=system_snapshot_manifest.DELETE_DAILY_SNAPSHOT_CONFIRMATION,
                operator="tester",
            )
            self.assertFalse(outside["success"])
            self.assertTrue(hourly_zip.exists())

            non_zip = daily_root / "daily_system_snapshot_20260711_233000.txt"
            non_zip.write_text("not zip", encoding="utf-8")
            rejected_non_zip = system_snapshot_manifest.delete_daily_snapshot(
                root=root,
                requested_path=non_zip,
                confirmation_text=system_snapshot_manifest.DELETE_DAILY_SNAPSHOT_CONFIRMATION,
                operator="tester",
            )
            self.assertFalse(rejected_non_zip["success"])
            self.assertTrue(non_zip.exists())

            directory_request = daily_root / "daily_system_snapshot_20260712_233000.zip"
            directory_request.mkdir()
            rejected_directory = system_snapshot_manifest.delete_daily_snapshot(
                root=root,
                requested_path=directory_request,
                confirmation_text=system_snapshot_manifest.DELETE_DAILY_SNAPSHOT_CONFIRMATION,
                operator="tester",
            )
            self.assertFalse(rejected_directory["success"])
            self.assertTrue(directory_request.is_dir())

            traversal = system_snapshot_manifest.delete_daily_snapshot(
                root=root,
                requested_path=daily_root / ".." / "hourly-system-snapshots" / hourly_zip.name,
                confirmation_text=system_snapshot_manifest.DELETE_DAILY_SNAPSHOT_CONFIRMATION,
                operator="tester",
            )
            self.assertFalse(traversal["success"])
            self.assertTrue(hourly_zip.exists())

            symlink_path = daily_root / "daily_system_snapshot_20260713_233000.zip"
            try:
                symlink_path.symlink_to(upload_file)
                rejected_symlink = system_snapshot_manifest.delete_daily_snapshot(
                    root=root,
                    requested_path=symlink_path,
                    confirmation_text=system_snapshot_manifest.DELETE_DAILY_SNAPSHOT_CONFIRMATION,
                    operator="tester",
                )
                self.assertFalse(rejected_symlink["success"])
                self.assertTrue(upload_file.exists())
            except (OSError, NotImplementedError):
                pass

            deleted = system_snapshot_manifest.delete_daily_snapshot(
                root=root,
                requested_path=allowed_zip,
                confirmation_text=system_snapshot_manifest.DELETE_DAILY_SNAPSHOT_CONFIRMATION,
                operator="tester",
            )

            self.assertTrue(deleted["success"])
            self.assertFalse(allowed_zip.exists())
            self.assertTrue(allowed_zip.with_name(allowed_zip.stem + ".manifest.json").exists())
            self.assertTrue(hourly_zip.exists())
            self.assertTrue(upload_file.exists())
            self.assertTrue(system_restore_file.exists())
            self.assertTrue(database_file.exists())
            delete_log = daily_root / "daily_snapshot_delete.log"
            self.assertTrue(delete_log.is_file())
            log_text = delete_log.read_text(encoding="utf-8")
            self.assertIn("deleted_daily_snapshot", log_text)
            self.assertIn("tester", log_text)

    def test_restore_plan_reads_snapshot_and_never_executes_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            snapshot_root = root / "backups" / "hourly-system-snapshots"
            hourly_system_snapshot.create_hourly_snapshot(
                root=root,
                db_path=db_path,
                snapshot_root=snapshot_root,
                now=datetime(2026, 7, 11, 10, 0, tzinfo=MELBOURNE),
            )
            before_app = (root / "app.py").read_text(encoding="utf-8")

            plan = system_snapshot_restore_plan.build_restore_plan(snapshot_root / "hourly_1")

            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), before_app)
            self.assertEqual(plan["backup_type"], "hourly")
            self.assertFalse(plan["restore_actions_executed"])
            self.assertTrue(plan["manual_confirmation_required"])
            self.assertIn("不执行真实恢复", "\n".join(plan["safety_notes"]))
            self.assertIn("database_backup.sqlite", plan["database_backup_path"])
            self.assertIn("app.py", plan["included_paths"])
            self.assertIn("停止 CRM 服务", "\n".join(plan["manual_restore_steps"]))

    def test_snapshot_settings_pause_hourly_and_daily_without_overwriting_slots(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            hourly_root = root / "backups" / "hourly-system-snapshots"
            daily_root = root / "backups" / "daily-system-snapshots"
            slot = hourly_root / "hourly_1"
            slot.mkdir(parents=True)
            sentinel = slot / "created_at.txt"
            sentinel.write_text("keep existing slot\n", encoding="utf-8")

            settings = system_snapshot_manifest.save_snapshot_settings(root, {
                "hourly_paused": True,
                "daily_paused": True,
                "daily_time": "23:30",
            })
            self.assertTrue(settings["hourly_paused"])
            self.assertTrue(settings["daily_paused"])

            hourly = hourly_system_snapshot.create_hourly_snapshot(
                root=root,
                db_path=db_path,
                snapshot_root=hourly_root,
                now=datetime(2026, 7, 11, 10, 0, tzinfo=MELBOURNE),
            )
            self.assertTrue(hourly["was_skipped"])
            self.assertEqual(hourly["skip_reason"], "skipped_paused")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep existing slot\n")
            self.assertIn("skipped_paused", (hourly_root / "snapshot.log").read_text(encoding="utf-8"))

            daily = daily_system_snapshot.create_daily_snapshot(
                root=root,
                db_path=db_path,
                snapshot_root=daily_root,
                now=datetime(2026, 7, 11, 23, 30, tzinfo=MELBOURNE),
            )
            self.assertTrue(daily["was_skipped"])
            self.assertEqual(daily["skip_reason"], "skipped_paused")
            self.assertFalse(list(daily_root.glob("daily_system_snapshot_*.zip")))
            self.assertIn("skipped_paused", (daily_root / "snapshot.log").read_text(encoding="utf-8"))

            system_snapshot_manifest.set_snapshot_paused(root, "hourly", False)
            resumed = hourly_system_snapshot.create_hourly_snapshot(
                root=root,
                db_path=db_path,
                snapshot_root=hourly_root,
                now=datetime(2026, 7, 11, 10, 0, tzinfo=MELBOURNE),
            )
            self.assertFalse(resumed["was_skipped"])
            self.assertTrue((slot / "snapshot.zip").is_file())

    def test_backup_control_status_lists_slots_daily_snapshots_and_launchd_templates_without_installing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            hourly_root = root / "backups" / "hourly-system-snapshots"
            daily_root = root / "backups" / "daily-system-snapshots"
            hourly_system_snapshot.create_hourly_snapshot(
                root=root,
                db_path=db_path,
                snapshot_root=hourly_root,
                now=datetime(2026, 7, 11, 9, 0, tzinfo=MELBOURNE),
            )
            daily = daily_system_snapshot.create_daily_snapshot(
                root=root,
                db_path=db_path,
                snapshot_root=daily_root,
                now=datetime(2026, 7, 11, 23, 30, tzinfo=MELBOURNE),
            )

            status = system_snapshot_manifest.build_backup_control_status(root)
            templates = system_snapshot_manifest.launchd_template_bundle(root)

            self.assertEqual(status["settings"]["daily_time"], "23:30")
            self.assertEqual(status["settings"]["hourly_active_window"], "09:00-21:00")
            self.assertEqual(len(status["hourly_slots"]), 6)
            self.assertEqual(status["hourly_slots"][0]["slot_name"], "hourly_0")
            self.assertEqual(status["hourly_slots"][0]["database_quick_check_result"], "ok")
            self.assertGreater(status["hourly_slots"][0]["code_snapshot_size"], 0)
            self.assertGreater(status["hourly_slots"][0]["database_backup_size"], 0)
            self.assertGreaterEqual(
                status["hourly_slots"][0]["total_snapshot_size"],
                status["hourly_slots"][0]["code_snapshot_size"] + status["hourly_slots"][0]["database_backup_size"],
            )
            self.assertEqual(status["daily_snapshots"][0]["snapshot_path"], daily["snapshot_path"])
            self.assertGreater(status["daily_snapshots"][0]["code_snapshot_size"], 0)
            self.assertGreater(status["daily_snapshots"][0]["database_backup_size"], 0)
            self.assertGreaterEqual(
                status["daily_snapshots"][0]["total_snapshot_size"],
                status["daily_snapshots"][0]["snapshot_size"],
            )
            self.assertEqual(status["size_columns"], ["code_snapshot_size", "database_backup_size", "total_snapshot_size"])
            self.assertIn("database backup", status["size_explanation"].lower())
            self.assertIn("current restore folder size", status["size_explanation"])
            self.assertIn("compressed daily archive size", status["size_explanation"])
            self.assertIn("com.aaie.crm.hourly-system-snapshot", templates["hourly_plist"])
            self.assertIn("com.aaie.crm.daily-system-snapshot", templates["daily_plist"])
            self.assertIn("launchctl bootstrap", templates["install_commands"])
            self.assertIn("/usr/bin/python3", templates["hourly_plist"])
            self.assertIn("/usr/bin/python3", templates["daily_plist"])
            self.assertNotIn(".cache/codex-runtimes", templates["hourly_plist"])
            self.assertNotIn(".cache/codex-runtimes", templates["daily_plist"])
            self.assertFalse((Path.home() / "Library" / "LaunchAgents" / "com.aaie.crm.hourly-system-snapshot.plist").exists() and templates.get("installed_by_tool"))

    def test_backup_control_visual_summary_logs_daily_archive_status_and_launchd_state_are_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            hourly_root = root / "backups" / "hourly-system-snapshots"
            daily_root = root / "backups" / "daily-system-snapshots"
            hourly_system_snapshot.create_hourly_snapshot(
                root=root,
                db_path=db_path,
                snapshot_root=hourly_root,
                now=datetime(2026, 7, 11, 10, 0, tzinfo=MELBOURNE),
            )
            daily_system_snapshot.create_daily_snapshot(
                root=root,
                db_path=db_path,
                snapshot_root=daily_root,
                now=datetime(2026, 7, 11, 23, 30, tzinfo=MELBOURNE),
            )
            (daily_root / ".daily_system_snapshot_20260711_000000.tmp").mkdir()
            (daily_root / ".daily_system_snapshot_20260711_000000.tmp" / "manifest.json").write_text(
                json.dumps({"created_at": "should not appear"}),
                encoding="utf-8",
            )
            with (hourly_root / "snapshot.log").open("a", encoding="utf-8") as handle:
                handle.write("{bad json\n")
                handle.write(json.dumps({"event": "failed_hourly_snapshot", "logged_at": "2026-07-11T11:00:00+10:00", "error_message": "disk full"}, ensure_ascii=False) + "\n")
            with (daily_root / "snapshot.log").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"event": "rotated_daily_snapshot", "logged_at": "2026-07-12T00:00:00+10:00", "file_name": "daily_system_snapshot_20260710_233000.zip"}, ensure_ascii=False) + "\n")
                handle.write(json.dumps({"event": "created_daily_snapshot"}, ensure_ascii=False) + "\n")

            status = system_snapshot_manifest.build_backup_control_status(root)

            summary = status["visual_summary"]
            self.assertEqual(summary["last_hourly_snapshot"]["created_at"], "2026-07-11T10:00:00+10:00")
            self.assertEqual(summary["last_daily_snapshot"]["file_name"], "daily_system_snapshot_20260711_233000.zip")
            self.assertEqual(summary["hourly_slot_count"], 6)
            self.assertEqual(summary["daily_archive_count"], 1)
            self.assertGreater(summary["latest_database_backup_size"], 0)
            self.assertGreater(summary["latest_code_snapshot_size"], 0)
            self.assertGreater(summary["latest_total_or_zip_size"], 0)
            self.assertFalse(summary["launchd_installed"])
            self.assertEqual(summary["launchd_loaded"], "not verified by this page")
            self.assertFalse(summary["auto_backup_enabled"])
            self.assertIn("until launchd is installed and loaded", summary["auto_backup_note"])

            logs = status["snapshot_logs"]
            self.assertLessEqual(len(logs), 10)
            self.assertTrue(any(item["snapshot_type"] == "hourly" and item["result"] == "failed" and item["error_message"] == "disk full" for item in logs))
            self.assertTrue(any(item["snapshot_type"] == "daily" and item["result"] == "success" for item in logs))
            self.assertFalse(any("bad json" in item.get("error_message", "") for item in logs))
            created_log = next(item for item in logs if item["event"] == "created_daily_snapshot")
            quick_check_log = next(item for item in logs if item["event"] == "database_quick_check_ok")
            self.assertTrue(created_log["size_available"])
            self.assertFalse(quick_check_log["size_available"])

            archive_status = status["daily_archive_status"]
            self.assertEqual(archive_status["archive_count"], 1)
            self.assertEqual(archive_status["max_archives"], 7)
            self.assertEqual(archive_status["latest_archive"], "daily_system_snapshot_20260711_233000.zip")
            self.assertEqual(archive_status["oldest_archive"], "daily_system_snapshot_20260711_233000.zip")
            self.assertTrue(archive_status["rotation_triggered"])
            self.assertIn("after a new Daily Snapshot is successfully created", archive_status["retention_note"])
            self.assertFalse(any(".tmp" in item["file_name"] for item in status["daily_snapshots"]))

    def test_launchd_backup_control_builds_hourly_and_daily_plists_and_status(self):
        from tools import launchd_backup_control

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            home = Path(folder) / "home"
            self.init_project(root)
            hourly_root = root / "backups" / "hourly-system-snapshots" / "hourly_2"
            hourly_root.mkdir(parents=True)
            (hourly_root / "manifest.json").write_text(
                json.dumps({
                    "backup_type": "hourly",
                    "created_at": "2026-07-11T19:00:00+10:00",
                    "slot_name": "hourly_2",
                    "success": True,
                }),
                encoding="utf-8",
            )
            daily_root = root / "backups" / "daily-system-snapshots"
            daily_root.mkdir(parents=True)
            daily_zip = daily_root / "daily_system_snapshot_20260711_213000.zip"
            daily_zip.write_text("zip", encoding="utf-8")
            daily_zip.with_name(daily_zip.stem + ".manifest.json").write_text(
                json.dumps({
                    "backup_type": "daily",
                    "created_at": "2026-07-11T21:30:00+10:00",
                    "success": True,
                }),
                encoding="utf-8",
            )
            log_dir = home / "Library" / "Logs" / "AAIECRM"
            log_dir.mkdir(parents=True)
            (log_dir / "hourly.launchd.out.log").write_text(
                "created_hourly_snapshot hourly_2\nHOURLY_SNAPSHOT_SKIPPED=skipped_outside_active_window\n",
                encoding="utf-8",
            )
            (log_dir / "hourly.launchd.err.log").write_text(
                "/Library/Developer/CommandLineTools/usr/bin/python3: can't open file 'tools/hourly_system_snapshot.py': [Errno 1] Operation not permitted\n",
                encoding="utf-8",
            )

            calls = []

            def fake_runner(args, **kwargs):
                calls.append(list(args))
                text = "runs = 3\nlast exit code = 0\n"
                return subprocess.CompletedProcess(args, 0, stdout=text, stderr="")

            hourly_plist = launchd_backup_control.build_launchd_plist(root, "hourly", home=home)
            daily_plist = launchd_backup_control.build_launchd_plist(root, "daily", home=home)
            status = launchd_backup_control.build_auto_backup_status(root, home=home, runner=fake_runner)

            self.assertIn("com.aaie.crm.hourly-system-snapshot", hourly_plist)
            self.assertIn("<key>StartInterval</key>", hourly_plist)
            self.assertIn("<integer>3600</integer>", hourly_plist)
            self.assertIn('/usr/bin/python3 tools/hourly_system_snapshot.py', hourly_plist)
            self.assertIn(str(home / "Library" / "Logs" / "AAIECRM" / "hourly.launchd.out.log"), hourly_plist)
            self.assertIn("com.aaie.crm.daily-system-snapshot", daily_plist)
            self.assertIn("<key>StartCalendarInterval</key>", daily_plist)
            self.assertIn("<key>Hour</key><integer>21</integer>", daily_plist)
            self.assertIn("<key>Minute</key><integer>30</integer>", daily_plist)
            self.assertIn('/usr/bin/python3 tools/daily_system_snapshot.py', daily_plist)
            self.assertTrue(status["hourly"]["launchd_loaded"])
            self.assertEqual(status["hourly"]["last_exit_code"], "0")
            self.assertEqual(status["hourly"]["runs"], "3")
            self.assertEqual(status["hourly"]["last_run_result"], "success")
            self.assertIn("created_hourly_snapshot", status["hourly"]["latest_success_line"])
            self.assertNotIn("skipped_outside_active_window", status["hourly"]["latest_success_line"])
            self.assertIn("skipped_outside_active_window", status["hourly"]["latest_skipped_line"])
            self.assertEqual(status["hourly"]["last_scheduled_run"], "skipped outside active window")
            self.assertIn("Operation not permitted", status["hourly"]["latest_error_line"])
            self.assertEqual(status["hourly"]["latest_snapshot"]["created_at"], "2026-07-11T19:00:00+10:00")
            self.assertEqual(status["daily"]["latest_snapshot"]["created_at"], "2026-07-11T21:30:00+10:00")
            self.assertTrue(any("launchctl" in call[0] and "print" in call for call in calls))

    def test_launchd_backup_control_requires_confirmation_and_never_deletes_backups(self):
        from tools import launchd_backup_control

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            home = Path(folder) / "home"
            self.init_project(root)
            launch_agents = home / "Library" / "LaunchAgents"
            backup_file = root / "backups" / "hourly-system-snapshots" / "hourly_0" / "manifest.json"
            backup_file.parent.mkdir(parents=True)
            backup_file.write_text("keep", encoding="utf-8")
            calls = []

            def fake_runner(args, **kwargs):
                calls.append(list(args))
                if args[0] == "plutil":
                    return subprocess.CompletedProcess(args, 0, stdout="OK", stderr="")
                return subprocess.CompletedProcess(args, 0, stdout="runs = 1\nlast exit code = 0\n", stderr="")

            wrong = launchd_backup_control.control_auto_backup(
                root,
                "hourly",
                "enable",
                confirmation_text="ENABLE",
                operator="tester",
                home=home,
                runner=fake_runner,
            )
            self.assertFalse(wrong["success"])
            self.assertFalse(calls)
            self.assertFalse((launch_agents / "com.aaie.crm.hourly-system-snapshot.plist").exists())
            self.assertTrue(backup_file.exists())

            enabled = launchd_backup_control.control_auto_backup(
                root,
                "hourly",
                "enable",
                confirmation_text="ENABLE HOURLY BACKUP",
                operator="tester",
                home=home,
                runner=fake_runner,
            )
            self.assertTrue(enabled["success"])
            self.assertTrue((launch_agents / "com.aaie.crm.hourly-system-snapshot.plist").is_file())
            self.assertTrue(any(call[:2] == ["launchctl", "bootstrap"] for call in calls))
            self.assertTrue(backup_file.exists())

            kicked = launchd_backup_control.control_auto_backup(
                root,
                "daily",
                "kickstart",
                confirmation_text="RUN DAILY BACKUP NOW",
                operator="tester",
                home=home,
                runner=fake_runner,
            )
            disabled = launchd_backup_control.control_auto_backup(
                root,
                "hourly",
                "disable",
                confirmation_text="DISABLE HOURLY BACKUP",
                operator="tester",
                home=home,
                runner=fake_runner,
            )

            self.assertTrue(kicked["success"])
            self.assertTrue(disabled["success"])
            self.assertTrue(any(call[:2] == ["launchctl", "kickstart"] for call in calls))
            self.assertTrue(any(call[:2] == ["launchctl", "bootout"] for call in calls))
            self.assertTrue(backup_file.exists())
            log_text = (root / "logs" / "system-snapshots" / "launchd_control.log").read_text(encoding="utf-8")
            self.assertIn("enabled_auto_backup", log_text)
            self.assertIn("kickstarted_auto_backup", log_text)
            self.assertIn("disabled_auto_backup", log_text)


if __name__ == "__main__":
    unittest.main()
