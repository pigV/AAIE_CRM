import io
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as app_module
from app import can_access_module
from database import Database
from tools import safebox_finish, safebox_start


class SafeboxAutoLoggerTests(unittest.TestCase):
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
        self.run_git(repo, "commit", "-m", "initial")
        self.run_git(repo, "checkout", "-b", "safebox/test-auto")

    def run_start(self, repo, db_path, *extra_args):
        output = io.StringIO()
        with redirect_stdout(output):
            safebox_start.main(
                [
                    "--root",
                    str(repo),
                    "--db-path",
                    str(db_path),
                    "--feature-name",
                    "SAFEBOX Auto Logger v2",
                    "--safebox-name",
                    "safebox-auto-logger-v2-test",
                    "--change-goal",
                    "自动创建 SAFEBOX 开发记录",
                    "--allowed-scope",
                    "只允许 SAFEBOX 相关文件",
                    "--forbidden-scope",
                    "不执行 git reset；不恢复数据库；不删除文件",
                    *extra_args,
                ]
            )
        return output.getvalue()

    def run_finish(self, repo, db_path, record_id, *extra_args):
        output = io.StringIO()
        with redirect_stdout(output):
            safebox_finish.main(
                [
                    "--root",
                    str(repo),
                    "--db-path",
                    str(db_path),
                    "--record-id",
                    str(record_id),
                    "--test-command",
                    "python -m unittest tests.test_safebox_auto_logger",
                    "--test-result",
                    "PASS",
                    "--health-check-result",
                    "/health ok",
                    *extra_args,
                ]
            )
        return output.getvalue()

    def run_finish_without_health_arg(self, repo, db_path, record_id):
        output = io.StringIO()
        with redirect_stdout(output):
            safebox_finish.main(
                [
                    "--root",
                    str(repo),
                    "--db-path",
                    str(db_path),
                    "--record-id",
                    str(record_id),
                    "--test-result",
                    "PASS",
                ]
            )
        return output.getvalue()

    def record(self, db_path, record_id=1):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute("SELECT * FROM safebox_records WHERE id=?", (record_id,)).fetchone()
        finally:
            conn.close()

    def test_safebox_start_creates_auto_record_with_git_context(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo = root / "repo"
            db_path = root / "aaie_crm.db"
            self.init_repo(repo)
            before_commit = self.run_git(repo, "rev-parse", "HEAD")

            output = self.run_start(repo, db_path)

            self.assertIn("SAFEBOX_RECORD_ID=1", output)
            record = self.record(db_path)
            self.assertEqual(record["feature_name"], "SAFEBOX Auto Logger v2")
            self.assertEqual(record["safebox_name"], "safebox-auto-logger-v2-test")
            self.assertEqual(record["record_source"], "自动")
            self.assertEqual(record["status"], "进行中")
            self.assertEqual(record["branch_name"], "safebox/test-auto")
            self.assertEqual(record["before_commit"], before_commit)
            self.assertEqual(record["git_status_before"], "")
            self.assertEqual(record["stash_present"], 0)
            self.assertEqual(record["workspace_clean_before"], 1)
            self.assertIsNotNone(record["auto_started_at"])
            self.assertIn("自动创建 SAFEBOX 开发记录", record["change_goal"])

    def test_safebox_finish_updates_record_with_diff_risk_and_restore_instruction(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo = root / "repo"
            db_path = root / "aaie_crm.db"
            self.init_repo(repo)
            self.run_start(repo, db_path)
            (repo / "database.py").write_text("# schema extension\n", encoding="utf-8")
            (repo / "tools").mkdir()
            (repo / "tools" / "safebox_start.py").write_text("print('start')\n", encoding="utf-8")
            self.run_git(repo, "add", "database.py", "tools/safebox_start.py")
            self.run_git(repo, "commit", "-m", "feat: safebox auto logger")
            after_commit = self.run_git(repo, "rev-parse", "HEAD")

            output = self.run_finish(repo, db_path, 1)

            self.assertIn("SAFEBOX_RECORD_ID=1", output)
            self.assertIn("SAFEBOX_STATUS=有风险", output)
            record = self.record(db_path)
            self.assertEqual(record["after_commit"], after_commit)
            self.assertEqual(record["workspace_clean_after"], 1)
            self.assertIn("database.py", record["modified_files"])
            self.assertIn("tools/safebox_start.py", record["modified_files"])
            self.assertIn("database.py", record["diff_summary"])
            self.assertIn("是", record["database_risk"])
            self.assertIn("人工确认", record["schema_change_risk"])
            self.assertEqual(record["database_changed"], 1)
            self.assertEqual(record["health_check_result"], "/health ok")
            self.assertIn("修改前 commit", record["restore_instruction"])
            self.assertIn("修改后 commit", record["restore_instruction"])
            self.assertIn("safebox/test-auto", record["restore_instruction"])
            self.assertIn("不执行 git reset", record["restore_instruction"])

    def test_safebox_finish_marks_failed_or_uncommitted_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo = root / "repo"
            db_path = root / "aaie_crm.db"
            self.init_repo(repo)
            self.run_start(repo, db_path)
            output = self.run_finish(repo, db_path, 1, "--test-result", "FAIL: regression failed")
            self.assertIn("SAFEBOX_STATUS=测试失败", output)

            self.run_start(repo, db_path, "--safebox-name", "uncommitted-record")
            (repo / "README.md").write_text("dirty workspace\n", encoding="utf-8")
            output = self.run_finish(repo, db_path, 2)
            self.assertIn("SAFEBOX_STATUS=未提交", output)
            record = self.record(db_path, 2)
            self.assertEqual(record["workspace_clean_after"], 0)
            self.assertEqual(record["after_commit"] or "", "")
            self.assertIn("README.md", record["git_status_after"])

            self.run_start(repo, db_path, "--safebox-name", "short-before-record")
            before_short = self.run_git(repo, "rev-parse", "--short", "HEAD")
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("UPDATE safebox_records SET before_commit=? WHERE id=3", (before_short,))
                conn.commit()
            finally:
                conn.close()
            (repo / "README.md").write_text("dirty workspace with short before\n", encoding="utf-8")
            output = self.run_finish(repo, db_path, 3)
            self.assertIn("SAFEBOX_STATUS=未提交", output)
            record = self.record(db_path, 3)
            self.assertEqual(record["after_commit"] or "", "")

            (repo / "README.md").write_text("committed workspace\n", encoding="utf-8")
            self.run_git(repo, "add", "README.md")
            self.run_git(repo, "commit", "-m", "commit after safebox finish")
            committed_after = self.run_git(repo, "rev-parse", "HEAD")
            output = self.run_finish(repo, db_path, 2)
            self.assertIn("SAFEBOX_RECORD_ID=2", output)
            record = self.record(db_path, 2)
            self.assertEqual(record["after_commit"], committed_after)

    def test_safebox_finish_auto_fills_health_when_argument_is_absent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repo = root / "repo"
            db_path = root / "aaie_crm.db"
            self.init_repo(repo)
            self.run_start(repo, db_path)
            (repo / "README.md").write_text("committed health check\n", encoding="utf-8")
            self.run_git(repo, "add", "README.md")
            self.run_git(repo, "commit", "-m", "commit for health check")

            original_health_check = safebox_finish.auto_health_check
            try:
                safebox_finish.auto_health_check = lambda urls=None: "auto health ok"
                self.run_finish_without_health_arg(repo, db_path, 1)
            finally:
                safebox_finish.auto_health_check = original_health_check

            record = self.record(db_path)
            self.assertEqual(record["health_check_result"], "auto health ok")

    def test_status_path_parser_keeps_staged_file_first_character(self):
        parsed = safebox_finish.parse_status_paths("M app.py\n M database.py\n?? tools/safebox_start.py")
        self.assertEqual(parsed, ["app.py", "database.py", "tools/safebox_start.py"])

    def test_safebox_centre_displays_auto_record_fields_and_keeps_regression_guards(self):
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
                       database_changed,test_result,modified_files,record_source,auto_started_at,
                       auto_finished_at,git_status_before,git_status_after,stash_present,
                       workspace_clean_before,workspace_clean_after,diff_summary,database_risk,
                       schema_change_risk,health_check_result,restore_instruction,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
                    (
                        "SAFEBOX Auto Logger v2",
                        "auto-display-test",
                        "有风险",
                        "safebox/safebox-auto-logger-v2",
                        "before123",
                        "after456",
                        1,
                        "PASS",
                        "database.py\ntools/safebox_finish.py",
                        "自动",
                        "2026-07-08T10:00:00",
                        "2026-07-08T10:30:00",
                        "",
                        " M database.py",
                        0,
                        1,
                        0,
                        "database.py | 10 +++++",
                        "是：database.py",
                        "可能有数据库 schema 变更；如果只新增独立表，需要人工确认是否影响核心表。",
                        "/health ok",
                        "自动恢复建议：只复制文本，不执行 git reset。",
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

                handler.safebox_centre("GET")
                self.assertEqual(captured["status"], 200)
                self.assertIn("自动", captured["html"])
                self.assertIn("数据库风险", captured["html"])
                self.assertIn("schema 风险", captured["html"])
                self.assertIn("2026-07-08T10:00:00", captured["html"])

                handler.safebox_detail("GET", str(record_id))
                detail_html = captured["html"]
                self.assertIn("记录来源", detail_html)
                self.assertIn("Git status before", detail_html)
                self.assertIn("Git status after", detail_html)
                self.assertIn("Diff 摘要", detail_html)
                self.assertIn("Health check 结果", detail_html)
                self.assertIn("自动恢复建议：只复制文本，不执行 git reset。", detail_html)

                db.execute("UPDATE safebox_records SET status='未提交' WHERE id=?", (record_id,))
                handler.safebox_detail("GET", str(record_id))
                self.assertIn('<option value="未提交" selected>未提交</option>', captured["html"])
                self.assertIn("合同管理", Path("ui.py").read_text(encoding="utf-8"))
                self.assertIn("Visa Management", Path("ui.py").read_text(encoding="utf-8"))
                self.assertIn("Offer", Path("database.py").read_text(encoding="utf-8"))
                self.assertIn("学校方案报告", Path("ui.py").read_text(encoding="utf-8"))
                self.assertIn("assistant-widget-isolated", Path("app.py").read_text(encoding="utf-8"))
                self.assertIn('r"^/students/(\\d+)$"', Path("app.py").read_text(encoding="utf-8"))
                self.assertIn("家长表单", Path("ui.py").read_text(encoding="utf-8"))
            finally:
                app_module.db = old_db


if __name__ == "__main__":
    unittest.main()
