import sqlite3
import unittest


class MemoryDatabase:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE parent_form_tokens (
                token TEXT PRIMARY KEY,
                consultant_id INTEGER,
                student_hint TEXT,
                student_id TEXT,
                expires_at TEXT,
                submitted_at TEXT,
                invalidated_at TEXT,
                created_by INTEGER,
                created_at TEXT
            );
            CREATE TABLE parent_form_access_bindings (
                token TEXT PRIMARY KEY,
                bound_wechat_openid TEXT,
                bound_wechat_nickname TEXT,
                bound_at TEXT
            );
            CREATE TABLE parent_form_access_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE contract_signing_logs (
                id INTEGER PRIMARY KEY,
                event_at TEXT,
                event_type TEXT
            );
            CREATE TABLE school_plan_report_view_logs (
                id INTEGER PRIMARY KEY,
                viewed_at TEXT,
                event_type TEXT
            );
            CREATE TABLE visa_form_assignments (
                id INTEGER PRIMARY KEY,
                submitted_at TEXT,
                status TEXT
            );
            CREATE TABLE reminders (
                id INTEGER PRIMARY KEY,
                student_id TEXT,
                reminder_type TEXT,
                title TEXT,
                description TEXT,
                created_at TEXT,
                suggested_action TEXT,
                notes TEXT,
                status TEXT,
                snooze_until TEXT,
                source_type TEXT
            );
            """
        )

    def execute(self, sql, params=()):
        cursor = self.connection.execute(sql, params)
        self.connection.commit()
        return cursor

    def one(self, sql, params=()):
        return self.connection.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        return self.connection.execute(sql, params).fetchall()

    def close(self):
        self.connection.close()


class ParentFormLiveNotificationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.database = MemoryDatabase()
        self.database.execute(
            "INSERT INTO parent_form_tokens (token, student_hint, student_id) VALUES (?, ?, ?)",
            ("form-a", "Jesse Zhang", "20260001"),
        )
        self.database.execute(
            "INSERT INTO parent_form_tokens (token, student_hint, student_id) VALUES (?, ?, ?)",
            ("form-b", "学生 B", None),
        )
        self.database.execute(
            """
            INSERT INTO parent_form_access_bindings
                (token, bound_wechat_openid, bound_wechat_nickname, bound_at)
            VALUES (?, ?, ?, ?)
            """,
            ("form-a", "openid-parent-A8K29X", "张妈妈", "2026-08-05T09:58:00+10:00"),
        )
        self._event("form-a", "opened", "2026-08-05T10:00:00+10:00")
        self._event("form-b", "opened", "2026-08-05T10:01:00+10:00")
        self._event("form-a", "authenticated", "2026-08-05T10:02:00+10:00")

    def tearDown(self):
        self.database.close()

    def _event(self, token, event_type, occurred_at):
        self.database.execute(
            """
            INSERT INTO parent_form_access_events
                (token, event_type, occurred_at, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (token, event_type, occurred_at, occurred_at),
        )

    def _adapter(self):
        try:
            from parent_form_notification_adapter import (
                latest_parent_form_activity_at,
                parent_form_activity_notifications,
            )
        except ModuleNotFoundError as exc:
            self.fail(f"家长表单提醒适配模块尚未实现：{exc}")
        return latest_parent_form_activity_at, parent_form_activity_notifications

    def test_maps_open_and_authentication_events_to_safe_toasts(self):
        _, notifications = self._adapter()

        events = notifications(
            self.database,
            "2026-08-05T09:59:59+10:00",
        )

        self.assertEqual(
            [event["id"] for event in events],
            ["parent_form_access:1", "parent_form_access:2", "parent_form_access:3"],
        )
        self.assertEqual(events[0]["title"], "家长已打开学生信息表")
        self.assertEqual(events[0]["kind"], "info")
        self.assertEqual(events[0]["href"], "/parent-links/form-a/access-history")
        self.assertIn("Jesse Zhang", events[0]["message"])
        self.assertEqual(events[2]["title"], "家长已完成微信认证")
        self.assertEqual(events[2]["kind"], "success")
        self.assertIn("张妈妈", events[2]["message"])
        self.assertEqual(events[2]["student_id"], "20260001")
        self.assertNotIn("openid-parent-A8K29X", repr(events))

    def test_cursor_is_strict_and_events_remain_separate_by_token(self):
        _, notifications = self._adapter()

        events = notifications(
            self.database,
            "2026-08-05T10:00:00+10:00",
        )

        self.assertEqual([event["id"] for event in events], ["parent_form_access:2", "parent_form_access:3"])
        self.assertEqual(events[0]["student_name"], "学生 B")
        self.assertEqual(events[1]["student_name"], "Jesse Zhang")

    def test_limit_and_latest_cursor_are_stable(self):
        latest, notifications = self._adapter()

        events = notifications(
            self.database,
            "2026-08-05T09:59:00+10:00",
            limit=2,
        )

        self.assertEqual([event["id"] for event in events], ["parent_form_access:1", "parent_form_access:2"])
        self.assertEqual(latest(self.database), "2026-08-05T10:02:00+10:00")


class ParentFormLiveNotificationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.database = MemoryDatabase()
        self.database.execute(
            "INSERT INTO contract_signing_logs (id, event_at, event_type) VALUES (?, ?, ?)",
            (1, "2026-08-05T09:00:00+10:00", "wechat_bound"),
        )
        self.database.execute(
            "INSERT INTO parent_form_tokens (token, student_hint, student_id) VALUES (?, ?, ?)",
            ("form-integration", "集成测试学生", "20260002"),
        )
        self.database.execute(
            """
            INSERT INTO parent_form_access_events
                (token, event_type, occurred_at, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                "form-integration",
                "opened",
                "2026-08-05T10:00:00+10:00",
                "2026-08-05T10:00:00+10:00",
            ),
        )

    def tearDown(self):
        self.database.close()

    def test_parent_activity_cursor_includes_parent_form_access(self):
        import app as app_module

        original_database = app_module.db
        app_module.db = self.database
        try:
            handler = object.__new__(app_module.CRMHandler)
            self.assertEqual(
                handler.parent_activity_latest_cursor(),
                "2026-08-05T10:00:00+10:00",
            )
        finally:
            app_module.db = original_database

    def test_parent_activity_api_includes_parent_form_notification(self):
        import app as app_module

        original_database = app_module.db
        app_module.db = self.database
        captured = {}
        try:
            handler = object.__new__(app_module.CRMHandler)
            handler.user = {"id": 1}
            handler.query = {"since": ["2026-08-05T09:59:00+10:00"]}
            handler.parent_activity_contract_events = lambda since: []
            handler.parent_activity_school_plan_events = lambda since: []
            handler.parent_activity_visa_information_events = lambda since: []
            handler.json_response = lambda payload, *args, **kwargs: captured.update(payload)

            handler.api_parent_activity_notifications("GET")

            self.assertEqual(len(captured["events"]), 1)
            self.assertEqual(captured["events"][0]["type"], "parent_form_opened")
            self.assertEqual(captured["events"][0]["title"], "家长已打开学生信息表")
        finally:
            app_module.db = original_database


if __name__ == "__main__":
    unittest.main()
