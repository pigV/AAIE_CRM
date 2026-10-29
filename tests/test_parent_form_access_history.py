import re
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from database import Database
from migration_runner import plan_migrations


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    ROOT / "migrations" / "20260805_013_parent_form_access_history.sql"
)
FORWARD_CHECK_PATH = (
    ROOT
    / "migrations"
    / "checks"
    / "20260805_013_parent_form_access_history_forward_check.sql"
)
SECURE_MIGRATION_PATH = (
    ROOT / "migrations" / "20260805_012_parent_form_secure_draft.sql"
)


class ParentFormAccessHistoryMigrationTests(unittest.TestCase):
    def test_migration_creates_isolated_access_event_table(self):
        self.assertTrue(MIGRATION_PATH.is_file())
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("CREATE TABLE parent_form_tokens(token TEXT PRIMARY KEY)")
            conn.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))
            columns = [
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(parent_form_access_events)"
                )
            ]
            self.assertEqual(
                columns,
                ["id", "token", "event_type", "occurred_at", "created_at"],
            )
            foreign_key = conn.execute(
                """SELECT [table],[from],[to],on_delete
                   FROM pragma_foreign_key_list('parent_form_access_events')"""
            ).fetchone()
            self.assertEqual(
                foreign_key,
                ("parent_form_tokens", "token", "token", "CASCADE"),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO parent_form_access_events(
                         token,event_type,occurred_at,created_at
                       ) VALUES('missing','opened','2026-08-05','2026-08-05')"""
                )
        finally:
            conn.close()

    def test_migration_accepts_only_supported_event_types(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("CREATE TABLE parent_form_tokens(token TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO parent_form_tokens(token) VALUES('form-a')")
            conn.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))
            for event_type in ("opened", "authenticated"):
                conn.execute(
                    """INSERT INTO parent_form_access_events(
                         token,event_type,occurred_at,created_at
                       ) VALUES(?,?,?,?)""",
                    ("form-a", event_type, "2026-08-05", "2026-08-05"),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO parent_form_access_events(
                         token,event_type,occurred_at,created_at
                       ) VALUES('form-a','failed','2026-08-05','2026-08-05')"""
                )
        finally:
            conn.close()

    def test_migration_package_is_discoverable_without_formal_database_write(self):
        self.assertTrue(FORWARD_CHECK_PATH.is_file())
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            migrations = root / "migrations"
            checks = migrations / "checks"
            checks.mkdir(parents=True)
            shutil.copy2(MIGRATION_PATH, migrations / MIGRATION_PATH.name)
            shutil.copy2(
                FORWARD_CHECK_PATH,
                checks / FORWARD_CHECK_PATH.name,
            )
            database = root / "parent-form-access-history.db"
            sqlite3.connect(database).close()

            plan = plan_migrations(database, migrations)

            self.assertEqual(
                [item["version"] for item in plan["will_apply"]],
                ["20260805_013"],
            )


class ParentFormAccessHistoryRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self.folder.name) / "parent-form-access-history.db"
        )
        conn = self.database.connect()
        try:
            conn.executescript(
                """
                CREATE TABLE parent_form_tokens (
                  token TEXT PRIMARY KEY,
                  consultant_id INTEGER NOT NULL,
                  student_hint TEXT,
                  expires_at TEXT NOT NULL,
                  submitted_at TEXT,
                  invalidated_at TEXT,
                  student_id TEXT,
                  created_by INTEGER,
                  created_at TEXT NOT NULL
                );
                """
            )
            conn.executescript(SECURE_MIGRATION_PATH.read_text(encoding="utf-8"))
            conn.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))
            conn.executemany(
                """INSERT INTO parent_form_tokens(
                     token,consultant_id,student_hint,expires_at,created_at
                   ) VALUES(?,?,?,?,?)""",
                [
                    (
                        "form-a",
                        1,
                        "A",
                        "2026-08-12T00:00:00+10:00",
                        "2026-08-05T00:00:00+10:00",
                    ),
                    (
                        "form-b",
                        1,
                        "B",
                        "2026-08-12T00:00:00+10:00",
                        "2026-08-05T00:00:00+10:00",
                    ),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        from parent_form_repository import ParentFormRepository

        self.repository = ParentFormRepository(self.database)

    def tearDown(self):
        self.folder.cleanup()

    def test_access_events_are_token_isolated_and_newest_first(self):
        self.assertTrue(
            self.repository.record_access_event(
                "form-a", "opened", "2026-08-05T10:00:00+10:00"
            )
        )
        self.repository.record_access_event(
            "form-b", "opened", "2026-08-05T10:01:00+10:00"
        )
        self.repository.record_access_event(
            "form-a", "opened", "2026-08-05T10:02:00+10:00"
        )

        rows = self.repository.list_access_events("form-a")

        self.assertEqual(
            [row["occurred_at"] for row in rows],
            ["2026-08-05T10:02:00+10:00", "2026-08-05T10:00:00+10:00"],
        )
        self.assertNotIn("form-b", [row["token"] for row in rows])

    def test_successful_reauthentication_records_every_authentication(self):
        self.repository.bind_identity(
            "form-a",
            {"openid": "openid-a", "nickname": "张妈妈"},
            "2026-08-05T10:00:00+10:00",
        )
        self.repository.bind_identity(
            "form-a",
            {"openid": "openid-a", "nickname": "张妈妈"},
            "2026-08-05T10:05:00+10:00",
        )

        rows = self.repository.list_access_events("form-a")

        self.assertEqual(
            [row["event_type"] for row in rows],
            ["authenticated", "authenticated"],
        )
        self.assertEqual(
            [row["occurred_at"] for row in rows],
            ["2026-08-05T10:05:00+10:00", "2026-08-05T10:00:00+10:00"],
        )

    def test_invalid_event_type_is_rejected(self):
        with self.assertRaises(ValueError):
            self.repository.record_access_event(
                "form-a", "failed", "2026-08-05T10:00:00+10:00"
            )

    def test_identity_conflict_does_not_record_authentication(self):
        from parent_form_repository import BindingIdentityConflict

        self.repository.bind_identity(
            "form-a",
            {"openid": "openid-a"},
            "2026-08-05T10:00:00+10:00",
        )
        with self.assertRaises(BindingIdentityConflict):
            self.repository.bind_identity(
                "form-a",
                {"openid": "openid-other"},
                "2026-08-05T10:05:00+10:00",
            )

        rows = self.repository.list_access_events("form-a")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_type"], "authenticated")

    def test_access_event_summaries_return_counts_and_latest_times(self):
        for token, event_type, stamp in (
            ("form-a", "opened", "2026-08-05T10:00:00+10:00"),
            ("form-a", "authenticated", "2026-08-05T10:01:00+10:00"),
            ("form-a", "opened", "2026-08-05T10:02:00+10:00"),
            ("form-b", "authenticated", "2026-08-05T10:03:00+10:00"),
        ):
            self.repository.record_access_event(token, event_type, stamp)

        summaries = self.repository.access_event_summaries(["form-a", "form-b"])

        self.assertEqual(
            summaries["form-a"],
            {
                "opened_count": 2,
                "authenticated_count": 1,
                "last_opened_at": "2026-08-05T10:02:00+10:00",
                "last_authenticated_at": "2026-08-05T10:01:00+10:00",
            },
        )
        self.assertEqual(summaries["form-b"]["opened_count"], 0)
        self.assertEqual(summaries["form-b"]["authenticated_count"], 1)
        self.assertEqual(self.repository.access_event_summaries([]), {})


class ParentFormAccessHistoryServiceTests(
    ParentFormAccessHistoryRepositoryTests
):
    def setUp(self):
        super().setUp()
        from parent_form_service import ParentFormService

        self.clock_value = datetime(
            2026, 8, 5, 10, 0, 0, tzinfo=ZoneInfo("Australia/Melbourne")
        )
        self.service = ParentFormService(
            self.repository,
            clock=lambda: self.clock_value,
            token_factory=lambda: "unused",
            encrypt=lambda value: value,
            decrypt=lambda value: value,
        )

    def test_record_open_records_real_links_in_any_status_and_skips_missing(self):
        self.assertTrue(self.service.record_open("form-a"))
        self.database.execute(
            "UPDATE parent_form_tokens SET submitted_at=? WHERE token='form-a'",
            ("2026-08-05T10:01:00+10:00",),
        )
        self.assertTrue(self.service.record_open("form-a"))
        self.assertFalse(self.service.record_open("missing"))

        self.assertEqual(
            [
                row["event_type"]
                for row in self.repository.list_access_events("form-a")
            ],
            ["opened", "opened"],
        )


class ParentFormAccessHistoryViewTests(unittest.TestCase):
    def test_masking_never_returns_full_openid(self):
        from parent_form_admin_views import mask_parent_form_openid

        value = mask_parent_form_openid("openid-parent-A8K29X")

        self.assertEqual(value, "****A8K29X")
        self.assertNotIn("openid-parent", value)
        self.assertEqual(mask_parent_form_openid(""), "未返回")

    def test_summary_shows_identity_counts_and_recent_times_without_full_openid(self):
        from parent_form_admin_views import render_parent_form_access_summary

        html = render_parent_form_access_summary(
            {
                "bound_wechat_nickname": "张妈妈",
                "bound_wechat_openid": "openid-parent-A8K29X",
                "bound_at": "2026-08-05T10:00:00+10:00",
            },
            {
                "opened_count": 3,
                "authenticated_count": 2,
                "last_opened_at": "2026-08-05T10:10:00+10:00",
                "last_authenticated_at": "2026-08-05T10:05:00+10:00",
            },
            "/parent-links/token-a/access-history",
        )

        self.assertIn("张妈妈", html)
        self.assertIn("****A8K29X", html)
        self.assertIn("绑定时间 2026-08-05 10:00:00+10:00", html)
        self.assertIn("打开 3 次", html)
        self.assertIn("认证 2 次", html)
        self.assertIn("查看全部记录", html)
        self.assertNotIn("openid-parent-A8K29X", html)

    def test_history_renders_all_events_newest_first_without_full_openid(self):
        from parent_form_admin_views import render_parent_form_access_history

        html = render_parent_form_access_history(
            {
                "token": "form-a",
                "student_hint": "学生 A",
                "expires_at": "2026-08-12T10:00:00+10:00",
            },
            {
                "bound_wechat_nickname": "张妈妈",
                "bound_wechat_openid": "openid-parent-A8K29X",
                "bound_at": "2026-08-05T10:00:00+10:00",
            },
            [
                {
                    "event_type": "opened",
                    "occurred_at": "2026-08-05T10:10:00+10:00",
                },
                {
                    "event_type": "authenticated",
                    "occurred_at": "2026-08-05T10:05:00+10:00",
                },
            ],
            status="填写中",
            consultant_name="顾问王老师",
        )

        self.assertIn("学生 A", html)
        self.assertIn("顾问王老师", html)
        self.assertIn("填写中", html)
        self.assertIn("张妈妈", html)
        self.assertIn("****A8K29X", html)
        self.assertIn("打开链接", html)
        self.assertIn("微信认证成功", html)
        self.assertLess(html.index("10:10:00"), html.index("10:05:00"))
        self.assertNotIn("openid-parent-A8K29X", html)

    def test_history_empty_state_is_clear(self):
        from parent_form_admin_views import render_parent_form_access_history

        html = render_parent_form_access_history(
            {"student_hint": "学生 A", "expires_at": ""},
            None,
            [],
            status="未绑定",
            consultant_name="—",
        )

        self.assertIn("暂无访问记录", html)


class ParentFormAccessHistoryRouteTests(unittest.TestCase):
    class FakeNas:
        @staticmethod
        def status():
            return {"ready": True, "mode": "test"}

    def setUp(self):
        import app as app_module

        self.app_module = app_module
        self.folder = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.folder.name) / "parent-form-routes.db")
        self.database.initialize()
        conn = self.database.connect()
        try:
            conn.executescript(SECURE_MIGRATION_PATH.read_text(encoding="utf-8"))
            conn.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))
            user = conn.execute(
                "SELECT id FROM users ORDER BY id LIMIT 1"
            ).fetchone()
            self.consultant_id = int(user["id"])
            conn.execute(
                """INSERT INTO parent_form_tokens(
                     token,consultant_id,student_hint,expires_at,created_by,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    "form-a",
                    self.consultant_id,
                    "学生 A",
                    "2099-08-12T10:00:00+10:00",
                    self.consultant_id,
                    "2026-08-05T09:00:00+10:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()
        from parent_form_repository import ParentFormRepository

        self.repository = ParentFormRepository(self.database)
        self.repository.bind_identity(
            "form-a",
            {
                "openid": "openid-parent-A8K29X",
                "nickname": "张妈妈",
            },
            "2026-08-05T10:00:00+10:00",
        )
        self.repository.record_access_event(
            "form-a", "opened", "2026-08-05T10:05:00+10:00"
        )
        self.repository.record_access_event(
            "form-a", "opened", "2026-08-05T10:10:00+10:00"
        )
        self.old_db = app_module.db
        self.old_nas = app_module.nas
        app_module.db = self.database
        app_module.nas = self.FakeNas()

    def tearDown(self):
        self.app_module.db = self.old_db
        self.app_module.nas = self.old_nas
        self.folder.cleanup()

    def make_handler(self, *, permitted=True):
        handler = object.__new__(self.app_module.CRMHandler)
        permissions = '["students.edit"]' if permitted else "[]"
        handler.user = {
            "id": self.consultant_id,
            "display_name": "顾问王老师",
            "role_name": "Consultant",
            "permissions_json": permissions,
            "module_permissions_json": "[]",
        }
        handler.csrf = "csrf-token"
        handler.query = {}
        handler.form = {}
        handler.headers = {}
        handler.client_address = ("127.0.0.1", 10000)
        handler.message = lambda: ("", "success")
        return handler

    def test_admin_list_renders_masked_identity_and_event_summary(self):
        handler = self.make_handler()
        captured = {}
        handler.send_html = lambda html, status=200: captured.update(
            html=html, status=status
        )

        handler.parent_links("GET")

        html = captured["html"]
        self.assertIn("张妈妈", html)
        self.assertIn("****A8K29X", html)
        self.assertIn("打开 2 次", html)
        self.assertIn("认证 1 次", html)
        self.assertIn("/parent-links/form-a/access-history", html)
        self.assertNotIn("openid-parent-A8K29X", html)

    def test_history_route_is_get_only_and_renders_newest_first(self):
        handler = self.make_handler()
        captured = []
        handler.send_html = lambda html, status=200: captured.append(
            (html, status)
        )
        handler.error_page = lambda message, status=400: captured.append(
            (message, status)
        )

        handler.parent_link_access_history("GET", "form-a")

        html, status = captured[-1]
        self.assertEqual(status, 200)
        self.assertIn("张妈妈", html)
        self.assertIn("****A8K29X", html)
        self.assertLess(html.index("10:10:00"), html.index("10:05:00"))
        self.assertNotIn("openid-parent-A8K29X", html)

        handler.parent_link_access_history("POST", "form-a")
        self.assertEqual(captured[-1][1], 405)

    def test_history_route_returns_404_and_denies_missing_permission(self):
        handler = self.make_handler()
        captured = []
        handler.error_page = lambda message, status=400: captured.append(
            (message, status)
        )
        handler.send_html = lambda html, status=200: captured.append(
            (html, status)
        )

        handler.parent_link_access_history("GET", "missing")
        self.assertEqual(captured[-1][1], 404)

        denied = self.make_handler(permitted=False)
        denied.error_page = lambda message, status=400: captured.append(
            (message, status)
        )
        denied.parent_link_access_history("GET", "form-a")
        self.assertEqual(captured[-1][1], 403)
        self.assertNotIn("张妈妈", captured[-1][0])

    def test_parent_portal_records_every_real_link_open_in_any_status(self):
        for token, submitted_at, invalidated_at, expires_at in (
            (
                "pending-link",
                None,
                None,
                "2099-08-12T10:00:00+10:00",
            ),
            (
                "submitted-link",
                "2026-08-05T10:00:00+10:00",
                None,
                "2099-08-12T10:00:00+10:00",
            ),
            (
                "expired-link",
                None,
                None,
                "2020-08-12T10:00:00+10:00",
            ),
            (
                "revoked-link",
                None,
                "2026-08-05T10:00:00+10:00",
                "2099-08-12T10:00:00+10:00",
            ),
        ):
            self.database.execute(
                """INSERT INTO parent_form_tokens(
                     token,consultant_id,student_hint,expires_at,submitted_at,
                     invalidated_at,created_by,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    token,
                    self.consultant_id,
                    token,
                    expires_at,
                    submitted_at,
                    invalidated_at,
                    self.consultant_id,
                    "2026-08-05T09:00:00+10:00",
                ),
            )
            handler = self.make_handler()
            handler.school_plan_wechat_oauth_enabled = lambda: False
            handler.send_html = lambda html, status=200: (html, status)
            handler.parent_portal("GET", token)

        missing_handler = self.make_handler()
        missing_handler.send_html = lambda html, status=200: (html, status)
        missing_handler.parent_portal("GET", "missing-link")

        for token in (
            "pending-link",
            "submitted-link",
            "expired-link",
            "revoked-link",
        ):
            self.assertEqual(len(self.repository.list_access_events(token)), 1)
            self.assertEqual(
                self.repository.list_access_events(token)[0]["event_type"],
                "opened",
            )
        self.assertEqual(self.repository.list_access_events("missing-link"), [])

    def test_open_event_failure_does_not_block_parent_page_or_log_full_token(self):
        handler = self.make_handler()
        handler.school_plan_wechat_oauth_enabled = lambda: False
        captured = {}
        handler.send_html = lambda html, status=200: captured.update(
            html=html, status=status
        )
        failing_service = mock.Mock()
        failing_service.record_open.side_effect = sqlite3.Error("write failed")

        with mock.patch.object(
            self.app_module,
            "secure_parent_form_service",
            return_value=failing_service,
        ), mock.patch.object(self.app_module, "append_service_log") as log:
            handler.parent_portal("GET", "form-a")

        self.assertEqual(captured["status"], 200)
        self.assertIn("微信验证", captured["html"])
        self.assertEqual(log.call_args.args[1], "parent_form_open_event_failed")
        self.assertNotIn("form-a", str(log.call_args))

    def test_route_registry_contains_access_history_endpoint(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn(
            '(r"^/parent-links/([^/]+)/access-history$", '
            "self.parent_link_access_history)",
            source,
        )


class ParentFormAccessHistoryCssTests(unittest.TestCase):
    def test_recent_links_table_has_dedicated_layout_hooks(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('class="panel parent-links-recent-panel"', source)
        self.assertIn(
            'class="table-wrap parent-links-table-wrap"',
            source,
        )
        self.assertIn('class="parent-links-table"', source)

    def test_recent_links_css_defines_all_nine_columns(self):
        css = (ROOT / "assets" / "ui-premium-v2.css").read_text(
            encoding="utf-8"
        )
        for column in range(1, 10):
            with self.subTest(column=column):
                self.assertIn(
                    f".parent-links-table th:nth-child({column})",
                    css,
                )
                self.assertIn(
                    f".parent-links-table td:nth-child({column})",
                    css,
                )

    def test_recent_links_dates_actions_and_scroll_have_stable_layout(self):
        css = (ROOT / "assets" / "ui-premium-v2.css").read_text(
            encoding="utf-8"
        )
        self.assertRegex(
            css,
            r"\.parent-links-table td:nth-child\(7\)[^{]*\{[^}]*"
            r"white-space:\s*nowrap",
        )
        self.assertRegex(
            css,
            r"\.parent-links-table \.row-actions[^{]*\{[^}]*"
            r"display:\s*table-cell",
        )
        self.assertRegex(
            css,
            r"\.parent-links-table \.row-actions\s*>\s*\*\s*\+\s*\*"
            r"[^{]*\{[^}]*margin-top:\s*6px",
        )
        self.assertRegex(
            css,
            r"\.parent-links-table-wrap[^{]*\{[^}]*"
            r"overflow-x:\s*auto",
        )

    def test_recent_links_desktop_table_fits_all_columns_without_horizontal_scroll(self):
        css = (ROOT / "assets" / "ui-premium-v2.css").read_text(
            encoding="utf-8"
        )
        start = css.index("/* Premium V2 Parent Links Visual Map */")
        end = css.index("/* End Premium V2 Parent Links Visual Map */", start)
        block = css[start:end]

        self.assertRegex(
            block,
            r"\.parent-links-table-wrap\s*\{[^}]*overflow-x:\s*visible",
        )
        self.assertRegex(
            block,
            r"\.parent-links-table\s*\{[^}]*min-width:\s*0",
        )

        expected_widths = {
            1: "14%",
            2: "8%",
            3: "8%",
            4: "auto",
            5: "11%",
            6: "9%",
            7: "12%",
            8: "7%",
            9: "144px",
        }
        for column, width in expected_widths.items():
            with self.subTest(column=column):
                self.assertRegex(
                    block,
                    rf"\.parent-links-table th:nth-child\({column}\),"
                    rf"[^{{]*\.parent-links-table td:nth-child\({column}\)\s*"
                    rf"\{{[^}}]*width:\s*{re.escape(width)}",
                )

    def test_access_history_css_is_premium_scoped(self):
        css = (ROOT / "assets" / "ui-premium-v2.css").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            ".ui-premium-v2 .parent-links-page .parent-link-access-summary",
            css,
        )
        self.assertIn(".ui-premium-v2 .parent-form-access-history-page", css)
        self.assertIn(
            ".ui-premium-v2 .parent-form-access-history-page "
            ".parent-form-access-timeline",
            css,
        )

    def test_access_history_css_has_no_unscoped_new_selectors(self):
        css = (ROOT / "assets" / "ui-premium-v2.css").read_text(
            encoding="utf-8"
        )
        start = css.index("/* Premium V2: Parent Form Access History */")
        end = css.index("/* End Premium V2: Parent Form Access History */", start)
        block = css[start:end]
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("/*", "@", "}")):
                continue
            if stripped.endswith("{"):
                self.assertTrue(
                    stripped.startswith(
                        (
                            ".ui-premium-v2 .parent-links-page",
                            ".ui-premium-v2 .parent-form-access-history-page",
                        )
                    ),
                    stripped,
                )


if __name__ == "__main__":
    unittest.main()
