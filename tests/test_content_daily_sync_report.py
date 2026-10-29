import unittest
from unittest import mock

import app as app_module


class FakeRuntimeStore:
    def __init__(self, logs, reports):
        self.logs = logs
        self.reports = reports

    def list_logs(self, limit=50, trigger=None):
        rows = [row for row in self.logs if trigger is None or row.get("trigger") == trigger]
        return rows[:limit]

    def list_reports(self, limit=50, trigger=None):
        rows = [row for row in self.reports if trigger is None or row.get("trigger") == trigger]
        return rows[:limit]


class ContentDailySyncReportTests(unittest.TestCase):
    def render_panel(self, report):
        handler = object.__new__(app_module.CRMHandler)
        with mock.patch.object(app_module, "latest_content_daily_sync_report", return_value=report):
            return handler.content_daily_sync_report_panel()

    def test_historical_failure_formats_render_numeric_count_and_optional_details(self):
        one = {
            "file_name": "one<&>.docx",
            "reason": "timeout <unsafe> authorization=secret-token",
            "source_url": "https://drive.invalid/file?access_token=url-secret",
        }
        cases = (
            ({"failed_count": 0, "errors": []}, 0, False),
            ({"failed_count": 1, "errors": [one]}, 1, True),
            ({"failed_count": 2, "errors": [one, {"file_name": "two.docx"}]}, 2, True),
            ({"failed": None}, 0, False),
            ({"status": "completed"}, 0, False),
            ({"failed": "[]"}, 0, False),
            ({"failed": '[{"file_name":"stringified.docx","reason":"denied"}]'}, 1, True),
            ({"failed_count": 4}, 4, False),
        )
        for report, expected_count, has_details in cases:
            with self.subTest(report=report):
                html = self.render_panel(report)
                self.assertIn(f"<span>失败</span><strong>{expected_count}</strong>", html)
                self.assertEqual("失败明细" in html, has_details)
                self.assertNotIn("<strong>[]</strong>", html)
                self.assertNotIn("<strong>[{", html)
        detail_html = self.render_panel({"failed_count": 1, "errors": [one]})
        self.assertIn("one&lt;&amp;&gt;.docx", detail_html)
        self.assertIn("timeout &lt;unsafe&gt;", detail_html)
        self.assertNotIn("<unsafe>", detail_html)
        self.assertNotIn("secret-token", detail_html)
        self.assertNotIn("url-secret", detail_html)

    def test_latest_report_uses_most_recent_automatic_run_and_correct_report_id(self):
        store = FakeRuntimeStore(
            logs=[
                {"trigger": "manual_scan", "created_at": "2026-08-21T04:17:15Z"},
                {
                    "trigger": "automatic",
                    "created_at": "2026-08-15T13:34:30.457083+00:00",
                    "started_at": "2026-08-15T23:34:28.603580+10:00",
                    "ended_at": "2026-08-15T23:34:30.457059+10:00",
                    "created": 1,
                    "updated": 2,
                    "skipped": 3,
                    "failed": 1,
                    "status": "completed_with_errors",
                },
            ],
            reports=[
                {
                    "report_id": "run-auto-current",
                    "scan_id": "run-auto-current",
                    "trigger": "automatic",
                    "_stored_at": "2026-08-15T13:34:30.430000+00:00",
                    "created": 1,
                    "updated": 2,
                    "skipped_duplicate": 3,
                    "failed": 1,
                    "items": [{"status": "failed", "file_name": "failed.docx", "reason": "timeout"}],
                }
            ],
        )

        with mock.patch.object(app_module, "google_drive_runtime_store", store):
            report = app_module.latest_content_daily_sync_report()
        html = self.render_panel(report)

        self.assertEqual(report["report_id"], "run-auto-current")
        self.assertEqual(report["created"], 1)
        self.assertEqual(report["updated"], 2)
        self.assertEqual(report["failed"], 1)
        self.assertIn("Run ID：run-auto-current", html)
        self.assertIn("2026-08-15T23:34:30.457059+10:00", html)
        self.assertIn("<span>新增</span><strong>1</strong>", html)
        self.assertIn("<span>更新</span><strong>2</strong>", html)
        self.assertIn("<span>失败</span><strong>1</strong>", html)
        self.assertIn("failed.docx", html)


if __name__ == "__main__":
    unittest.main()
