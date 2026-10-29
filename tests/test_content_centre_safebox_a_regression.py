import inspect
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import app as app_module
from database import Database


class ContentCentreSafeBoxARegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template_dir = tempfile.TemporaryDirectory(prefix="content-centre-safebox-a-template-")
        cls.template_path = Path(cls.template_dir.name) / "template.sqlite3"
        db = Database(cls.template_path)
        db.initialize()
        base = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)
        with db.transaction() as conn:
            for index in range(125):
                stamp = (base + timedelta(minutes=index)).isoformat()
                status = "Draft" if index % 3 == 0 else "PendingRecording" if index % 3 == 1 else "Published"
                script_id = conn.execute(
                    """INSERT INTO content_scripts(
                       title,body,category,status,content_year,content_month,content_week,
                       tags_json,created_at,updated_at,recorded_at,published_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"{'Needle ' if index % 25 == 0 else ''}SAFEBOX A Script {index:03d}",
                        f"Content body {index:03d}",
                        "Planning" if index % 2 == 0 else "Visa",
                        status,
                        2026,
                        8,
                        35,
                        '["content","safebox"]',
                        stamp,
                        stamp,
                        "2026-08-31" if status in {"PendingRecording", "Published"} else "",
                        "2026-08-31" if status == "Published" else "",
                    ),
                ).lastrowid
                conn.execute(
                    """INSERT INTO content_materials(
                       script_id,material_type,title,source_path,source_url,file_hash,
                       source_modified_at,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        script_id,
                        "google_drive_document",
                        f"Material {index:03d}",
                        f"gdrive://safebox-a-{script_id}",
                        f"https://drive.google.com/open?id=safebox-a-{script_id}",
                        f"hash-{script_id}",
                        stamp,
                        stamp,
                        stamp,
                    ),
                )
                if status == "Published":
                    conn.execute(
                        """INSERT INTO content_published_posts(
                           script_id,published_at,platform,status,created_at,updated_at
                           ) VALUES(?,?,?,?,?,?)""",
                        (script_id, "2026-08-31", "Xiaohongshu", "Published", stamp, stamp),
                    )
            conn.execute(
                """INSERT INTO content_performance_metrics(
                   script_id,checkpoint,views,likes,saves,comments,shares,measured_at,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (1, "post_day_3", 100, 20, 12, 4, 2, base.isoformat(), base.isoformat(), base.isoformat()),
            )

    @classmethod
    def tearDownClass(cls):
        cls.template_dir.cleanup()

    def setUp(self):
        self.case_dir = tempfile.TemporaryDirectory(prefix="content-centre-safebox-a-case-")
        self.case_path = Path(self.case_dir.name)
        self.db_path = self.case_path / "case.sqlite3"
        shutil.copy2(self.template_path, self.db_path)
        self.db = Database(self.db_path)
        self.old_db = app_module.db
        self.old_db_path = app_module.DB_PATH
        app_module.db = self.db
        app_module.DB_PATH = self.db_path

    def tearDown(self):
        app_module.DB_PATH = self.old_db_path
        app_module.db = self.old_db
        self.case_dir.cleanup()

    def handler(self, query=None, user=None):
        handler = object.__new__(app_module.CRMHandler)
        handler.query = {key: [str(value)] for key, value in (query or {}).items()}
        handler.form = {}
        handler.csrf = "csrf-token"
        handler.path = "/content"
        handler.client_address = ("127.0.0.1", 0)
        handler.headers = {"User-Agent": "SAFEBOX A regression"}
        handler.user = user or {
            "id": 1,
            "username": "admin",
            "role_name": "Admin",
            "display_name": "Sonny Zhang",
        }
        handler.message = lambda: ("", "success")
        handler.send_html = lambda html: html
        return handler

    def test_main_tabs_render_with_temp_db_and_zero_real_external_calls(self):
        external_calls = {
            "sync_redbook_scripts_folder": mock.patch.object(app_module, "sync_redbook_scripts_folder"),
            "sync_google_drive_video_folder": mock.patch.object(app_module, "sync_google_drive_video_folder"),
            "google_drive_request": mock.patch.object(app_module, "google_drive_request"),
            "google_drive_recursive_files": mock.patch.object(app_module, "google_drive_recursive_files"),
            "google_drive_download_file": mock.patch.object(app_module, "google_drive_download_file"),
            "read_google_drive_cloud_source": mock.patch.object(app_module, "read_google_drive_cloud_source"),
            "generated_xiaohongshu_script": mock.patch.object(app_module, "generated_xiaohongshu_script"),
        }
        mocks = {}
        with mock.patch.object(app_module, "latest_content_daily_sync_report", return_value=None), \
            mock.patch.object(app_module.nas, "status", return_value={"mode": "test", "ready": True}):
            for name, patcher in external_calls.items():
                mocks[name] = patcher.start()
                self.addCleanup(patcher.stop)
            expected = {
                "dashboard": ("内容管理工作台", 'href="/content?tab=create"', 'name="sort_by"'),
                "create": ("创建新文案", 'method="post" action="/content/create"', 'name="csrf"'),
                "extract": ("小红书文案提取中心", 'action="/content/extractions/start"', 'name="upload_file"'),
                "sync": ("Google Drive", 'data-drive-scan method="post" action="/content/google-drive/scan"', "立即检查 Drive"),
                "tracking": ("发布数据追踪", 'method="post" action="/content/performance/create"', "保存数据"),
                "analysis": ("内容分析报告", "按分类分析", "表现较好的文案"),
            }
            for tab, required in expected.items():
                with self.subTest(tab=tab):
                    html = self.handler({"tab": tab}).content_center("GET")
                    for needle in required:
                        self.assertIn(needle, html)
        self.assertEqual({name: call.call_count for name, call in mocks.items()}, {name: 0 for name in mocks})

    def test_content_center_method_permissions_and_csrf_contract_are_unchanged(self):
        source = inspect.getsource(app_module.CRMHandler.dispatch)
        content_source = inspect.getsource(app_module.CRMHandler.content_center)
        create_panel_source = inspect.getsource(app_module.CRMHandler.content_create_panel)
        extract_panel_source = inspect.getsource(app_module.CRMHandler.content_extraction_panel)

        self.assertIn('if method == "POST" and self.parsed.path != "/login" and not self.check_csrf()', source)
        self.assertLess(source.index("not self.check_csrf()"), source.index('(r"^/content$", self.content_center)'))
        self.assertIn('if method != "GET":', content_source)
        self.assertIn('return self.error_page("Method Not Allowed", HTTPStatus.METHOD_NOT_ALLOWED)', content_source)
        self.assertIn("if not self.can_create_content()", create_panel_source)
        self.assertIn("if not self.can_create_content()", extract_panel_source)
        self.assertIn('name="csrf"', create_panel_source)
        self.assertIn('name="csrf"', extract_panel_source)

        handler = self.handler()
        handler.error_page = lambda message, status: (message, status)
        self.assertEqual(handler.content_center("POST"), ("Method Not Allowed", app_module.HTTPStatus.METHOD_NOT_ALLOWED))

        denied = self.handler(user={"id": 2, "username": "readonly", "role_name": "View Only", "display_name": "Read Only"})
        self.assertIn("你没有新建文案权限", denied.content_create_panel())
        self.assertIn("你没有使用文案提取中心的权限", denied.content_extraction_panel())

    def test_dashboard_search_filter_sort_pagination_and_status_action_stay_functional(self):
        handler = self.handler({"q": "Needle", "category": "Planning", "sort_by": "created_at", "sort_order": "asc", "page": 1})
        html = handler.content_dashboard_panel()

        self.assertIn('value="Needle"', html)
        self.assertIn('<option value="Planning" selected>Planning</option>', html)
        self.assertIn('<option value="created_at" selected>建立时间</option>', html)
        self.assertIn('<option value="asc" selected>最旧 → 最新</option>', html)
        self.assertIn('name="page" value="1"', html)
        self.assertIn('aria-label="文案库分页"', html)
        self.assertIn("内容库最近记录", html)

        script_id = self.db.one("SELECT id FROM content_scripts WHERE status='Draft' ORDER BY id LIMIT 1")["id"]
        action_handler = self.handler()
        action_handler.form = {
            "action": "set_recorded",
            "status_date": "2026-09-01",
            "return_to": "/content?tab=dashboard",
        }
        action_handler.flash_redirect = lambda location, message, kind="success": (location, message, kind)
        location, message, kind = action_handler.content_script_action("POST", script_id)
        updated = self.db.one("SELECT status,review_status,recorded_at FROM content_scripts WHERE id=?", (script_id,))

        self.assertEqual((location, message, kind), ("/content?tab=dashboard", "已设为已录制。", "success"))
        self.assertEqual(updated["status"], "Recorded")
        self.assertEqual(updated["review_status"], "Approved")
        self.assertEqual(updated["recorded_at"], "2026-09-01")

    def test_dispatch_keeps_content_route_table_contract(self):
        source = inspect.getsource(app_module.CRMHandler.dispatch)
        required_routes = [
            '(r"^/content$", self.content_center)',
            '(r"^/content/create$", self.content_create)',
            '(r"^/content/generate$", self.content_generate)',
            '(r"^/content/extractions/start$", self.content_extraction_start)',
            '(r"^/content/sync-redbook-scripts$", self.content_sync_redbook_scripts)',
            '(r"^/content/sync-google-drive-video$", self.content_sync_google_drive_video)',
            '(r"^/content/google-drive/scan$", self.content_google_drive_scan)',
            '(r"^/content/performance/create$", self.content_performance_create)',
            '(r"^/content/scripts/(\\d+)/action$", self.content_script_action)',
        ]
        for route in required_routes:
            self.assertIn(route, source)

    def test_tracked_diff_scope_is_safebox_a_allowlist(self):
        changed = {
            line.strip()
            for line in subprocess.check_output(
                ["git", "diff", "--name-only"],
                cwd=Path(__file__).resolve().parents[1],
                text=True,
            ).splitlines()
            if line.strip() and not line.startswith("error:")
        }
        self.assertLessEqual(changed, {"adjustment.md", "app.py", "assets/ui-premium-v2.css"})


if __name__ == "__main__":
    unittest.main()
