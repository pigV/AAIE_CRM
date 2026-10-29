import unittest
import tempfile
from pathlib import Path

import app as app_module
from database import Database
from mobile.parent_links.views import render_recent_link_cards


class MobileUISpecialFixesTests(unittest.TestCase):
    def test_assistant_widget_is_absent_in_mobile_and_retained_on_desktop(self):
        handler = object.__new__(app_module.CRMHandler)
        handler.user = {"id": 1}

        handler.request_view_mode = lambda: app_module.MOBILE_MODE
        self.assertEqual(handler.assistant_widget_html(), "")

        handler.request_view_mode = lambda: "desktop"
        self.assertIn("data-assistant-widget", handler.assistant_widget_html())

    def test_recent_link_mobile_renderer_outputs_cards_without_a_table(self):
        html = render_recent_link_cards(
            [
                {
                    "name": "Jesse <Zhang>",
                    "token": "full-token-1234567890",
                    "expires_at": "2026-08-16",
                    "status": "Pending",
                    "status_class": "ok",
                    "wechat_html": '<div class="parent-link-access-summary">微信未绑定</div>',
                    "draft_html": '<span class="status">尚未填写</span>',
                    "consultant_name": "Consultant A",
                    "created_at": "2026-08-09 23:49",
                    "student_html": '<a href="/students/20269991">20269991</a>',
                    "actions_html": '<button class="copy-button" data-copy="https://form.example/full-token-1234567890">复制链接</button>',
                }
            ]
        )

        self.assertIn('class="mobile-recent-link-card"', html)
        self.assertIn("Jesse &lt;Zhang&gt;", html)
        self.assertIn('title="full-token-1234567890"', html)
        self.assertIn("https://form.example/full-token-1234567890", html)
        self.assertIn("待提交", html)
        self.assertNotIn("<table", html)

    def test_parent_links_selects_cards_only_for_mobile_view_mode(self):
        old_db = app_module.db
        old_nas = app_module.nas
        with tempfile.TemporaryDirectory() as folder:
            try:
                database = Database(Path(folder) / "mobile-parent-links.db")
                database.initialize()
                connection = database.connect()
                try:
                    connection.executescript(
                        (Path(__file__).resolve().parents[1] / "migrations" / "20260805_012_parent_form_secure_draft.sql").read_text(encoding="utf-8")
                    )
                    connection.executescript(
                        (Path(__file__).resolve().parents[1] / "migrations" / "20260805_013_parent_form_access_history.sql").read_text(encoding="utf-8")
                    )
                    connection.commit()
                finally:
                    connection.close()
                database.execute(
                    """INSERT INTO parent_form_tokens(
                         token,consultant_id,student_hint,expires_at,created_by,created_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (
                        "mobile-full-token-1234567890",
                        1,
                        "学生 A",
                        "2026-08-16T12:00:00+10:00",
                        1,
                        "2026-08-09T23:49:00+10:00",
                    ),
                )
                app_module.db = database

                class FakeNas:
                    @staticmethod
                    def status():
                        return {"ready": True, "mode": "test"}

                app_module.nas = FakeNas()
                handler = object.__new__(app_module.CRMHandler)
                handler.user = {"id": 1, "display_name": "Admin", "role_name": "Admin"}
                handler.csrf = "csrf-token"
                handler.query = {}
                handler.form = {}
                handler.headers = {}
                handler.require = lambda permission: permission == "students.edit"
                handler.message = lambda: ("", "")

                mobile_capture = {}
                handler.request_view_mode = lambda: app_module.MOBILE_MODE
                handler.send_html = lambda html, *args, **kwargs: mobile_capture.setdefault("html", html)
                handler.parent_links("GET")
                self.assertIn('class="mobile-recent-link-card"', mobile_capture["html"])
                self.assertNotIn('class="parent-links-table"', mobile_capture["html"])

                desktop_capture = {}
                handler.request_view_mode = lambda: "desktop"
                handler.send_html = lambda html, *args, **kwargs: desktop_capture.setdefault("html", html)
                handler.parent_links("GET")
                self.assertIn('class="parent-links-table"', desktop_capture["html"])
                self.assertNotIn('class="mobile-recent-link-card"', desktop_capture["html"])
            finally:
                app_module.db = old_db
                app_module.nas = old_nas

    def test_recent_link_mobile_cards_do_not_create_horizontal_scroll(self):
        css = (Path(__file__).resolve().parents[1] / "assets" / "mobile.css").read_text(encoding="utf-8")
        self.assertIn(".mobile-recent-link-card-list", css)
        self.assertIn(".mobile-recent-link-card__actions", css)
        self.assertRegex(
            css,
            r"\.mobile-business-page \.parent-links-page \.link-status-tabs\s*\{[^}]*overflow:\s*visible",
        )
        self.assertRegex(
            css,
            r"\.mobile-recent-link-card__token code\s*\{[^}]*text-overflow:\s*ellipsis",
        )


if __name__ == "__main__":
    unittest.main()
