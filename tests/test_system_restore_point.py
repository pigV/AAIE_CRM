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

import app as app_module
from database import Database
from tools import system_restore_point


class SystemRestorePointTests(unittest.TestCase):
    def run_git(self, repo, *args):
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    def init_project(self, root):
        root.mkdir(parents=True, exist_ok=True)
        (root / "assets").mkdir()
        (root / "tools").mkdir()
        (root / "tests").mkdir()
        (root / "docs").mkdir()
        (root / "templates" / "contracts").mkdir(parents=True)
        (root / "database").mkdir()
        (root / "upload_staging").mkdir()
        (root / "secure_assets").mkdir()
        (root / "reports" / "school_plan_reports").mkdir(parents=True)
        (root / ".git").mkdir()
        (root / "__pycache__").mkdir()
        (root / "logs").mkdir()

        (root / "app.py").write_text("print('app v1')\n", encoding="utf-8")
        (root / "ui.py").write_text("NAV='ui'\n", encoding="utf-8")
        (root / "database.py").write_text("SCHEMA='v1'\n", encoding="utf-8")
        (root / "assistant_readonly.py").write_text("ASSISTANT=True\n", encoding="utf-8")
        (root / "assets" / "style.css").write_text("body { color: #123; }\n", encoding="utf-8")
        (root / "assets" / "app.js").write_text("console.log('crm');\n", encoding="utf-8")
        (root / "tools" / "safebox_start.py").write_text("print('safe')\n", encoding="utf-8")
        (root / "tests" / "test_core.py").write_text("def test_placeholder(): pass\n", encoding="utf-8")
        (root / "docs" / "CRM_SAFE_BOX.md").write_text("# Safe Box\n", encoding="utf-8")
        (root / "templates" / "contracts" / "contract.html").write_text("<p>contract</p>\n", encoding="utf-8")
        (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
        (root / "start.command").write_text("#!/bin/zsh\n", encoding="utf-8")
        (root / ".gitignore").write_text("system_restore_points/\n", encoding="utf-8")

        (root / "upload_staging" / "student-upload.pdf").write_text("student data", encoding="utf-8")
        (root / "secure_assets" / "signature.png").write_text("signature", encoding="utf-8")
        (root / "reports" / "school_plan_reports" / "parent-report.pdf").write_text("report", encoding="utf-8")
        (root / "__pycache__" / "app.pyc").write_bytes(b"cache")
        (root / "logs" / "admin.log").write_text("log", encoding="utf-8")

        db_path = root / "database" / "aaie_crm.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute("CREATE TABLE safebox_records (id INTEGER PRIMARY KEY, feature_name TEXT)")

        self.run_git(root, "init")
        self.run_git(root, "config", "user.email", "codex@example.test")
        self.run_git(root, "config", "user.name", "Codex Test")
        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-m", "initial system")
        return db_path

    def create_restore_point(self, root, db_path, **kwargs):
        return system_restore_point.create_system_restore_point(
            root=root,
            db_path=db_path,
            reason=kwargs.get("reason", "Before system change"),
            task_name=kwargs.get("task_name", "System Restore Point V1"),
            trigger_type=kwargs.get("trigger_type", "manual"),
            expected_files_to_change=kwargs.get("expected_files_to_change", ["app.py", "assets/style.css"]),
            affected_modules=kwargs.get("affected_modules", ["SAFEBOX", "UI"]),
            risk_level=kwargs.get("risk_level", "medium"),
            created_by=kwargs.get("created_by", "Codex"),
            notes=kwargs.get("notes", "test restore point"),
            restore_root=kwargs.get("restore_root", root / "system_restore_points"),
        )

    def test_create_system_restore_point(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)

            point = self.create_restore_point(root, db_path)

            point_dir = Path(point["restore_point_dir"])
            self.assertTrue(point_dir.is_dir())
            self.assertTrue((point_dir / "restore_meta.json").is_file())
            self.assertTrue((point_dir / "code_snapshot").is_dir())
            self.assertFalse(point["actual_restore_allowed"])

    def test_restore_point_meta_contains_git_info(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)

            point = self.create_restore_point(root, db_path, reason="Before CSS trial")
            meta = json.loads((Path(point["restore_point_dir"]) / "restore_meta.json").read_text(encoding="utf-8"))

            self.assertEqual(meta["reason"], "Before CSS trial")
            self.assertEqual(meta["git_branch"], self.run_git(root, "branch", "--show-current"))
            self.assertEqual(meta["git_commit"], self.run_git(root, "rev-parse", "HEAD"))
            self.assertTrue(meta["git_status_clean"])
            self.assertEqual(meta["git_status_short"], "")
            self.assertIn("expected_files_to_change", meta)
            self.assertFalse(meta["actual_restore_allowed"])

    def test_restore_point_captures_schema_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)

            point = self.create_restore_point(root, db_path)
            point_dir = Path(point["restore_point_dir"])
            schema_sql = (point_dir / "schema_snapshot.sql").read_text(encoding="utf-8")
            schema_json = json.loads((point_dir / "schema_snapshot.json").read_text(encoding="utf-8"))

            self.assertIn("CREATE TABLE students", schema_sql)
            self.assertIn("students", {row["name"] for row in schema_json})
            self.assertIn("schema_snapshot.sql", point["schema_snapshot_path"])

    def test_restore_point_captures_code_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)

            point = self.create_restore_point(root, db_path)
            manifest = json.loads((Path(point["restore_point_dir"]) / "file_manifest.json").read_text(encoding="utf-8"))
            paths = {item["path"] for item in manifest["files"]}

            self.assertIn("app.py", paths)
            self.assertIn("ui.py", paths)
            self.assertIn("database.py", paths)
            self.assertIn("assets/style.css", paths)
            self.assertIn("assets/app.js", paths)
            self.assertIn("tools/safebox_start.py", paths)
            self.assertIn("templates/contracts/contract.html", paths)
            self.assertTrue((Path(point["restore_point_dir"]) / "code_snapshot" / "app.py").is_file())

    def test_restore_points_list_page(self):
        old_db = app_module.db
        old_app_dir = app_module.APP_DIR
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            self.create_restore_point(root, db_path, task_name="List Page Test")
            try:
                db = Database(Path(folder) / "aaie_crm_app.db")
                db.initialize()
                app_module.db = db
                app_module.APP_DIR = root
                handler = self.make_handler(db)
                captured = self.capture(handler)

                handler.system_restore_points("GET")

                self.assertEqual(captured["status"], 200)
                self.assertIn("System Restore Points", captured["html"])
                self.assertIn("List Page Test", captured["html"])
                self.assertIn("Actual Restore is locked by default.", captured["html"])
                self.assertIn("Locked Restore V2", captured["html"])
            finally:
                app_module.db = old_db
                app_module.APP_DIR = old_app_dir

    def test_restore_point_detail_page(self):
        old_db = app_module.db
        old_app_dir = app_module.APP_DIR
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            point = self.create_restore_point(root, db_path, task_name="Detail Page Test")
            try:
                db = Database(Path(folder) / "aaie_crm_app.db")
                db.initialize()
                app_module.db = db
                app_module.APP_DIR = root
                handler = self.make_handler(db)
                captured = self.capture(handler)

                handler.system_restore_point_detail("GET", point["restore_point_id"])

                self.assertEqual(captured["status"], 200)
                self.assertIn("Detail Page Test", captured["html"])
                self.assertIn("git_commit", captured["html"])
                self.assertIn("Dry-run restore", captured["html"])
                self.assertIn("Actual Restore（禁用）", captured["html"])
                self.assertIn("disabled", captured["html"])
            finally:
                app_module.db = old_db
                app_module.APP_DIR = old_app_dir

    def test_locked_restore_page_shows_default_lock_and_disabled_actual_restore(self):
        old_db = app_module.db
        old_app_dir = app_module.APP_DIR
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            point = self.create_restore_point(root, db_path, task_name="Locked Restore Page Test")
            try:
                db = Database(Path(folder) / "aaie_crm_app.db")
                db.initialize()
                app_module.db = db
                app_module.APP_DIR = root
                handler = self.make_handler(db)
                captured = self.capture(handler)

                handler.system_restore_point_locked_restore("GET", point["restore_point_id"])

                self.assertEqual(captured["status"], 200)
                self.assertIn("Actual Restore is locked by default.", captured["html"])
                self.assertIn("RESTORE SYSTEM FILES", captured["html"])
                self.assertIn("disabled", captured["html"])
                self.assertIn("locked-restore/dry-run", captured["html"])
                self.assertIn("current_before_restore", captured["html"])
            finally:
                app_module.db = old_db
                app_module.APP_DIR = old_app_dir

    def test_restore_dry_run_does_not_modify_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            point = self.create_restore_point(root, db_path)
            app_file = root / "app.py"
            app_file.write_text("print('dirty but must remain')\n", encoding="utf-8")

            result = system_restore_point.dry_run_restore_point(root, point["restore_point_id"])

            self.assertEqual(app_file.read_text(encoding="utf-8"), "print('dirty but must remain')\n")
            self.assertFalse(result["actual_restore_allowed"])
            self.assertIn("app.py", result["possible_overwrite_files"])

    def test_restore_dry_run_reports_affected_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            point = self.create_restore_point(root, db_path, affected_modules=["SAFEBOX", "CSS"])
            (root / "assets" / "style.css").write_text("body { color: red; }\n", encoding="utf-8")

            result = system_restore_point.dry_run_restore_point(root, point["restore_point_id"])

            self.assertEqual(result["target_restore_point_id"], point["restore_point_id"])
            self.assertIn("assets/style.css", result["changed_files_since_restore_point"])
            self.assertEqual(result["affected_modules"], ["SAFEBOX", "CSS"])
            self.assertIn(result["risk_level"], {"low", "medium", "high"})
            self.assertIn("Actual restore is disabled in V1.", result["blocked_reason"])

    def test_actual_restore_disabled_in_v1(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            point = self.create_restore_point(root, db_path)
            detail = system_restore_point.load_restore_point(root, point["restore_point_id"])
            dry_run = system_restore_point.dry_run_restore_point(root, point["restore_point_id"])

            self.assertFalse(detail["restore_meta"]["actual_restore_allowed"])
            self.assertFalse(dry_run["actual_restore_allowed"])
            self.assertNotIn("restore", system_restore_point.available_actions())
            self.assertEqual(system_restore_point.actual_restore_status(), "disabled")

    def test_no_upload_files_in_system_restore_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)

            point = self.create_restore_point(root, db_path)
            point_dir = Path(point["restore_point_dir"])
            manifest = json.loads((point_dir / "file_manifest.json").read_text(encoding="utf-8"))
            manifest_paths = "\n".join(item["path"] for item in manifest["files"])
            snapshot_paths = "\n".join(str(path.relative_to(point_dir)) for path in point_dir.rglob("*"))

            self.assertNotIn("upload_staging/student-upload.pdf", manifest_paths)
            self.assertNotIn("secure_assets/signature.png", manifest_paths)
            self.assertNotIn("reports/school_plan_reports/parent-report.pdf", manifest_paths)
            self.assertNotIn("upload_staging/student-upload.pdf", snapshot_paths)
            self.assertNotIn("secure_assets/signature.png", snapshot_paths)
            self.assertNotIn("reports/school_plan_reports/parent-report.pdf", snapshot_paths)

    def test_actual_restore_locked_by_default(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            point = self.create_restore_point(root, db_path)

            status = system_restore_point.locked_restore_status(root, point["restore_point_id"])

            self.assertFalse(status["actual_restore_allowed"])
            self.assertFalse(status["can_execute"])
            self.assertTrue(status["locked_by_default"])
            self.assertIn("Actual Restore is locked by default.", status["locked_reason"])

    def test_actual_restore_requires_clean_worktree(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            point = self.create_restore_point(root, db_path)
            system_restore_point.locked_restore_dry_run(root, point["restore_point_id"])
            system_restore_point.create_current_before_restore(root, db_path, point["restore_point_id"])
            (root / "app.py").write_text("print('dirty')\n", encoding="utf-8")

            with self.assertRaises(system_restore_point.RestoreSafetyError) as ctx:
                system_restore_point.execute_locked_restore(
                    root,
                    point["restore_point_id"],
                    confirmation_text=system_restore_point.LOCKED_RESTORE_CONFIRMATION_TEXT,
                    second_confirm=True,
                )

            self.assertIn("clean", str(ctx.exception).lower())

    def test_actual_restore_requires_dry_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            point = self.create_restore_point(root, db_path)
            system_restore_point.create_current_before_restore(root, db_path, point["restore_point_id"])

            with self.assertRaises(system_restore_point.RestoreSafetyError) as ctx:
                system_restore_point.execute_locked_restore(
                    root,
                    point["restore_point_id"],
                    confirmation_text=system_restore_point.LOCKED_RESTORE_CONFIRMATION_TEXT,
                    second_confirm=True,
                )

            self.assertIn("dry-run", str(ctx.exception))

    def test_actual_restore_requires_current_before_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            point = self.create_restore_point(root, db_path)
            system_restore_point.locked_restore_dry_run(root, point["restore_point_id"])

            with self.assertRaises(system_restore_point.RestoreSafetyError) as ctx:
                system_restore_point.execute_locked_restore(
                    root,
                    point["restore_point_id"],
                    confirmation_text=system_restore_point.LOCKED_RESTORE_CONFIRMATION_TEXT,
                    second_confirm=True,
                )

            self.assertIn("current_before_restore", str(ctx.exception))

    def test_actual_restore_requires_confirmation_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            point = self.create_restore_point(root, db_path)
            system_restore_point.locked_restore_dry_run(root, point["restore_point_id"])
            system_restore_point.create_current_before_restore(root, db_path, point["restore_point_id"])

            with self.assertRaises(system_restore_point.RestoreSafetyError) as ctx:
                system_restore_point.execute_locked_restore(
                    root,
                    point["restore_point_id"],
                    confirmation_text="RESTORE",
                    second_confirm=True,
                )

            self.assertIn(system_restore_point.LOCKED_RESTORE_CONFIRMATION_TEXT, str(ctx.exception))

    def test_actual_restore_creates_pre_restore_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            point = self.create_restore_point(root, db_path)

            backup = system_restore_point.create_current_before_restore(root, db_path, point["restore_point_id"])

            backup_dir = Path(backup["current_before_restore_dir"])
            self.assertTrue(backup_dir.is_dir())
            self.assertTrue((backup_dir / "restore_meta.json").is_file())
            self.assertEqual(backup["target_restore_point_id"], point["restore_point_id"])

    def test_actual_restore_only_restores_allowed_system_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            point = self.create_restore_point(root, db_path)
            (root / "app.py").write_text("print('app v2')\n", encoding="utf-8")
            self.run_git(root, "add", "app.py")
            self.run_git(root, "commit", "-m", "app v2")
            system_restore_point.locked_restore_dry_run(root, point["restore_point_id"])
            system_restore_point.create_current_before_restore(root, db_path, point["restore_point_id"])

            result = system_restore_point.execute_locked_restore(
                root,
                point["restore_point_id"],
                confirmation_text=system_restore_point.LOCKED_RESTORE_CONFIRMATION_TEXT,
                second_confirm=True,
                health_check_fn=lambda: {"status": "ok"},
                test_runner=lambda: {"status": "passed"},
            )

            self.assertEqual(result["status"], "success")
            self.assertEqual((root / "app.py").read_text(encoding="utf-8"), "print('app v1')\n")
            self.assertIn("app.py", result["restored_files"])

    def test_actual_restore_refuses_database_files(self):
        self.assertFalse(system_restore_point.is_restorable_system_file("database/aaie_crm.db"))
        self.assertFalse(system_restore_point.is_restorable_system_file("database/aaie_crm.sqlite3"))
        self.assertFalse(system_restore_point.is_restorable_system_file("database.py"))

    def test_actual_restore_refuses_upload_files(self):
        self.assertFalse(system_restore_point.is_restorable_system_file("uploads/student.pdf"))
        self.assertFalse(system_restore_point.is_restorable_system_file("upload_staging/student.pdf"))
        self.assertFalse(system_restore_point.is_restorable_system_file("secure_assets/signature.png"))
        self.assertFalse(system_restore_point.is_restorable_system_file("reports/school_plan_reports/final.pdf"))

    def test_actual_restore_writes_restore_log(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            point = self.create_restore_point(root, db_path)
            (root / "app.py").write_text("print('app v2')\n", encoding="utf-8")
            self.run_git(root, "add", "app.py")
            self.run_git(root, "commit", "-m", "app v2")
            system_restore_point.locked_restore_dry_run(root, point["restore_point_id"])
            system_restore_point.create_current_before_restore(root, db_path, point["restore_point_id"])

            result = system_restore_point.execute_locked_restore(
                root,
                point["restore_point_id"],
                confirmation_text=system_restore_point.LOCKED_RESTORE_CONFIRMATION_TEXT,
                second_confirm=True,
                health_check_fn=lambda: {"status": "ok"},
                test_runner=lambda: {"status": "passed"},
            )

            log = json.loads(Path(result["restore_log_path"]).read_text(encoding="utf-8"))
            self.assertEqual(log["status"], "success")
            self.assertEqual(log["target_restore_point_id"], point["restore_point_id"])
            self.assertEqual(log["health_check_result"]["status"], "ok")
            self.assertEqual(log["test_result"]["status"], "passed")

    def test_actual_restore_does_not_use_git_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            point = self.create_restore_point(root, db_path)
            (root / "app.py").write_text("print('app v2')\n", encoding="utf-8")
            self.run_git(root, "add", "app.py")
            self.run_git(root, "commit", "-m", "app v2")
            system_restore_point.locked_restore_dry_run(root, point["restore_point_id"])
            system_restore_point.create_current_before_restore(root, db_path, point["restore_point_id"])
            original_run = system_restore_point.subprocess.run

            def guarded_run(args, *run_args, **run_kwargs):
                if args[:2] == ["git", "reset"] or args[:2] == ["git", "checkout"]:
                    self.fail(f"dangerous git command used: {' '.join(args)}")
                return original_run(args, *run_args, **run_kwargs)

            system_restore_point.subprocess.run = guarded_run
            try:
                system_restore_point.execute_locked_restore(
                    root,
                    point["restore_point_id"],
                    confirmation_text=system_restore_point.LOCKED_RESTORE_CONFIRMATION_TEXT,
                    second_confirm=True,
                    health_check_fn=lambda: {"status": "ok"},
                    test_runner=lambda: {"status": "passed"},
                )
            finally:
                system_restore_point.subprocess.run = original_run

    def test_actual_restore_does_not_apply_stash(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            point = self.create_restore_point(root, db_path)
            (root / "app.py").write_text("print('app v2')\n", encoding="utf-8")
            self.run_git(root, "add", "app.py")
            self.run_git(root, "commit", "-m", "app v2")
            system_restore_point.locked_restore_dry_run(root, point["restore_point_id"])
            system_restore_point.create_current_before_restore(root, db_path, point["restore_point_id"])
            original_run = system_restore_point.subprocess.run

            def guarded_run(args, *run_args, **run_kwargs):
                if args[:3] in (["git", "stash", "apply"], ["git", "stash", "pop"]):
                    self.fail(f"dangerous stash command used: {' '.join(args)}")
                return original_run(args, *run_args, **run_kwargs)

            system_restore_point.subprocess.run = guarded_run
            try:
                system_restore_point.execute_locked_restore(
                    root,
                    point["restore_point_id"],
                    confirmation_text=system_restore_point.LOCKED_RESTORE_CONFIRMATION_TEXT,
                    second_confirm=True,
                    health_check_fn=lambda: {"status": "ok"},
                    test_runner=lambda: {"status": "passed"},
                )
            finally:
                system_restore_point.subprocess.run = original_run

    def test_restore_failure_preserves_current_before_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "crm"
            db_path = self.init_project(root)
            point = self.create_restore_point(root, db_path)
            (root / "app.py").write_text("print('app v2')\n", encoding="utf-8")
            self.run_git(root, "add", "app.py")
            self.run_git(root, "commit", "-m", "app v2")
            system_restore_point.locked_restore_dry_run(root, point["restore_point_id"])
            backup = system_restore_point.create_current_before_restore(root, db_path, point["restore_point_id"])
            detail = system_restore_point.load_restore_point(root, point["restore_point_id"])
            (Path(detail["restore_point_dir"]) / "code_snapshot" / "app.py").unlink()

            with self.assertRaises(system_restore_point.RestoreExecutionError):
                system_restore_point.execute_locked_restore(
                    root,
                    point["restore_point_id"],
                    confirmation_text=system_restore_point.LOCKED_RESTORE_CONFIRMATION_TEXT,
                    second_confirm=True,
                    health_check_fn=lambda: {"status": "ok"},
                    test_runner=lambda: {"status": "passed"},
                )

            self.assertTrue(Path(backup["current_before_restore_dir"]).is_dir())

    def make_handler(self, db):
        admin = dict(db.one("SELECT u.*,r.name role_name,r.permissions_json FROM users u JOIN roles r ON r.id=u.role_id WHERE username='admin'"))
        admin["module_permissions_json"] = app_module.modules_json_for_user(admin["id"])
        handler = object.__new__(app_module.CRMHandler)
        handler.user = admin
        handler.csrf = "csrf-token"
        handler.query = {}
        handler.form = {}
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {}
        return handler

    def capture(self, handler):
        captured = {}
        handler.send_html = lambda html, status=200, headers=None: captured.update({"html": html, "status": status})
        handler.error_page = lambda message, status=400: captured.update({"error": message, "status": status})
        handler.redirect = lambda location, cookie=None: captured.update({"redirect": location})
        handler.flash_redirect = lambda location, message, kind="success": captured.update({"redirect": location, "message": message, "kind": kind})
        return captured


if __name__ == "__main__":
    unittest.main()
