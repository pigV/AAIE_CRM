import unittest

from google_drive_sync_reports import (
    normalize_failure_report,
    select_latest_automatic_report,
)


class FailureReportNormalizationTests(unittest.TestCase):
    def test_count_and_detail_formats_are_normalized_separately(self):
        one = {"file_name": "one.docx", "reason": "timeout"}
        two = {"file_name": "two.docx", "reason": "permission denied"}
        cases = (
            ({"failed_count": 0, "errors": []}, 0, []),
            ({"failed_count": 1, "errors": [one]}, 1, [one]),
            ({"failed_count": 2, "errors": [one, two]}, 2, [one, two]),
            ({"failed": []}, 0, []),
            ({"failed": [one]}, 1, [one]),
            ({"failed": [one, two]}, 2, [one, two]),
            ({"failed": None}, 0, []),
            ({}, 0, []),
            ({"failed": 2}, 2, []),
            ({"failed": "2"}, 2, []),
            ({"failed": "[]"}, 0, []),
            ({"failed": '[{"file_name":"one.docx","reason":"timeout"}]'}, 1, [one]),
            ({"failure_items": [one]}, 1, [one]),
            ({"failed_items": [one, two]}, 2, [one, two]),
            ({"failure_items": "2"}, 2, []),
            ({"failed_items": 1}, 1, []),
            ({"failures": [one]}, 1, [one]),
            ({"items": [{"status": "created"}, {**one, "status": "failed"}]}, 1, [one]),
            ({"items": '[{"status":"failed","file_name":"one.docx","reason":"timeout"}]'}, 1, [one]),
        )

        for report, expected_count, expected_items in cases:
            with self.subTest(report=report):
                normalized = normalize_failure_report(report)
                self.assertEqual(normalized["count"], expected_count)
                self.assertEqual(
                    [
                        {key: value for key, value in item.items() if key != "status"}
                        for item in normalized["items"]
                    ],
                    expected_items,
                )
                self.assertTrue(all(item["status"] == "failed" for item in normalized["items"]))

    def test_valid_scalar_count_wins_without_inventing_missing_details(self):
        normalized = normalize_failure_report({"failed_count": "7"})

        self.assertEqual(normalized, {"count": 7, "items": []})

    def test_invalid_or_negative_counts_are_safe(self):
        self.assertEqual(normalize_failure_report({"failed": -2})["count"], 0)
        self.assertEqual(normalize_failure_report({"failed": "not-json"})["count"], 1)
        self.assertEqual(
            normalize_failure_report({"failed": "not-json"})["items"],
            [{"status": "failed", "reason": "not-json"}],
        )

    def test_current_items_keep_only_failed_details(self):
        normalized = normalize_failure_report(
            {
                "failed": 1,
                "items": [
                    {"status": "created", "file_name": "created.docx"},
                    {"status": "failed", "file_name": "failed.docx", "reason": "timeout"},
                ],
            }
        )

        self.assertEqual(normalized["count"], 1)
        self.assertEqual(normalized["items"][0]["file_name"], "failed.docx")


class LatestAutomaticReportTests(unittest.TestCase):
    def test_newer_manual_log_is_ignored_and_legacy_run_id_is_reconciled(self):
        logs = [
            {
                "trigger": "manual_scan",
                "created_at": "2026-08-21T04:17:15+00:00",
                "created": 0,
                "updated": 0,
                "failed": 0,
            },
            {
                "trigger": "automatic",
                "created_at": "2026-08-15T13:34:30.457083+00:00",
                "started_at": "2026-08-15T23:34:28.603580+10:00",
                "ended_at": "2026-08-15T23:34:30.457059+10:00",
                "created": 1,
                "updated": 0,
                "skipped": 0,
                "failed": 0,
                "status": "completed",
            },
        ]
        reports = [
            {
                "report_id": "automatic-run-1",
                "scan_id": "automatic-run-1",
                "trigger": "automatic",
                "_stored_at": "2026-08-15T13:34:30.430000+00:00",
                "created": 1,
                "updated": 0,
                "failed": 0,
                "items": [{"status": "created", "drive_file_id": "one"}],
            }
        ]

        latest = select_latest_automatic_report(logs, reports)

        self.assertEqual(latest["report_id"], "automatic-run-1")
        self.assertEqual(latest["started_at"], "2026-08-15T23:34:28.603580+10:00")
        self.assertEqual(latest["created"], 1)
        self.assertEqual(latest["updated"], 0)
        self.assertEqual(latest["failed"], 0)

    def test_explicit_report_id_is_preferred_over_signature_matching(self):
        logs = [{
            "trigger": "automatic", "created_at": "2026-08-16T00:00:00Z",
            "ended_at": "2026-08-16T00:00:00Z", "report_id": "selected",
            "created": 0, "updated": 0, "failed": 1,
        }]
        reports = [
            {"report_id": "other", "trigger": "automatic", "created": 0, "updated": 0, "failed": 1},
            {
                "report_id": "selected", "trigger": "automatic", "created": 0, "updated": 0,
                "failed": 1, "items": [{"status": "failed", "reason": "timeout"}],
            },
        ]

        latest = select_latest_automatic_report(logs, reports)

        self.assertEqual(latest["report_id"], "selected")
        self.assertEqual(latest["items"][0]["reason"], "timeout")

    def test_historical_signature_includes_skipped_count_before_time_proximity(self):
        logs = [{
            "trigger": "automatic", "created_at": "2026-08-16T00:00:00Z",
            "ended_at": "2026-08-16T00:00:00Z", "created": 1, "updated": 0,
            "skipped": 3, "failed": 0,
        }]
        reports = [
            {
                "report_id": "wrong-closer", "trigger": "automatic",
                "_stored_at": "2026-08-16T00:00:00.010000Z",
                "created": 1, "updated": 0, "skipped_duplicate": 0, "failed": 0,
            },
            {
                "report_id": "correct-skipped", "trigger": "automatic",
                "_stored_at": "2026-08-16T00:00:02Z",
                "created": 1, "updated": 0, "skipped_duplicate": 3, "failed": 0,
            },
        ]

        latest = select_latest_automatic_report(logs, reports)

        self.assertEqual(latest["report_id"], "correct-skipped")

    def test_equally_near_fallback_reports_fail_closed_without_inventing_run_id(self):
        logs = [{
            "trigger": "automatic", "created_at": "2026-08-16T00:00:00Z",
            "ended_at": "2026-08-16T00:00:00Z", "created": 1,
            "updated": 0, "skipped": 0, "failed": 0,
        }]
        reports = [
            {
                "report_id": "before", "trigger": "automatic",
                "_stored_at": "2026-08-15T23:59:59Z", "created": 1,
                "updated": 0, "skipped_duplicate": 0, "failed": 0,
            },
            {
                "report_id": "after", "trigger": "automatic",
                "_stored_at": "2026-08-16T00:00:01Z", "created": 1,
                "updated": 0, "skipped_duplicate": 0, "failed": 0,
            },
        ]

        latest = select_latest_automatic_report(logs, reports)

        self.assertNotIn("report_id", latest)


if __name__ == "__main__":
    unittest.main()
