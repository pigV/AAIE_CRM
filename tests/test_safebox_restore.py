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
from app import can_access_module
from database import Database
from tools.safebox_start import ensure_safebox_table
from tools import safebox_restore_backup, safebox_restore_check, safebox_restore_plan


class SafeboxRestoreTests(unittest.TestCase):
    def run_git(self, repo, *args):
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    def init_repo(self, repo):
        repo.mkdir(parents=True, exist_ok=True)
        self.run_git(repo, "init")
        self.run_git(repo, "config", "user.email", "codex@example.test")
        self.run_git(repo, "config", "user.name", "Codex Test")
        (repo / "README.md").write_text("initial\n", encoding="utf-8")
        self.run_git(repo, "add", "README.md")
        self.run_git(repo, "commit", "-m", "initial stable")
        stable_commit = self.run_git(repo, "rev-parse", "HEAD")
        self.run_git(repo, "tag", "stable/test-restore")
        (repo / "database.py").write_text("# schema change\n", encoding="utf-8")
        (repo / "app.py").write_text("# safebox ui change\n", encoding="utf-8")
        self.run_git(repo, "add", "database.py", "app.py")
        self.run_git(repo, "commit", "-m", "risky change")
        current_commit = self.run_git(repo, "rev-parse", "HEAD")
        return stable_commit, current_commit

    def create_db(self, db_path, before_commit, after_commit, modified_files=None):
        conn = sqlite3.connect(db_path)
        try:
            ensure_safebox_table(conn)
            conn.execute(
                """INSERT INTO safebox_records(
                   feature_name,safebox_name,status,branch_name,before_commit,after_commit,
                   modified_files,database_changed,test_result,record_source,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
                (
                    "SAFEBOX Restore v3",
                    "restore-plan-record",
                    "有风险",
                    "safebox/test-restore",
                    before_commit,
                    after_commit,
                    "app.py\ndatabase.py" if modified_files is None else modified_files,
                    1,
                    "PASS",
                    "自动",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def record(self, db_path, record_id=1):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute("SELECT * FROM safebox_records WHERE id=?", (record_id,)).fetchone()
        finally:
            conn.close()

    def test_restore_plan_generates_read_only_plan_and_codex_instruction(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "repo"
            db_path = Path(folder) / "aaie_crm.db"
            before_commit, after_commit = self.init_repo(root)
            self.create_db(db_path, before_commit, after_commit)

            plan = safebox_restore_plan.build_restore_plan(root, db_path, 1)

            self.assertEqual(plan["record_id"], 1)
            self.assertEqual(plan["target_commit"], before_commit)
            self.assertEqual(plan["current_commit"], after_commit)
            self.assertEqual(plan["restore_modes"][0], "只生成恢复指令")
            self.assertIn("代码 + schema 兼容恢复", plan["restore_modes"])
            self.assertEqual(plan["database_risk"], "高")
            self.assertEqual(plan["schema_risk"], "高")
            self.assertIn("database.py", plan["files_to_review"])
            self.assertIn("合同", "\n".join(plan["protected_modules"]))
            self.assertIn("不执行 git reset", "\n".join(plan["forbidden_actions"]))
            self.assertIn("请按 SAFEBOX Restore v3 流程", plan["codex_instruction"])

    def test_v3_restore_instruction_uses_current_git_status_when_record_files_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "repo"
            db_path = Path(folder) / "aaie_crm.db"
            before_commit, _after_commit = self.init_repo(root)
            self.run_git(root, "tag", "backup-before-dirty")
            (root / "README.md").write_text("stash me\n", encoding="utf-8")
            self.run_git(root, "stash", "push", "-m", "test stash for restore instruction")
            (root / "app.py").write_text("# dirty app\n", encoding="utf-8")
            (root / "tools").mkdir(exist_ok=True)
            (root / "tools" / "safebox_restore_plan.py").write_text("# untracked\n", encoding="utf-8")
            self.create_db(db_path, before_commit, "", modified_files="")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "UPDATE safebox_records SET backup_dir=?,database_risk=?,schema_change_risk=? WHERE id=1",
                    (
                        "backups/safe-box/20260708_before_restore_1",
                        "是：database.py",
                        "可能有数据库 schema 变更；如果只新增独立表，需要人工确认是否影响核心表。",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            instruction = safebox_restore_plan.build_v3_restore_instruction(root, db_path, 1)

            self.assertIn("当前分支：", instruction)
            self.assertIn(f"修改前 commit：{before_commit}", instruction)
            self.assertIn("修改后 commit：未提交", instruction)
            self.assertIn("回滚前备份路径：backups/safe-box/20260708_before_restore_1", instruction)
            self.assertIn("当前 git status 文件列表：", instruction)
            self.assertIn("app.py", instruction)
            self.assertIn("tools/safebox_restore_plan.py", instruction)
            self.assertIn("数据库是否变更：是", instruction)
            self.assertIn("database_risk：高", instruction)
            self.assertIn("schema_risk：高", instruction)
            self.assertIn("恢复风险等级：高", instruction)
            self.assertIn("当前工作区未 clean，必须先人工检查未提交文件，不要直接回滚。", instruction)
            self.assertIn("检测到 stash 存在，不要自动恢复 stash，需人工确认。", instruction)
            self.assertIn("不执行 git reset、不执行恢复用途 git checkout、不恢复数据库、不删除文件、不覆盖上传文件", instruction)

    def test_restore_check_detects_dirty_worktree_missing_target_and_schema_risk(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "repo"
            db_path = Path(folder) / "aaie_crm.db"
            upload_dir = Path(folder) / "upload_staging"
            upload_dir.mkdir()
            before_commit, after_commit = self.init_repo(root)
            self.create_db(db_path, "deadbeef", after_commit, "database.py\nstudent_doc.py")
            (root / "README.md").write_text("dirty\n", encoding="utf-8")

            result = safebox_restore_check.run_restore_check(root, db_path, 1, upload_dirs=[upload_dir])

            self.assertFalse(result["git_status_clean"])
            self.assertFalse(result["target_exists"])
            self.assertEqual(result["quick_check"], "ok")
            self.assertTrue(result["upload_dirs"][0]["accessible"])
            self.assertTrue(result["schema_risk"])
            self.assertTrue(result["database_risk"])
            self.assertIn("student_doc.py", result["core_business_risk_files"])
            self.assertIn(result["risk_level"], {"高", "禁止自动恢复"})

    def test_restore_check_uses_git_status_files_for_schema_risk(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "repo"
            db_path = Path(folder) / "aaie_crm.db"
            upload_dir = Path(folder) / "upload_staging"
            upload_dir.mkdir()
            before_commit, after_commit = self.init_repo(root)
            self.create_db(db_path, before_commit, after_commit, modified_files="")
            (root / "database.py").write_text("# dirty schema file\n", encoding="utf-8")

            result = safebox_restore_check.run_restore_check(root, db_path, 1, upload_dirs=[upload_dir])

            self.assertIn("database.py", result["git_status_files"])
            self.assertTrue(result["schema_risk"])
            self.assertTrue(result["database_risk"])
            self.assertIn("database.py", result["schema_risk_files"])
            self.assertIn("git status 包含数据库 schema 文件", "\n".join(result["risk_reasons"]))

    def test_restore_backup_creates_backup_without_copying_upload_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "repo"
            db_path = Path(folder) / "aaie_crm.db"
            backup_root = Path(folder) / "backups" / "safe-box"
            upload_dir = Path(folder) / "upload_staging"
            upload_dir.mkdir()
            (upload_dir / "student-upload.pdf").write_text("do not copy", encoding="utf-8")
            before_commit, after_commit = self.init_repo(root)
            self.create_db(db_path, before_commit, after_commit)
            (root / "database.py").write_text("# dirty schema before backup\n", encoding="utf-8")

            result = safebox_restore_backup.create_restore_backup(
                root,
                db_path,
                1,
                backup_root=backup_root,
                upload_dirs=[upload_dir],
                health_check_result="health ok",
            )

            backup_dir = Path(result["backup_dir"])
            self.assertTrue(backup_dir.is_dir())
            for filename in (
                "git_context.json",
                "git_status.txt",
                "current.diff",
                "safebox_records.json",
                "database_backup.sqlite3",
                "schema.sql",
                "upload_inventory.txt",
                "restore_target.json",
                "restore_plan.json",
                "restore_check.json",
                "health_check.txt",
            ):
                self.assertTrue((backup_dir / filename).exists(), filename)
            restore_plan = json.loads((backup_dir / "restore_plan.json").read_text(encoding="utf-8"))
            restore_check = json.loads((backup_dir / "restore_check.json").read_text(encoding="utf-8"))
            self.assertEqual(restore_plan["record_id"], 1)
            self.assertEqual(restore_plan["safebox_name"], "restore-plan-record")
            self.assertEqual(restore_plan["after_commit_for_restore"], after_commit)
            self.assertEqual(restore_plan["backup_dir"], str(backup_dir))
            self.assertIn("database.py", restore_plan["current_git_status_files"])
            self.assertIn("不执行 git reset", "\n".join(restore_plan["forbidden_actions"]))
            self.assertEqual(restore_check["record_id"], 1)
            self.assertEqual(restore_check["quick_check"], "ok")
            self.assertIn("database.py", restore_check["schema_risk_files"])
            self.assertIn("git status 包含数据库 schema 文件", "\n".join(restore_check["risk_reasons"]))
            inventory = (backup_dir / "upload_inventory.txt").read_text(encoding="utf-8")
            self.assertIn("student-upload.pdf", inventory)
            copied_uploads = [path for path in backup_dir.rglob("student-upload.pdf")]
            self.assertEqual(copied_uploads, [])

    def test_safebox_detail_shows_restore_v3_read_only_actions(self):
        old_db = app_module.db
        with tempfile.TemporaryDirectory() as folder:
            try:
                db = Database(Path(folder) / "aaie_crm.db")
                db.initialize()
                app_module.db = db
                admin = dict(db.one("SELECT u.*,r.name role_name,r.permissions_json FROM users u JOIN roles r ON r.id=u.role_id WHERE username='admin'"))
                admin["module_permissions_json"] = app_module.modules_json_for_user(admin["id"])
                self.assertTrue(can_access_module(admin, "safebox_centre"))
                record_id = db.execute(
                    """INSERT INTO safebox_records(
                       feature_name,safebox_name,status,branch_name,before_commit,after_commit,
                       database_changed,modified_files,record_source,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
                    (
                        "SAFEBOX Restore v3",
                        "restore-ui-test",
                        "有风险",
                        "safebox/safebox-restore-v3-plan-check-backup",
                        "before123",
                        "after456",
                        1,
                        "app.py\ndatabase.py",
                        "自动",
                    ),
                )

                handler = object.__new__(app_module.CRMHandler)
                handler.user = admin
                handler.csrf = "csrf-token"
                handler.query = {}
                handler.client_address = ("127.0.0.1", 12345)
                handler.headers = {}
                captured = {}
                handler.send_html = lambda html, status=200, headers=None: captured.update({"html": html, "status": status})
                handler.error_page = lambda message, status=400: captured.update({"error": message, "status": status})

                handler.safebox_detail("GET", str(record_id))
                html = captured["html"]
                self.assertIn("SAFEBOX Restore v3", html)
                self.assertIn("恢复风险等级", html)
                self.assertIn(f"/safebox/{record_id}/restore/plan", html)
                self.assertIn(f"/safebox/{record_id}/restore/check", html)
                self.assertIn(f"/safebox/{record_id}/restore/backup", html)
                self.assertIn("复制 v3 恢复指令", html)
                self.assertIn("执行代码恢复（禁用）", html)
                self.assertIn("执行数据库恢复（禁用）", html)
                self.assertIn("disabled", html)
                self.assertIn("当前 git status 文件列表", html)
                current_status = subprocess.run(
                    ["git", "status", "--short"],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.splitlines()
                current_changed_files = []
                for line in current_status:
                    path = line[3:].strip()
                    if " -> " in path:
                        path = path.rsplit(" -> ", 1)[-1].strip()
                    if path:
                        current_changed_files.append(path)
                self.assertTrue(current_changed_files, "expected at least one current git status file for restore instruction check")
                self.assertTrue(
                    any(path in html for path in current_changed_files),
                    f"expected restore instruction to include one current git status file from {current_changed_files!r}",
                )
                self.assertNotIn("需要检查的文件：&#x27;\\n未记录", html)
            finally:
                app_module.db = old_db

    def test_safebox_restore_routes_render_plan_and_check_pages(self):
        old_db = app_module.db
        with tempfile.TemporaryDirectory() as folder:
            try:
                db = Database(Path(folder) / "aaie_crm.db")
                db.initialize()
                app_module.db = db
                admin = dict(db.one("SELECT u.*,r.name role_name,r.permissions_json FROM users u JOIN roles r ON r.id=u.role_id WHERE username='admin'"))
                admin["module_permissions_json"] = app_module.modules_json_for_user(admin["id"])
                record_id = db.execute(
                    """INSERT INTO safebox_records(
                       feature_name,safebox_name,status,branch_name,before_commit,after_commit,
                       database_changed,modified_files,record_source,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
                    (
                        "SAFEBOX Restore v3",
                        "restore-route-test",
                        "有风险",
                        "safebox/safebox-restore-v3-plan-check-backup",
                        "HEAD",
                        "HEAD",
                        1,
                        "database.py",
                        "自动",
                    ),
                )
                handler = object.__new__(app_module.CRMHandler)
                handler.user = admin
                handler.csrf = "csrf-token"
                handler.query = {}
                handler.client_address = ("127.0.0.1", 12345)
                handler.headers = {}
                captured = {}
                handler.send_html = lambda html, status=200, headers=None: captured.update({"html": html, "status": status})
                handler.redirect = lambda location, cookie=None: captured.update({"redirect": location})
                handler.flash_redirect = lambda location, message, kind="success": captured.update({"redirect": location, "message": message, "kind": kind})
                handler.error_page = lambda message, status=400: captured.update({"error": message, "status": status})

                handler.safebox_restore_plan("GET", str(record_id))
                self.assertEqual(captured["status"], 200)
                self.assertIn("恢复计划", captured["html"])
                self.assertIn("不执行 git reset", captured["html"])

                handler.safebox_restore_check("GET", str(record_id))
                self.assertEqual(captured["status"], 200)
                self.assertIn("恢复前检查", captured["html"])
                self.assertIn("PRAGMA quick_check", captured["html"])
            finally:
                app_module.db = old_db


if __name__ == "__main__":
    unittest.main()
