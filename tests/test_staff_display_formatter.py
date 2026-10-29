import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import staff_display_formatter


class StaffDisplayFormatterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "staff-display.db"
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(
                """
                CREATE TABLE users(
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL,
                    display_name TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1
                );
                INSERT INTO users(id,username,display_name,is_active) VALUES
                    (1,'sonnyz','Sonny Zhang',1),
                    (2,'tedc','Ted Chen',0),
                    (3,'tongy','',1);
                """
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_active_disabled_and_username_fallback_are_business_names(self):
        staff_map = staff_display_formatter.load_staff_display_map(
            self.database_path, [1, 2, 3]
        )

        self.assertEqual(
            staff_display_formatter.format_staff_name(1, staff_map),
            "Sonny Zhang",
        )
        self.assertEqual(
            staff_display_formatter.format_staff_name(2, staff_map),
            "Ted Chen（已停用）",
        )
        self.assertEqual(
            staff_display_formatter.format_staff_name(3, staff_map),
            "tongy",
        )

    def test_system_null_and_missing_user_have_no_numeric_fallback(self):
        staff_map = staff_display_formatter.load_staff_display_map(
            self.database_path, [1, 99, None]
        )

        self.assertEqual(
            staff_display_formatter.format_staff_name(
                None, staff_map, none_is_system=True
            ),
            "系统",
        )
        self.assertEqual(
            staff_display_formatter.format_staff_name(None, staff_map),
            "未知员工",
        )
        self.assertEqual(
            staff_display_formatter.format_staff_name(99, staff_map),
            "未知员工",
        )
        self.assertEqual(
            staff_display_formatter.format_staff_label(
                "主负责人", 99, staff_map
            ),
            "主负责人：未知员工",
        )

    def test_many_ids_use_one_users_select(self):
        queries = []
        real_connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            connection.set_trace_callback(queries.append)
            return connection

        with patch(
            "staff_display_formatter.sqlite3.connect",
            side_effect=traced_connect,
        ):
            staff_map = staff_display_formatter.load_staff_display_map(
                self.database_path, [3, 1, 2, 1, 99]
            )

        user_selects = [
            query
            for query in queries
            if query.lstrip().upper().startswith("SELECT")
            and " FROM USERS" in query.upper()
        ]
        self.assertEqual(len(user_selects), 1)
        self.assertEqual(staff_map[1], "Sonny Zhang")
        self.assertEqual(staff_map[2], "Ted Chen（已停用）")


if __name__ == "__main__":
    unittest.main()
