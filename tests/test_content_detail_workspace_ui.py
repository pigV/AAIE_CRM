import json
import re
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest import mock

import app as app_module
from database import Database


class ContentDetailWorkspaceUITest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name).resolve()
        self.old_db = app_module.db
        self.old_nas_status = app_module.nas.status
        self.db = Database(self.temp_path / "isolated-aaie-crm.sqlite3")
        self.db.initialize()
        app_module.db = self.db
        app_module.nas.status = lambda: {
            "status": "ok",
            "ready": True,
            "root": str(self.temp_path / "synthetic-nas"),
        }
        self.csrf = "content-detail-workspace-csrf"

    def tearDown(self):
        app_module.db = self.old_db
        app_module.nas.status = self.old_nas_status
        self.temp_dir.cleanup()

    def handler(self):
        handler = object.__new__(app_module.CRMHandler)
        handler.user = {
            "id": 1,
            "username": "admin",
            "role_name": "Admin",
            "display_name": "Admin",
        }
        handler.csrf = self.csrf
        handler.client_address = ("127.0.0.1", 12600)
        handler.headers = {"User-Agent": "safebox-b-content-detail-workspace"}
        handler.message = lambda: ("", "success")
        return handler

    def render(self, method_name, record_id):
        handler = self.handler()
        captured = {}
        handler.send_html = lambda html, *args, **kwargs: captured.setdefault("html", html)
        with mock.patch.object(
            app_module.urllib.request,
            "urlopen",
            side_effect=AssertionError("external URL access blocked by SAFEBOX B"),
        ):
            getattr(handler, method_name)("GET", record_id)
        return captured["html"]

    def seed_records(self):
        stamp = "2026-08-31T10:00:00"
        topic_id = self.db.execute(
            """INSERT INTO content_topics(
               topic_name,category,status,content_year,source,source_type,source_reference,tags_json,
               imported_at,imported_by,import_batch_id,potential_duplicate,migration_notes,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "SAFEBOX B Topic " + "LongTitle" * 8,
                "澳洲低龄留学",
                "Active",
                2026,
                "CRM",
                "manual",
                "topic-reference",
                json.dumps(["topic", "workspace"], ensure_ascii=False),
                stamp,
                1,
                "safebox-b",
                0,
                "",
                stamp,
                stamp,
            ),
        )
        script_id = self.db.execute(
            """INSERT INTO content_scripts(
               title,body,category,topic_id,status,review_status,version_number,source_type,
               content_year,tags_json,notes,source_url,planned_publish_date,published_at,
               hook,original_text,cleaned_text,viral_analysis_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "SAFEBOX B Script " + "LongTitle" * 8,
                "脚本正文。" * 80,
                "澳洲低龄留学",
                topic_id,
                "Draft",
                "Not Reviewed",
                2,
                "crm_generated",
                2026,
                json.dumps(["签证", "陪读"], ensure_ascii=False),
                "脚本备注",
                "https://example.invalid/source",
                "2026-09-01",
                "",
                "前三秒钩子",
                "原始文案",
                "清洗文案",
                json.dumps({"viral_reason": "结构清晰", "rewrite_direction": "保持员工审核"}, ensure_ascii=False),
                stamp,
                stamp,
            ),
        )
        self.db.execute(
            """INSERT INTO content_script_versions(script_id,title,body,version_number,created_at)
               VALUES(?,?,?,?,?)""",
            (script_id, "SAFEBOX B Script V1", "旧版本正文", 1, stamp),
        )
        staging_id = self.db.execute(
            """INSERT INTO content_import_staging(
               import_batch_id,legacy_system_name,legacy_table_name,legacy_record_id,
               old_title,old_body,old_category,old_status,old_year,old_tags,
               old_source_type,old_source_url,issue_flags_json,needs_review,
               duplicate_title,duplicate_group_id,missing_gdoc_source,imported_at,review_status
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "safebox-b-staging",
                "redbook",
                "scripts",
                2001,
                "SAFEBOX B Staging " + "LongTitle" * 8,
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
                "safebox-b-dup",
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
                    "safebox-b-duplicates",
                    "redbook",
                    "scripts",
                    3000 + index,
                    "SAFEBOX B Duplicate Title",
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
                    "safebox-b-dup",
                    stamp,
                    "Needs Review",
                ),
            )
        return topic_id, script_id, staging_id, "safebox-b-dup"

    def assert_workspace_shell(self, html, page_root):
        self.assertEqual(html.count(f'class="{page_root}"'), 1)
        root_match = re.search(
            rf'<div class="{re.escape(page_root)}">(?P<body>.*?)</div>\s*</main>',
            html,
            flags=re.S,
        )
        self.assertIsNotNone(root_match)
        body = root_match.group("body")
        self.assertEqual(body.count('class="content-detail-workspace"'), 1)
        for class_name in (
            "content-detail-workspace__header",
            "content-detail-workspace__layout",
            "content-detail-workspace__context",
            "content-detail-workspace__main",
            "content-detail-workspace__side",
        ):
            self.assertIn(class_name, body)
        return body

    def test_detail_pages_render_unified_workspace_shell_without_changing_contracts(self):
        topic_id, script_id, staging_id, group_id = self.seed_records()

        topic_body = self.assert_workspace_shell(
            self.render("content_topic_detail", topic_id),
            "content-topic-detail-page",
        )
        self.assertEqual(topic_body.count("<form"), 0)
        self.assertEqual(topic_body.count("<button"), 0)
        self.assertIn('href="/content?tab=topics"', topic_body)
        self.assertIn(f'href="/content/scripts/{script_id}"', topic_body)

        staging_body = self.assert_workspace_shell(
            self.render("content_staging_detail", staging_id),
            "content-staging-detail-page",
        )
        self.assertEqual(staging_body.count("<form"), 0)
        self.assertEqual(staging_body.count("<button"), 0)
        self.assertIn('href="/content"', staging_body)
        self.assertIn("SAFEBOX B Staging", staging_body)

        duplicate_body = self.assert_workspace_shell(
            self.render("content_duplicate_group_detail", group_id),
            "content-duplicate-detail-page",
        )
        self.assertEqual(duplicate_body.count("<form"), 1)
        self.assertEqual(duplicate_body.count('name="csrf"'), 1)
        self.assertIn(f'name="csrf" value="{self.csrf}"', duplicate_body)
        self.assertIn(f'action="/content/duplicates/{group_id}/action"', duplicate_body)
        for action in (
            "keep_separate",
            "convert_to_versions",
            "archive_placeholder",
            "skip",
            "left_for_later",
        ):
            self.assertIn(f'value="{action}"', duplicate_body)

        script_body = self.assert_workspace_shell(
            self.render("content_script_detail", script_id),
            "content-script-detail-page",
        )
        self.assertEqual(script_body.count("<form"), 2)
        self.assertEqual(script_body.count('name="csrf"'), 2)
        self.assertIn(f'action="/content/scripts/{script_id}/save"', script_body)
        self.assertIn(f'formaction="/content/scripts/{script_id}/version"', script_body)
        self.assertIn('action="/content/performance/create"', script_body)
        self.assertIn(f'name="return_to" value="/content/scripts/{script_id}"', script_body)

    def test_detail_workspace_css_is_content_scoped_and_theme_aware(self):
        css = Path("assets/ui-premium-v2.css").read_text(encoding="utf-8")
        start_marker = "/* SAFEBOX B Content Detail Workspace Theme Convergence */"
        end_marker = "/* End SAFEBOX B Content Detail Workspace Theme Convergence */"
        self.assertIn(start_marker, css)
        self.assertIn(end_marker, css)
        scoped_css = css.split(start_marker, 1)[1].split(end_marker, 1)[0]

        for selector in (
            ".ui-premium-v2 .content-detail-workspace",
            ".ui-premium-v2 .content-topic-detail-page .content-detail-workspace",
            ".ui-premium-v2 .content-staging-detail-page .content-detail-workspace",
            ".ui-premium-v2 .content-duplicate-detail-page .content-detail-workspace",
            ".ui-premium-v2 .content-script-detail-page .content-detail-workspace",
            ".ui-premium-v2 .content-detail-workspace__layout",
            ".ui-premium-v2 .content-detail-workspace__context",
            ".ui-premium-v2 .content-detail-workspace__main",
            ".ui-premium-v2 .content-detail-workspace__side",
            '[data-theme="dark"] .ui-premium-v2 .content-detail-workspace',
        ):
            self.assertIn(selector, scoped_css)
        self.assertIn("@media (max-width: 900px)", scoped_css)
        self.assertIn("@media (max-width: 430px)", scoped_css)
        self.assertIn("overflow-wrap: anywhere", scoped_css)

        for selector in (
            ".ui-premium-v2 .panel",
            ".ui-premium-v2 form",
            ".ui-premium-v2 input",
            ".ui-premium-v2 select",
            ".ui-premium-v2 textarea",
            ".ui-premium-v2 button",
            ".ui-premium-v2 table",
        ):
            self.assertNotRegex(scoped_css, rf"(?m)^\s*{re.escape(selector)}(?:\s|,|\{{)")
        for protected_selector in (
            ".content-centre-page",
            ".content-create-page",
            ".content-extract-page",
            ".content-sync-page",
            ".content-tracking-page",
            ".content-analysis-page",
            ".students-page",
            ".school-plan",
            ".contract",
            ".reminder",
        ):
            self.assertNotIn(protected_selector, scoped_css.lower())

    def test_existing_detail_routes_and_mutation_handlers_remain_present(self):
        source = Path("app.py").read_text(encoding="utf-8")
        routes_start = source.index("            routes = [")
        routes = source[routes_start:source.index("            for pattern", routes_start)]
        for route in (
            r'r"^/content/topics/(\d+)$"',
            r'r"^/content/staging/(\d+)$"',
            r'r"^/content/duplicates/([^/]+)$"',
            r'r"^/content/duplicates/([^/]+)/action$"',
            r'r"^/content/scripts/(\d+)$"',
            r'r"^/content/scripts/(\d+)/save$"',
            r'r"^/content/scripts/(\d+)/version$"',
            r'r"^/content/scripts/(\d+)/action$"',
        ):
            self.assertIn(route, routes)

        for marker in (
            "    def content_topic_detail(self, method, topic_id):",
            "    def content_staging_detail(self, method, staging_id):",
            "    def content_duplicate_group_detail(self, method, group_id):",
            "    def content_duplicate_group_action(self, method, group_id):",
            "    def content_script_detail(self, method, script_id):",
            "    def content_script_save(self, method, script_id):",
            "    def content_script_version(self, method, script_id):",
            "    def content_script_action(self, method, script_id):",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
