import unittest
from unittest.mock import Mock, patch

import app


class ReminderPopupQuietTests(unittest.TestCase):
    def setUp(self):
        self.handler = object.__new__(app.CRMLegacyHandler)
        self.handler.user = {"id": 7}
        self.handler.csrf = "test"
        self.handler.headers = {}
        self.row = {
            "id": 42, "student_id": None, "source_event_id": "", "source_record_id": "",
            "reminder_datetime": "", "due_datetime": "", "due_date": "",
            "description": "Test reminder", "notes": "", "title": "Test reminder",
        }
        for name, value in (
            ("can_access_module", True),
            ("can_reminder_action", True),
            ("reminder_effective_status", "Due"),
            ("reminder_related_link", ""),
        ):
            patcher = patch.object(app, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_quiet_cookie_suppresses_popup_until_expiry_for_current_account(self):
        self.handler.headers["Cookie"] = "aaie_reminder_quiet_7=8200000"
        with patch.object(app.time, "time", return_value=8199.999):
            self.assertEqual(self.handler.global_reminder_popup_html([self.row]), "")
            self.handler.user = {"id": 8}
            self.assertIn("data-reminder-toast", self.handler.global_reminder_popup_html([self.row]))
        self.handler.user = {"id": 7}
        with patch.object(app.time, "time", return_value=8200):
            self.assertIn("data-reminder-toast", self.handler.global_reminder_popup_html([self.row]))

    def test_actionable_api_keeps_pending_items_during_quiet_period(self):
        self.handler.headers["Cookie"] = "aaie_reminder_quiet_7=8200000"
        self.handler.json_response = Mock(side_effect=lambda payload: payload)
        with patch.object(app.time, "time", return_value=1000), patch.object(
            app, "list_actionable_canonical_reminders", return_value=[self.row]
        ):
            payload = self.handler.api_reminders_actionable("GET")
        self.assertEqual(payload["reminder_ids"], [42])
        self.assertEqual(payload["toast_html"], "")
