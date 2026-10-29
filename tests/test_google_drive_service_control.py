import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from google_drive_service_control import (
    GoogleDriveServiceControl,
    ServiceStartError,
    WriterBusyError,
)
from google_drive_sync_log_store import GoogleDriveRuntimeStore


MELBOURNE = ZoneInfo("Australia/Melbourne")


@dataclass(frozen=True)
class Connection:
    code: str = "connected"
    label: str = "已连接"
    detail: str = "ok"
    last_verified_at: str = "2026-08-10T10:00:00+10:00"
    folder_id: str = "folder-test"
    oauth_ready: bool = True


class ConnectionService:
    def __init__(self, code="connected"):
        self.code = code

    def status(self):
        return Connection(code=self.code)


class GoogleDriveServiceControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="google-drive-control-")
        self.now_value = datetime(2026, 8, 10, 22, 30, tzinfo=MELBOURNE)
        self.store = GoogleDriveRuntimeStore(Path(self.temp.name), now=lambda: self.now_value)

    def tearDown(self):
        self.temp.cleanup()

    def control(self, connection_code="connected"):
        return GoogleDriveServiceControl(
            store=self.store,
            connection_service=ConnectionService(connection_code),
            lock_path=self.store.writer_lock_path,
            now=lambda: self.now_value,
        )

    def test_next_run_is_today_before_2300_and_tomorrow_after_2300(self):
        before = self.control().next_run_at()
        self.now_value = datetime(2026, 8, 10, 23, 1, tzinfo=MELBOURNE)
        after = self.control().next_run_at()

        self.assertEqual(before.isoformat(), "2026-08-10T23:00:00+10:00")
        self.assertEqual(after.isoformat(), "2026-08-11T23:00:00+10:00")

    def test_next_run_uses_correct_offset_across_daylight_saving_boundary(self):
        self.now_value = datetime(2026, 10, 3, 23, 30, tzinfo=MELBOURNE)
        result = self.control().next_run_at()

        self.assertEqual(result.isoformat(), "2026-10-04T23:00:00+11:00")

    def test_status_combines_policy_connection_and_carrier(self):
        paused = self.control().status({"automation_id": "aaie-crm-google-drive", "status": "PAUSED"})
        self.store.write_policy({"is_enabled": True, "status": "RUNNING"})
        running = self.control().status({
            "automation_id": "aaie-crm-google-drive",
            "status": "ACTIVE", "carrier_kind": "launchd",
            "observed_at": "2026-08-09T13:01:00+00:00",
            "attempted_at": "2026-08-09T13:01:00+00:00",
            "attempt_status": "completed",
            "reason_code": "completed",
        })
        abnormal = self.control().status({"automation_id": "wrong", "status": "ACTIVE"})

        self.assertEqual(paused["service_code"], "paused")
        self.assertEqual(paused["scheduler_health_code"], "paused")
        self.assertEqual(running["service_code"], "running")
        self.assertTrue(running["next_run_at"].startswith("2026-08-10T23:00"))
        self.assertEqual(abnormal["service_code"], "abnormal")

    def test_active_configuration_with_blocked_latest_attempt_is_not_healthy(self):
        self.store.write_policy({"is_enabled": True, "status": "RUNNING"})
        status = self.control().status({
            "automation_id": "aaie-crm-google-drive",
            "status": "ACTIVE", "carrier_kind": "launchd",
            "observed_at": "2026-08-09T13:01:00+00:00",
            "attempted_at": "2026-08-09T13:01:00+00:00",
            "attempt_status": "blocked",
            "reason_code": "admin_health_unreachable",
        })

        self.assertEqual(status["service_code"], "abnormal")
        self.assertEqual(status["scheduler_health_code"], "blocked")
        self.assertEqual(status["last_attempt_reason"], "无法连接 Admin 健康检查")

    def test_scan_failure_diagnostic_fields_are_exposed_for_status_ui(self):
        self.store.write_policy({"is_enabled": True, "status": "RUNNING"})
        status = self.control().status({
            "automation_id": "aaie-crm-google-drive",
            "status": "ACTIVE",
            "carrier_kind": "launchd",
            "observed_at": "2026-08-09T13:01:00+00:00",
            "attempted_at": "2026-08-09T13:01:00+00:00",
            "attempt_status": "failed",
            "reason_code": "scan_execution_failed",
            "attempt_id": "attempt-safe",
            "failure_stage": "drive_listing_started",
            "exception_type": "TimeoutError",
            "sanitized_error": "The read operation timed out",
            "http_status": 429,
            "retryability": "transient_retryable",
            "traceback_fingerprint": "abc123",
        })

        self.assertEqual(status["last_attempt_id"], "attempt-safe")
        self.assertEqual(status["last_attempt_failure_stage"], "drive_listing_started")
        self.assertEqual(status["last_attempt_exception_type"], "TimeoutError")
        self.assertEqual(status["last_attempt_error"], "The read operation timed out")
        self.assertEqual(status["last_attempt_http_status"], 429)
        self.assertEqual(status["last_attempt_retryability"], "transient_retryable")

    def test_old_heartbeat_active_flag_is_not_a_valid_scheduler_carrier(self):
        self.store.write_policy({"is_enabled": True, "status": "RUNNING"})
        status = self.control().status({
            "automation_id": "aaie-crm-google-drive",
            "status": "ACTIVE",
            "observed_at": "2026-08-09T13:01:00+00:00",
        })

        self.assertFalse(status["carrier_valid"])
        self.assertEqual(status["service_code"], "abnormal")

    def test_missing_expected_attempt_is_reported_as_stale_after_grace_period(self):
        self.now_value = datetime(2026, 8, 11, 2, 1, tzinfo=MELBOURNE)
        self.store.write_policy({
            "is_enabled": True,
            "status": "RUNNING",
            "started_at": "2026-08-09T10:00:00+10:00",
        })
        status = self.control().status({
            "automation_id": "aaie-crm-google-drive",
            "status": "ACTIVE", "carrier_kind": "launchd",
            "observed_at": "2026-08-09T13:01:00+00:00",
            "attempted_at": "2026-08-09T13:01:00+00:00",
            "attempt_status": "completed",
            "reason_code": "completed",
        })

        self.assertEqual(status["service_code"], "abnormal")
        self.assertEqual(status["scheduler_health_code"], "stale")
        self.assertEqual(status["last_expected_run_at"], "2026-08-10T23:00:00+10:00")

    def test_recent_successful_attempt_keeps_schedule_healthy_across_dst(self):
        self.now_value = datetime(2026, 10, 5, 0, 30, tzinfo=MELBOURNE)
        self.store.write_policy({"is_enabled": True, "status": "RUNNING"})
        status = self.control().status({
            "automation_id": "aaie-crm-google-drive",
            "status": "ACTIVE", "carrier_kind": "launchd",
            "observed_at": "2026-10-04T12:01:00+00:00",
            "attempted_at": "2026-10-04T12:01:00+00:00",
            "attempt_status": "completed",
            "reason_code": "completed",
        })

        self.assertEqual(status["service_code"], "running")
        self.assertEqual(status["scheduler_health_code"], "healthy")
        self.assertEqual(status["last_expected_run_at"], "2026-10-04T23:00:00+11:00")
        self.assertEqual(status["next_run_at"], "2026-10-05T23:00:00+11:00")

    def test_newly_registered_preflight_ready_carrier_is_healthy_before_first_run(self):
        self.now_value = datetime(2026, 8, 27, 14, 0, tzinfo=MELBOURNE)
        self.store.write_policy({
            "is_enabled": True,
            "status": "RUNNING",
            "started_at": "2026-08-10T10:00:00+10:00",
        })
        status = self.control().status({
            "automation_id": "aaie-crm-google-drive",
            "carrier_kind": "launchd",
            "status": "ACTIVE",
            "registered_at": "2026-08-27T04:00:00+00:00",
            "preflight_at": "2026-08-27T04:00:01+00:00",
            "preflight_status": "ready",
            "preflight_reason_code": "preflight_ok",
        })

        self.assertEqual(status["service_code"], "running")
        self.assertEqual(status["scheduler_health_code"], "healthy")
        self.assertEqual(status["last_attempt_at"], "")
        self.assertEqual(status["last_attempt_reason"], "调度环境预检通过")

    def test_failed_current_preflight_overrides_an_old_successful_attempt(self):
        self.store.write_policy({"is_enabled": True, "status": "RUNNING"})
        status = self.control().status({
            "automation_id": "aaie-crm-google-drive",
            "carrier_kind": "launchd",
            "status": "ACTIVE",
            "registered_at": "2026-08-09T01:00:00+10:00",
            "attempted_at": "2026-08-09T23:00:00+10:00",
            "attempt_status": "completed",
            "reason_code": "completed",
            "preflight_at": "2026-08-10T14:00:00+10:00",
            "preflight_status": "blocked",
            "preflight_reason_code": "admin_health_unreachable",
        })

        self.assertEqual(status["service_code"], "abnormal")
        self.assertEqual(status["scheduler_health_code"], "blocked")
        self.assertEqual(status["last_attempt_reason"], "无法连接 Admin 健康检查")

    def test_start_requires_connected_expected_active_carrier(self):
        carrier = {"automation_id": "aaie-crm-google-drive", "carrier_kind": "launchd", "status": "ACTIVE"}
        with self.assertRaises(ServiceStartError):
            self.control("authorization_expired").start(7, carrier)
        with self.assertRaises(ServiceStartError):
            self.control().start(7, {"automation_id": "other", "status": "ACTIVE"})
        with self.assertRaises(ServiceStartError):
            self.control().start(7, {"automation_id": "aaie-crm-google-drive", "status": "PAUSED"})

    def test_start_is_idempotent_and_records_next_run(self):
        carrier = {"automation_id": "aaie-crm-google-drive", "carrier_kind": "launchd", "status": "ACTIVE"}
        started = self.control().start(7, carrier)

        self.assertTrue(started["is_enabled"])
        self.assertEqual(started["status"], "RUNNING")
        self.assertEqual(started["started_by"], 7)
        self.assertIn("next_run_at", started)
        with self.assertRaises(ServiceStartError):
            self.control().start(7, carrier)

    def test_second_writer_cannot_acquire_common_lock(self):
        first = self.control()
        second = self.control()

        with first.writer_lock("manual", actor_user_id=7):
            with self.assertRaises(WriterBusyError):
                with second.writer_lock("automatic"):
                    self.fail("second writer must not acquire the lock")

    def test_start_refuses_while_writer_is_active(self):
        carrier = {"automation_id": "aaie-crm-google-drive", "carrier_kind": "launchd", "status": "ACTIVE"}
        with self.control().writer_lock("manual", actor_user_id=7):
            with self.assertRaises(ServiceStartError):
                self.control().start(7, carrier)

    def test_pause_does_not_interrupt_active_writer(self):
        self.store.write_policy({"is_enabled": True, "status": "RUNNING"})
        with ExitStack() as stack:
            stack.enter_context(self.control().writer_lock("automatic"))
            paused = self.control().pause(7)
            self.assertFalse(paused["is_enabled"])
            self.assertEqual(paused["status"], "PAUSED")
            self.assertTrue(paused["writer_active"])
            self.assertEqual(paused["paused_by"], 7)


if __name__ == "__main__":
    unittest.main()
