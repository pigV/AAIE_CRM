import tempfile
import unittest
from pathlib import Path

import app as app_module
from database import Database


class ContentCentreMainWorkspaceUITest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name).resolve()
        self.old_db = app_module.db
        self.db = Database(self.temp_path / "isolated-aaie-crm.sqlite3")
        self.db.initialize()
        app_module.db = self.db

    def tearDown(self):
        app_module.db = self.old_db
        self.temp_dir.cleanup()

    def handler(self, query=None):
        handler = object.__new__(app_module.CRMHandler)
        handler.query = query or {}
        handler.csrf = "csrf-token"
        handler.user = {"id": 1, "username": "sonny", "role_name": "Admin", "display_name": "Sonny Zhang"}
        return handler

    def test_dashboard_uses_isolated_database_and_renders_main_workspace_contract(self):
        self.assertIn(self.temp_path, Path(app_module.db.path).resolve().parents)
        stamp = "2026-08-31T10:00:00"
        script_id = self.db.execute(
            """INSERT INTO content_scripts(title,body,category,status,content_year,content_month,content_week,
               recorded_at,tags_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "澳洲低龄留学选题",
                "完整口播正文",
                "澳洲低龄留学",
                "PendingRecording",
                2026,
                8,
                35,
                "2026-08-31",
                '["签证","陪读"]',
                stamp,
                stamp,
            ),
        )
        self.db.execute(
            """INSERT INTO content_extraction_jobs(source_platform,content_type,process_status,status,note_title,
               created_by,updated_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            ("Xiaohongshu", "video", "待处理", "待拆解", "待处理任务", 1, 1, stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO content_published_posts(script_id,published_at,platform,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?)""",
            (script_id, "2026-08-31", "Xiaohongshu", "Published", stamp, stamp),
        )

        html = self.handler().content_dashboard_panel()

        self.assertIn("content-centre-dashboard", html)
        self.assertIn("内容管理工作台", html)
        self.assertIn("待处理队列", html)
        self.assertIn("最新同步概览", html)
        self.assertIn("高频入口", html)
        self.assertIn("内容库最近记录", html)
        self.assertIn("澳洲低龄留学选题", html)
        self.assertIn('href="/content?tab=create"', html)
        self.assertIn('href="/content?tab=extract"', html)
        self.assertIn('href="/content?tab=tracking"', html)
        self.assertIn('href="/content?tab=analysis"', html)
        self.assertIn('name="q"', html)
        self.assertIn('name="sort_by"', html)
        self.assertIn('name="sort_order"', html)
        self.assertIn('name="status_date"', html)
        self.assertIn('name="return_to" value="/content?tab=dashboard"', html)
        self.assertIn('action="/content/scripts/', html)
        self.assertIn('value="set_recorded"', html)
        self.assertIn('value="mark_published"', html)
        self.assertIn('name="csrf" value="csrf-token"', html)

    def test_content_center_roots_keep_existing_tabs_without_new_routes(self):
        source = Path("app.py").read_text(encoding="utf-8")
        content_center_block = source[source.index("def content_center"):source.index("def content_create(self, method):")]

        self.assertIn('"dashboard": self.content_dashboard_panel', content_center_block)
        self.assertIn('"create": self.content_create_panel', content_center_block)
        self.assertIn('"extract": self.content_extraction_panel', content_center_block)
        self.assertIn('"sync": self.content_sync_panel', content_center_block)
        self.assertIn('"tracking": self.content_tracking_panel', content_center_block)
        self.assertIn('"analysis": self.content_analysis_report_panel', content_center_block)
        self.assertIn('class="content-centre-page"', content_center_block)
        self.assertIn('"content-create-page"', content_center_block)
        self.assertIn('"content-extract-page"', content_center_block)
        self.assertIn('"content-sync-page"', content_center_block)
        self.assertIn('"content-tracking-page"', content_center_block)
        self.assertIn('"content-analysis-page"', content_center_block)
        self.assertNotIn("content_materials_panel,", content_center_block)
        self.assertNotIn("content_hooks_panel,", content_center_block)

    def test_css_is_content_scoped_and_theme_aware(self):
        css = Path("assets/ui-premium-v2.css").read_text(encoding="utf-8")
        safebox_css = css.split("/* SAFEBOX A Content Centre Main Workspace Theme Convergence */", 1)[1]
        safebox_css = safebox_css.split("/* End SAFEBOX A Content Centre Main Workspace Theme Convergence */", 1)[0]

        self.assertIn("SAFEBOX A Content Centre Main Workspace Theme Convergence", css)
        self.assertIn(".ui-premium-v2 .content-centre-page", safebox_css)
        self.assertIn(".ui-premium-v2 .content-create-page", safebox_css)
        self.assertIn(".ui-premium-v2 .content-extract-page", safebox_css)
        self.assertIn(".ui-premium-v2 .content-sync-page", safebox_css)
        self.assertIn(".ui-premium-v2 .content-tracking-page", safebox_css)
        self.assertIn(".ui-premium-v2 .content-analysis-page", safebox_css)
        self.assertIn('[data-theme="dark"] .ui-premium-v2 .content-centre-page', safebox_css)
        self.assertIn("--content-main-gradient", safebox_css)
        self.assertIn(".content-centre-overview-grid", safebox_css)
        self.assertIn(".content-centre-queue-list", safebox_css)
        self.assertIn(".content-centre-sync-grid", safebox_css)
        self.assertIn(".content-centre-chip-actions", safebox_css)
        self.assertIn(".content-script-card-actions > form > input[type=\"date\"]", safebox_css)
        self.assertNotIn("body .content-centre-page", safebox_css)
        self.assertNotIn("\n.panel {", safebox_css)

    def test_existing_post_handlers_are_not_changed_by_ui_contract(self):
        source = Path("app.py").read_text(encoding="utf-8")
        drive_views_source = Path("google_drive_sync_views.py").read_text(encoding="utf-8")

        self.assertIn("def content_create(self, method):", source)
        self.assertIn("def content_extraction_start(self, method):", source)
        self.assertIn("def content_sync_panel(self):", source)
        self.assertIn("def content_performance_create(self, method):", source)
        self.assertIn('action="/content/create"', source)
        self.assertIn('action="/content/extractions/start"', source)
        self.assertIn('action="/content/performance/create"', source)
        self.assertIn('data-drive-scan method="post" action="/content/google-drive/scan"', drive_views_source)


if __name__ == "__main__":
    unittest.main()
