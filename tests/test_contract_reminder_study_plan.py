import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app
from database import Database
import notification_adapter
from notification_adapter import emit_business_event
from reminder_center import complete, list_actionable


STAMP = "2026-11-05T15:07:00+11:00"
NOW = "2026-11-14T15:07:00+11:00"


class ContractReminderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Database(f"{tmp.name}/test.db")
        self.db.initialize()
        with self.db.transaction() as connection:
            connection.executescript(Path("migrations/20260820_026_unified_reminder_center.sql").read_text())
        patcher = patch.object(app, "db", self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db.execute(
            """INSERT INTO students(student_id,passport_first_name,passport_last_name,
                                         created_date,updated_at,form_data_json)
               VALUES('S1','Test','Student',?,?,'{}')""",
            (STAMP, STAMP),
        )
        for contract_id in (1, 2, 3):
            self.db.execute(
                """INSERT INTO contracts(id,student_id,contract_category,contract_type,contract_title,
                                         contract_status,generated_at,updated_at)
                   VALUES(?,'S1','Study','StudyAbroadContract','Contract','Sent',?,?)""",
                (contract_id, STAMP, STAMP),
            )

    def pending(self, contract_id, when=STAMP):
        return app.create_auto_reminder({
            "student_id": "S1", "reminder_type": "contract_sent",
            "title": "家长合同待签署", "source_type": "contract",
            "source_record_id": contract_id, "related_record_id": contract_id,
            "due_datetime": when, "repeat_rule": "every_2_days",
        })

    def actionable(self, when=NOW):
        return [r["id"] for r in list_actionable(self.db, {"role_name": "Admin"}, now=when, validate_contracts=True)]

    def test_signed_evidence_clears_only_matching_waiting_reminders(self):
        ids = [self.pending(i) for i in (1, 2, 3)]
        self.db.execute("UPDATE contracts SET contract_status='FullyExecuted' WHERE id=1")
        self.db.execute(
            """INSERT INTO contract_signatures(contract_id,signed_at,signature_image_path,created_at,updated_at)
               VALUES(2,?,'fixture.png',?,?)""",
            (STAMP, STAMP, STAMP),
        )
        self.assertEqual(self.actionable(), [ids[2]])
        app.resolve_completed_auto_reminders()
        self.assertEqual(self.db.one("SELECT status FROM reminders WHERE id=?", (ids[0],))["status"], "Completed")
        self.assertEqual(self.db.one("SELECT status FROM reminders WHERE id=?", (ids[1],))["status"], "Completed")
        self.assertEqual(notification_adapter.business_event_notifications(self.db, STAMP, include_contract_signed=True), [])

    def test_complete_prevents_regeneration_after_schedule_changes(self):
        rid = self.pending(1)
        complete(self.db, rid, actor_id=None, now=NOW)
        self.assertIsNone(self.pending(1, NOW))
        self.assertEqual(self.actionable(), [])

    def test_staff_recording_notifies_only_for_new_signature(self):
        rid = self.pending(1)
        previous = self.db.one("SELECT * FROM contracts WHERE id=1")
        self.db.execute("UPDATE contracts SET contract_status='ParentSigned',signed_at=? WHERE id=1", (NOW,))
        app.notify_recorded_contract_signature(previous, NOW)
        app.notify_recorded_contract_signature(self.db.one("SELECT * FROM contracts WHERE id=1"), NOW)
        self.assertEqual(self.db.one("SELECT status FROM reminders WHERE id=?", (rid,))["status"], "Completed")
        self.assertEqual(len(notification_adapter.business_event_notifications(self.db, STAMP, include_contract_signed=True)), 1)

    def test_open_or_close_defers_waiting_reminder_for_configured_interval(self):
        opened = self.pending(1)
        closed = self.pending(2)
        self.db.set_setting("reminder_contract_repeat_every_days", "3")
        handler = object.__new__(app.CRMHandler)
        handler.user = {"id": None, "role_name": "Admin", "username": "fixture"}
        handler.headers = {"X-Requested-With": "fetch"}
        handler.client_address = ("127.0.0.1", 1234)
        handler.json_response = Mock()
        with patch.object(app, "now_iso", return_value=NOW), patch.object(app, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.fromisoformat(NOW)
            handler.reminder_action("POST", opened, "open")
            handler.json_response.assert_called_with({"ok": True, "redirect": "/students/S1"})
            handler.reminder_action("POST", closed, "defer")
        self.assertEqual(self.actionable("2026-11-17T15:06:59+11:00"), [])
        self.assertEqual(self.actionable("2026-11-17T15:07:00+11:00"), [opened, closed])

    def test_new_signed_notification_is_recorded_once_per_contract(self):
        self.pending(1)
        self.db.execute("UPDATE contracts SET contract_status='ParentSigned' WHERE id=1")
        for signature_id in (12, 13):
            emit_business_event(
                self.db, event_type="contract.signed", student_id="S1", entity_type="contract",
                entity_id=1, occurred_at=NOW, actor_type="parent",
                idempotency_key=f"contract:1:signed:{signature_id}", title="家长合同已签署",
                message="家长合同已签署", href="/students/S1",
            )
        self.assertEqual(self.actionable(), [])
        self.assertEqual([e["title"] for e in notification_adapter.business_event_notifications(self.db, STAMP, include_contract_signed=True)], ["家长合同已签署"])
        self.assertEqual(notification_adapter.business_event_notifications(self.db, STAMP), [])


class StudyPlanStudentLinkTests(unittest.TestCase):
    def test_detail_heading_links_to_student_profile(self):
        report = dict(id=9, student_id="20260174", report_version=4, report_title="Study Plan",
                      file_path="", status="generated", created_at=STAMP, created_by_name="")
        handler = SimpleNamespace(
            user={}, csrf="", school_plan_snapshot=lambda row: {},
            school_plan_student_name=lambda student: "Student & Name",
            school_plan_report_status_label=lambda status: status,
            school_plan_report_share_management_html=lambda row: "",
            school_plan_report_snapshot_summary_html=lambda *args: "",
            message=lambda: ("", ""), send_html=Mock(),
        )
        with patch.multiple(app, db=SimpleNamespace(one=lambda *args: report),
                            can_access_module=lambda *args: True,
                            page=lambda title, body, *args: body,
                            nas=SimpleNamespace(status=lambda: {})):
            app.CRMLegacyHandler.school_plan_report_detail(handler, "GET", 9)
        html = handler.send_html.call_args.args[0]
        self.assertRegex(html, r'<h2><a [^>]*href="/students/20260174"[^>]*>Student &amp; Name</a> · v4</h2>')
