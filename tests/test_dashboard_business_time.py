import sqlite3
import unittest
from datetime import date

from dashboard_business_time import (
    MELBOURNE_TIMEZONE,
    business_date_iso,
    business_windows,
    register_dashboard_business_date,
)


class DashboardBusinessTimeTests(unittest.TestCase):
    def test_utc_timestamp_crosses_to_next_melbourne_day(self):
        self.assertEqual("2026-08-17", business_date_iso("2026-08-16T15:30:00Z"))
        self.assertEqual("2026-08-16", business_date_iso("2026-08-16T13:59:59Z"))

    def test_naive_and_date_only_values_are_melbourne_business_values(self):
        self.assertEqual("2026-08-17", business_date_iso("2026-08-17"))
        self.assertEqual("2026-08-17", business_date_iso("2026-08-17 00:01:00"))

    def test_week_is_monday_inclusive_to_next_monday_exclusive(self):
        windows = business_windows(date(2026, 8, 16))
        self.assertEqual(date(2026, 8, 10), windows.week_start)
        self.assertEqual(date(2026, 8, 17), windows.week_end)

    def test_month_end_is_exclusive_next_month(self):
        windows = business_windows(date(2026, 12, 31))
        self.assertEqual(date(2026, 12, 1), windows.month_start)
        self.assertEqual(date(2027, 1, 1), windows.next_month)

    def test_dst_and_non_dst_offsets_use_zoneinfo_rules(self):
        self.assertEqual("2026-10-04", business_date_iso("2026-10-03T16:30:00Z"))
        self.assertEqual("2026-04-05", business_date_iso("2026-04-04T14:30:00Z"))
        self.assertEqual("Australia/Melbourne", MELBOURNE_TIMEZONE.key)

    def test_sqlite_function_uses_the_same_converter(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        register_dashboard_business_date(connection)
        value = connection.execute(
            "SELECT dashboard_business_date(?)",
            ("2026-08-16T15:30:00Z",),
        ).fetchone()[0]
        self.assertEqual("2026-08-17", value)


if __name__ == "__main__":
    unittest.main()
