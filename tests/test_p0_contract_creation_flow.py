import json
import shutil
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path

import app


ROOT = Path(__file__).resolve().parents[1]


class ReminderContractCreatedPolicyTests(unittest.TestCase):
    class Row(dict):
        def keys(self):
            return super().keys()

    def test_contract_created_is_informational_not_overdue(self):
        row = self.Row(
            status="Pending",
            reminder_type="contract.created",
            source_event_type="contract.created",
            due_date="2026-08-20",
            due_datetime="2026-08-20T00:00:00+00:00",
            reminder_datetime="2026-08-20T00:00:00+00:00",
            snooze_until="",
        )
        now = datetime(2026, 8, 22, tzinfo=timezone.utc)
        self.assertEqual(app.reminder_effective_status(row, now), "Pending")
        row["reminder_type"] = "parent_form.submitted"
        row["source_event_type"] = "parent_form.submitted"
        self.assertEqual(app.reminder_effective_status(row, now), "Overdue")

    def test_contract_parent_engaged_is_informational_not_overdue(self):
        occurred_at = "2026-08-22T13:18:35+10:00"
        row = self.Row(
            status="Pending",
            reminder_type="contract.parent_engaged",
            source_event_type="contract.parent_engaged",
            created_at=occurred_at,
            due_date="2026-08-22",
            due_datetime=occurred_at,
            reminder_datetime=occurred_at,
            snooze_until="",
        )
        now = datetime.fromisoformat("2026-08-22T13:18:36+10:00")

        self.assertEqual(app.reminder_effective_status(row, now), "Pending")

    def test_unrelated_reminder_deadline_and_terminal_semantics_are_unchanged(self):
        now = datetime.fromisoformat("2026-08-22T13:18:36+10:00")
        row = self.Row(
            status="Pending",
            reminder_type="parent_form.submitted",
            source_event_type="parent_form.submitted",
            due_date="2026-08-22",
            due_datetime="2026-08-22T13:19:36+10:00",
            reminder_datetime="2026-08-22T13:19:36+10:00",
            snooze_until="",
        )
        self.assertEqual(app.reminder_effective_status(row, now), "Pending")

        row["due_datetime"] = "2026-08-22T13:17:36+10:00"
        row["reminder_datetime"] = "2026-08-22T13:17:36+10:00"
        self.assertEqual(app.reminder_effective_status(row, now), "Overdue")

        row["status"] = "Completed"
        self.assertEqual(app.reminder_effective_status(row, now), "Completed")
        row["status"] = "Cancelled"
        self.assertEqual(app.reminder_effective_status(row, now), "Cancelled")
        row["status"] = "Snoozed"
        row["snooze_until"] = "2026-08-22T14:18:36+10:00"
        self.assertEqual(app.reminder_effective_status(row, now), "Snoozed")


class BrowserPricingContractTests(unittest.TestCase):
    def test_browser_pricing_matches_backend_business_matrix(self):
        script = ROOT / "tests" / "js" / "contract_published_pricing.test.cjs"
        node = shutil.which("node") or "/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
        result = subprocess.run([node, str(script)], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
