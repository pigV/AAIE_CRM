import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as app_module
from database import Database


class _SyntheticDriveRuntimeStore:
    def list_logs(self, limit=50, **_kwargs):
        return [
            {
                "started_at": "2026-08-31T10:00:00",
                "ended_at": "2026-08-31T10:01:00",
                "trigger": "manual_scan",
                "actor_user_id": 1,
                "scanned": 3,
                "created": 1,
                "updated": 1,
                "skipped": 1,
                "failed": 0,
                "status": "completed",
                "report_id": "safebox-c-synthetic",
            }
        ][:limit]

    def list_reports(self, limit=200, **_kwargs):
        return [
            {
                "status": "completed",
                "created": 4,
                "updated": 1,
                "skipped": 2,
                "failed": 0,
                "ended_at": "2026-08-31T10:30:00",
                "report_id": "safebox-c-synthetic",
            }
        ][:limit]

    def load_preview_for_confirmation(self, _scan_id):
        raise AssertionError("SAFEBOX C visual render must not read real Drive previews")


class ContentCentreFinalConsistencyUITest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name).resolve()
        self.old_db = app_module.db
        self.old_nas_status = app_module.nas.status
        self.old_drive_store = app_module.google_drive_runtime_store
        self.old_drive_status = app_module.google_drive_workspace_status
        self.old_latest_sync_report = app_module.latest_content_daily_sync_report
        self.db = Database(self.temp_path / "isolated-aaie-crm.sqlite3")
        self.db.initialize()
        app_module.db = self.db
        app_module.nas.status = lambda: {
            "mode": "test",
            "status": "ok",
            "ready": True,
            "root": str(self.temp_path / "synthetic-nas"),
        }
        app_module.google_drive_runtime_store = _SyntheticDriveRuntimeStore()
        app_module.google_drive_workspace_status = lambda: {
            "connection_code": "connected",
            "connection_label": "已连接",
            "service_code": "paused",
            "service_label": "已暂停",
            "scheduler_health_code": "healthy",
            "scheduler_health_label": "健康",
            "carrier_status": "PAUSED",
            "is_enabled": False,
            "last_success_at": "2026-08-31T10:00:00",
            "last_scan_at": "2026-08-31T10:00:00",
            "next_run_at": "2026-08-31T23:00:00",
            "last_result": "synthetic",
            "last_error": "",
        }
        app_module.latest_content_daily_sync_report = lambda: {
            "status": "completed",
            "created": 4,
            "updated": 1,
            "skipped": 2,
            "failed": 0,
            "ended_at": "2026-08-31T10:30:00",
            "report_id": "safebox-c-synthetic",
        }
        self.csrf = "content-centre-final-consistency-csrf"
        self.ids = self.seed_records()

    def tearDown(self):
        app_module.latest_content_daily_sync_report = self.old_latest_sync_report
        app_module.google_drive_workspace_status = self.old_drive_status
        app_module.google_drive_runtime_store = self.old_drive_store
        app_module.nas.status = self.old_nas_status
        app_module.db = self.old_db
        self.temp_dir.cleanup()

    def seed_records(self):
        stamp = "2026-08-31T10:00:00"
        topic_id = self.db.execute(
            """INSERT INTO content_topics(
               topic_name,category,status,content_year,source,source_type,source_reference,tags_json,
               imported_at,imported_by,import_batch_id,potential_duplicate,migration_notes,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "SAFEBOX C Topic " + "LongTitle" * 6,
                "澳洲低龄留学",
                "Active",
                2026,
                "CRM",
                "manual",
                "synthetic-topic-reference",
                json.dumps(["topic", "final-consistency"], ensure_ascii=False),
                stamp,
                1,
                "safebox-c",
                0,
                "",
                stamp,
                stamp,
            ),
        )
        script_id = self.db.execute(
            """INSERT INTO content_scripts(
               title,body,category,topic_id,status,review_status,version_number,source_type,
               content_year,content_month,content_week,tags_json,notes,source_url,planned_recording_date,
               planned_publish_date,published_at,hook,original_text,cleaned_text,viral_analysis_json,
               created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "SAFEBOX C Script " + "LongTitle" * 6,
                "SAFEBOX C 正文。" * 90,
                "澳洲低龄留学",
                topic_id,
                "Draft",
                "Not Reviewed",
                2,
                "crm_generated",
                2026,
                8,
                35,
                json.dumps(["签证", "陪读"], ensure_ascii=False),
                "final consistency notes",
                "https://example.invalid/content-centre/final-consistency",
                "2026-09-01",
                "2026-09-05",
                "",
                "前三秒钩子",
                "原始文案",
                "清洗文案",
                json.dumps({"viral_reason": "结构清晰", "rewrite_direction": "员工审核"}, ensure_ascii=False),
                stamp,
                stamp,
            ),
        )
        self.db.execute(
            """INSERT INTO content_script_versions(script_id,title,body,version_number,created_at)
               VALUES(?,?,?,?,?)""",
            (script_id, "SAFEBOX C Script V1", "旧版本正文", 1, stamp),
        )
        self.db.execute(
            """INSERT INTO content_published_posts(script_id,topic_id,published_at,platform,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?)""",
            (script_id, topic_id, "2026-08-31", "Xiaohongshu", "Published", stamp, stamp),
        )
        self.db.execute(
            """INSERT INTO content_performance_metrics(script_id,checkpoint,views,likes,saves,comments,shares,measured_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (script_id, "post_day_7", 1800, 120, 80, 22, 8, stamp, stamp, stamp),
        )
        extraction_id = self.db.execute(
            """INSERT INTO content_extraction_jobs(
               source_platform,source_url,source_type,content_type,note_title,pasted_text,
               process_status,status,error_message,category,hook,rewritten_script,confirmed_at,
               is_in_script_library,view_count,favorite_count,created_by,updated_by,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "Xiaohongshu",
                "https://example.invalid/safebox-c",
                "text",
                "text",
                "SAFEBOX C Extraction",
                "Synthetic pasted text",
                "已完成",
                "待确认",
                "",
                "澳洲低龄留学",
                "hook",
                "rewritten script",
                stamp,
                1,
                100,
                20,
                1,
                1,
                stamp,
                stamp,
            ),
        )
        staging_id = self.db.execute(
            """INSERT INTO content_import_staging(
               import_batch_id,legacy_system_name,legacy_table_name,legacy_record_id,
               old_title,old_body,old_category,old_status,old_year,old_tags,
               old_source_type,old_source_url,issue_flags_json,needs_review,
               duplicate_title,duplicate_group_id,missing_gdoc_source,imported_at,review_status
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "safebox-c-staging",
                "redbook",
                "scripts",
                4001,
                "SAFEBOX C Staging " + "LongTitle" * 6,
                "staging 正文。" * 80,
                "旧分类",
                "待审核",
                2025,
                "留学,签证",
                "url",
                "https://example.invalid/staging",
                json.dumps({"duplicate_title": True, "missing_gdoc_source": True}, ensure_ascii=False),
                1,
                1,
                "safebox-c-duplicate",
                1,
                stamp,
                "Needs Review",
            ),
        )
        for index in range(2):
            self.db.execute(
                """INSERT INTO content_import_staging(
                   import_batch_id,legacy_system_name,legacy_table_name,legacy_record_id,
                   old_title,old_body,old_category,old_status,old_year,old_tags,
                   old_source_type,old_source_url,issue_flags_json,needs_review,
                   duplicate_title,duplicate_group_id,imported_at,review_status
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "safebox-c-duplicates",
                    "redbook",
                    "scripts",
                    4100 + index,
                    "SAFEBOX C Duplicate Title",
                    f"duplicate 正文 {index}",
                    "旧分类",
                    "待审核",
                    2025,
                    "留学",
                    "url",
                    f"https://example.invalid/duplicate/{index}",
                    "{}",
                    1,
                    1,
                    "safebox-c-duplicate",
                    stamp,
                    "Needs Review",
                ),
            )
        return {
            "topic": topic_id,
            "script": script_id,
            "extraction": extraction_id,
            "staging": staging_id,
            "duplicate": "safebox-c-duplicate",
        }

    def handler(self, query=None):
        user = dict(
            self.db.one(
                "SELECT u.*,r.name role_name,r.permissions_json "
                "FROM users u JOIN roles r ON r.id=u.role_id WHERE u.username='admin'"
            )
        )
        user["module_permissions_json"] = json.dumps(["content_center"])
        handler = object.__new__(app_module.CRMHandler)
        handler.user = user
        handler.query = query or {}
        handler.csrf = self.csrf
        handler.client_address = ("127.0.0.1", 13100)
        handler.headers = {"User-Agent": "safebox-c-content-final-consistency"}
        handler.message = lambda: ("", "success")
        return handler

    def render_content_tab(self, tab):
        handler = self.handler({"tab": [tab]})
        captured = {}
        handler.send_html = lambda html, *args, **kwargs: captured.setdefault("html", html)
        with mock.patch.object(
            app_module.urllib.request,
            "urlopen",
            side_effect=AssertionError("external URL access blocked by SAFEBOX C"),
        ):
            handler.content_center("GET")
        return captured["html"]

    def render_detail(self, method_name, record_id):
        handler = self.handler()
        captured = {}
        handler.send_html = lambda html, *args, **kwargs: captured.setdefault("html", html)
        with mock.patch.object(
            app_module.urllib.request,
            "urlopen",
            side_effect=AssertionError("external URL access blocked by SAFEBOX C"),
        ):
            getattr(handler, method_name)("GET", record_id)
        return captured["html"]

    def root_body(self, html, root):
        match = re.search(rf'<div class="{re.escape(root)}">(?P<body>.*?)</div>\s*</main>', html, flags=re.S)
        self.assertIsNotNone(match)
        return match.group("body")

    def test_main_workspace_tabs_keep_routes_forms_csrf_and_roots(self):
        expectations = {
            "dashboard": ("content-centre-page", 'method="get" action="/content"', 'href="/content/scripts/'),
            "create": ("content-create-page", 'method="post" action="/content/create"', 'name="title"'),
            "extract": ("content-extract-page", 'action="/content/extractions/start"', 'type="file" name="upload_file"'),
            "sync": ("content-sync-page", 'action="/content/google-drive/scan"', "google-drive-update-page"),
            "tracking": ("content-tracking-page", 'action="/content/performance/create"', "content-tracking-table"),
            "analysis": ("content-analysis-page", "content-analysis-table", "content-analysis-kpi-grid"),
        }
        for tab, (root, marker_a, marker_b) in expectations.items():
            html = self.render_content_tab(tab)
            body = self.root_body(html, root)
            self.assertIn(marker_a, body)
            self.assertIn(marker_b, body)
            self.assertIn('class="link-status-tabs content-tabs', body)
            if tab != "analysis":
                self.assertIn(f'name="csrf" value="{self.csrf}"', body)
            self.assertNotIn("/content/sync-redbook-scripts", body)

    def test_detail_workspaces_keep_read_only_and_mutation_contracts(self):
        topic_body = self.root_body(
            self.render_detail("content_topic_detail", self.ids["topic"]),
            "content-topic-detail-page",
        )
        self.assertEqual(topic_body.count("<form"), 0)
        self.assertIn('href="/content?tab=topics"', topic_body)

        staging_body = self.root_body(
            self.render_detail("content_staging_detail", self.ids["staging"]),
            "content-staging-detail-page",
        )
        self.assertEqual(staging_body.count("<form"), 0)
        self.assertIn("SAFEBOX C Staging", staging_body)

        duplicate_body = self.root_body(
            self.render_detail("content_duplicate_group_detail", self.ids["duplicate"]),
            "content-duplicate-detail-page",
        )
        self.assertEqual(duplicate_body.count("<form"), 1)
        self.assertIn(f'name="csrf" value="{self.csrf}"', duplicate_body)
        self.assertIn(f'action="/content/duplicates/{self.ids["duplicate"]}/action"', duplicate_body)
        for action in ("keep_separate", "convert_to_versions", "archive_placeholder", "skip", "left_for_later"):
            self.assertIn(f'value="{action}"', duplicate_body)

        script_body = self.root_body(
            self.render_detail("content_script_detail", self.ids["script"]),
            "content-script-detail-page",
        )
        self.assertEqual(script_body.count("<form"), 2)
        self.assertEqual(script_body.count(f'name="csrf" value="{self.csrf}"'), 2)
        self.assertIn(f'action="/content/scripts/{self.ids["script"]}/save"', script_body)
        self.assertIn(f'formaction="/content/scripts/{self.ids["script"]}/version"', script_body)
        self.assertIn('action="/content/performance/create"', script_body)

    def test_css_scopes_preserve_content_only_final_consistency_boundaries(self):
        css = Path("assets/ui-premium-v2.css").read_text(encoding="utf-8")
        for marker in (
            "/* SAFEBOX A Content Centre Main Workspace Theme Convergence */",
            "/* SAFEBOX B Content Detail Workspace Theme Convergence */",
        ):
            self.assertIn(marker, css)
        scoped = (
            css.split("/* SAFEBOX A Content Centre Main Workspace Theme Convergence */", 1)[1]
            .split("/* End SAFEBOX B Content Detail Workspace Theme Convergence */", 1)[0]
        )
        for selector in (
            ".ui-premium-v2 .content-centre-page",
            ".ui-premium-v2 .content-create-page",
            ".ui-premium-v2 .content-extract-page",
            ".ui-premium-v2 .content-sync-page",
            ".ui-premium-v2 .content-tracking-page",
            ".ui-premium-v2 .content-analysis-page",
            ".ui-premium-v2 .content-detail-workspace",
            '[data-theme="dark"] .ui-premium-v2 .content-centre-page',
            '[data-theme="dark"] .ui-premium-v2 .content-detail-workspace',
            "--content-main-gradient",
            "--content-detail-blue",
        ):
            self.assertIn(selector, scoped)
        for forbidden in (
            ".students-page",
            ".school-plan",
            ".contract",
            ".reminder",
            ".backup",
            ".revenue",
        ):
            self.assertNotIn(forbidden, scoped.lower())
        for global_selector in (".ui-premium-v2 .panel", ".ui-premium-v2 input", ".ui-premium-v2 button"):
            self.assertNotRegex(scoped, rf"(?m)^\s*{re.escape(global_selector)}(?:\s|,|\{{)")

    def test_final_consistency_gate_uses_isolated_storage_and_blocks_external_calls(self):
        external_calls = {"urlopen": 0}

        def blocked_urlopen(*_args, **_kwargs):
            external_calls["urlopen"] += 1
            raise AssertionError("external URL access blocked by SAFEBOX C")

        with mock.patch.object(app_module.urllib.request, "urlopen", side_effect=blocked_urlopen):
            for tab in ("dashboard", "create", "extract", "sync", "tracking", "analysis"):
                self.render_content_tab(tab)
            self.render_detail("content_topic_detail", self.ids["topic"])
            self.render_detail("content_staging_detail", self.ids["staging"])
            self.render_detail("content_duplicate_group_detail", self.ids["duplicate"])
            self.render_detail("content_script_detail", self.ids["script"])

        self.assertEqual(external_calls["urlopen"], 0)
        self.assertIn(self.temp_path, Path(app_module.db.path).resolve().parents)
        self.assertEqual(Path(app_module.db.path).name, "isolated-aaie-crm.sqlite3")


if __name__ == "__main__":
    unittest.main()
