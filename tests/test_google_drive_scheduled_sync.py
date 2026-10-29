import inspect
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools import google_drive_scheduled_sync as scheduled


EXPECTED_APP = Path("/Volumes/Zhang/AAIE CodeX/CRM")
EXPECTED_DB = EXPECTED_APP / "database/aaie_crm.db"


class PolicyStore:
    def __init__(self, enabled=True):
        self.enabled = enabled

    def read_policy(self):
        return {"is_enabled": self.enabled, "status": "RUNNING" if self.enabled else "PAUSED"}


class Connection:
    def __init__(self, code="connected"):
        self.code = code

    def status(self):
        return type("Status", (), {"code": self.code})()


class ServiceControl:
    def __init__(self, code="connected"):
        self.connection_service = Connection(code)


class Service:
    def __init__(
        self, *, connection="connected", report=None,
        scan_error=None, confirm_error=None, probe_error=None,
    ):
        self.service_control = ServiceControl(connection)
        self.report = report or {"status": "completed", "failed": 0}
        self.scan_error = scan_error
        self.confirm_error = confirm_error
        self.probe_error = probe_error
        self.probe_calls = 0
        self.scan_calls = []
        self.confirm_calls = []

    def probe_drive_metadata(self):
        self.probe_calls += 1
        if self.probe_error:
            raise self.probe_error
        return {"id": "folder-test"}

    def scan(self, actor_user_id, *args, **kwargs):
        self.scan_calls.append(actor_user_id)
        stage_callback = kwargs.get("stage_callback")
        if stage_callback:
            stage_callback({"stage": "scan_enter", "folder_id": "folder-test"})
        if self.scan_error:
            raise self.scan_error
        return {
            "scan_id": "scan-auto",
            "confirmation_nonce": "nonce-auto",
            "items": [
                {"drive_file_id": "one", "eligible": True, "default_selected": True},
                {"drive_file_id": "manual", "eligible": False, "default_selected": False},
            ],
        }

    def confirm(self, scan_id, nonce, selected_ids, actor_user_id, trigger):
        self.confirm_calls.append((scan_id, nonce, selected_ids, actor_user_id, trigger))
        if self.confirm_error:
            raise self.confirm_error
        return dict(self.report)


def healthy():
    return {
        "status": "ok",
        "service": "admin",
        "port": 8780,
        "dependencies": {"database": "ok"},
    }


class ScheduledSyncTests(unittest.TestCase):
    def test_preflight_status_preserves_real_attempt_and_first_registration(self):
        existing = {
            "automation_id": "aaie-crm-google-drive",
            "carrier_kind": "launchd",
            "status": "ACTIVE",
            "registered_at": "2026-08-27T04:00:00+00:00",
            "attempted_at": "2026-08-27T13:00:00+00:00",
            "finished_at": "2026-08-27T13:02:00+00:00",
            "attempt_status": "completed",
            "reason_code": "completed",
            "exit_code": 0,
        }

        status = scheduled.build_preflight_carrier_status(
            existing=existing,
            carrier_kind="launchd",
            observed_at="2026-08-28T04:00:00+00:00",
            outcome={
                "attempted_at": "2026-08-28T04:00:00+00:00",
                "finished_at": "2026-08-28T04:00:01+00:00",
                "attempt_status": "ready",
                "reason_code": "preflight_ok",
                "exit_code": 0,
            },
        )

        self.assertEqual(status["registered_at"], "2026-08-27T04:00:00+00:00")
        self.assertEqual(status["attempted_at"], "2026-08-27T13:00:00+00:00")
        self.assertEqual(status["attempt_status"], "completed")
        self.assertEqual(status["reason_code"], "completed")
        self.assertEqual(status["preflight_status"], "ready")
        self.assertEqual(status["preflight_reason_code"], "preflight_ok")

    def test_direct_entrypoint_bootstraps_repository_root_without_pythonpath(self):
        with tempfile.TemporaryDirectory(prefix="google-drive-scheduled-entrypoint-") as folder:
            root = Path(folder)
            repository = root / "crm"
            tools = repository / "tools"
            tools.mkdir(parents=True)
            shutil.copy2(Path(scheduled.__file__), tools / "google_drive_scheduled_sync.py")
            (repository / "app.py").write_text(
                textwrap.dedent(
                    f"""
                    from pathlib import Path

                    APP_DIR = Path({str(repository)!r})
                    DB_PATH = APP_DIR / "database" / "aaie_crm.db"

                    class RuntimeStore:
                        def write_carrier_status(self, carrier):
                            return dict(carrier)

                        def read_policy(self):
                            return {{"is_enabled": False, "status": "PAUSED"}}

                    google_drive_runtime_store = RuntimeStore()

                    def build_google_drive_sync_service():
                        raise AssertionError("paused entrypoint must not construct the writer service")
                    """
                ),
                encoding="utf-8",
            )
            unrelated_cwd = root / "launchd-cwd"
            unrelated_cwd.mkdir()
            environment = {
                "HOME": str(root / "home"),
                "PATH": "/usr/bin:/bin",
                "AAIE_GOOGLE_DRIVE_CARRIER_KIND": "launchd",
            }

            for cwd in (repository, unrelated_cwd):
                with self.subTest(cwd=cwd):
                    result = subprocess.run(
                        [sys.executable, str(tools / "google_drive_scheduled_sync.py")],
                        cwd=cwd,
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )

                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn("ModuleNotFoundError", result.stderr)
                    self.assertIn('"exit_code": 0', result.stdout)

    def run_case(
        self, *, enabled=True, health=None, service=None, app_dir=EXPECTED_APP,
        db_path=EXPECTED_DB, carrier_kind="launchd", preflight_only=False,
    ):
        self.health_calls = 0
        self.factory_calls = 0
        self.outcomes = []
        service = service or Service()

        def health_probe():
            self.health_calls += 1
            if isinstance(health, Exception):
                raise health
            return healthy() if health is None else health

        def factory():
            self.factory_calls += 1
            return service

        code = scheduled.run_scheduled_sync(
            health_probe=health_probe,
            service_factory=factory,
            policy_store=PolicyStore(enabled),
            carrier_id="aaie-crm-google-drive",
            carrier_kind=carrier_kind,
            app_dir=app_dir,
            db_path=db_path,
            now=lambda: datetime(2026, 8, 27, 13, 1, 30, tzinfo=timezone.utc),
            outcome_sink=self.outcomes.append,
            preflight_only=preflight_only,
        )
        return code, service

    def test_paused_policy_is_noop_before_health_or_service(self):
        code, service = self.run_case(enabled=False)
        self.assertEqual(code, 0)
        self.assertEqual(self.health_calls, 0)
        self.assertEqual(self.factory_calls, 0)
        self.assertEqual(service.scan_calls, [])

    def test_unreachable_or_mismatched_health_fails_closed(self):
        code, _ = self.run_case(health=RuntimeError("offline"))
        self.assertEqual(code, 2)
        self.assertEqual(self.factory_calls, 0)
        self.assertEqual(self.outcomes[-1]["reason_code"], "admin_health_unreachable")
        self.assertNotIn("offline", str(self.outcomes[-1]))
        for bad in (
            {**healthy(), "service": "parent"},
            {**healthy(), "port": 8781},
            {**healthy(), "dependencies": {"database": "failed"}},
        ):
            code, _ = self.run_case(health=bad)
            self.assertEqual(code, 2)
            self.assertEqual(self.factory_calls, 0)
            self.assertEqual(self.outcomes[-1]["reason_code"], "admin_health_invalid")

    def test_wrong_local_app_or_database_path_fails_closed(self):
        code, _ = self.run_case(app_dir=Path("/wrong"))
        self.assertEqual(code, 2)

    def test_non_launchd_carrier_is_rejected_before_health_or_writer(self):
        code, service = self.run_case(carrier_kind="codex_heartbeat")

        self.assertEqual(code, 2)
        self.assertEqual(self.health_calls, 0)
        self.assertEqual(self.factory_calls, 0)
        self.assertEqual(service.scan_calls, [])
        self.assertEqual(self.outcomes[-1]["reason_code"], "carrier_environment_mismatch")
        code, _ = self.run_case(db_path=Path("/wrong.db"))
        self.assertEqual(code, 2)

    def test_invalid_connection_fails_before_scan(self):
        code, service = self.run_case(service=Service(connection="authorization_expired"))
        self.assertEqual(code, 2)
        self.assertEqual(service.scan_calls, [])
        self.assertEqual(self.outcomes[-1]["reason_code"], "drive_not_connected")

    def test_active_run_uses_scan_then_common_automatic_confirm(self):
        code, service = self.run_case()
        self.assertEqual(code, 0)
        self.assertEqual(service.scan_calls, [0])
        self.assertEqual(
            service.confirm_calls,
            [("scan-auto", "nonce-auto", ["one"], 0, "automatic")],
        )
        self.assertEqual(self.outcomes[-1]["attempt_status"], "completed")
        self.assertEqual(self.outcomes[-1]["reason_code"], "completed")

    def test_preflight_only_checks_environment_without_scan_or_confirm(self):
        code, service = self.run_case(preflight_only=True)

        self.assertEqual(code, 0)
        self.assertEqual(self.health_calls, 1)
        self.assertEqual(self.factory_calls, 1)
        self.assertEqual(service.probe_calls, 1)
        self.assertEqual(service.scan_calls, [])
        self.assertEqual(service.confirm_calls, [])
        self.assertEqual(self.outcomes[-1]["attempt_status"], "ready")
        self.assertEqual(self.outcomes[-1]["reason_code"], "preflight_ok")

    def test_preflight_validates_environment_even_when_schedule_is_paused(self):
        code, service = self.run_case(enabled=False, preflight_only=True)

        self.assertEqual(code, 0)
        self.assertEqual(self.health_calls, 1)
        self.assertEqual(self.factory_calls, 1)
        self.assertEqual(service.probe_calls, 1)
        self.assertEqual(service.scan_calls, [])
        self.assertEqual(service.confirm_calls, [])
        self.assertEqual(self.outcomes[-1]["reason_code"], "preflight_ok")

    def test_preflight_requires_bounded_drive_metadata_network_probe(self):
        code, service = self.run_case(
            preflight_only=True,
            service=Service(probe_error=RuntimeError("secret network detail")),
        )

        self.assertEqual(code, 2)
        self.assertEqual(service.probe_calls, 1)
        self.assertEqual(service.scan_calls, [])
        self.assertEqual(service.confirm_calls, [])
        self.assertEqual(self.outcomes[-1]["attempt_status"], "blocked")
        self.assertEqual(self.outcomes[-1]["reason_code"], "drive_environment_preflight_failed")
        self.assertNotIn("secret network detail", str(self.outcomes[-1]))

    def test_writer_busy_and_item_failures_have_distinct_exit_codes(self):
        code, _ = self.run_case(service=Service(report={"status": "writer_busy", "failed": 1}))
        self.assertEqual(code, 3)
        self.assertEqual(self.outcomes[-1]["attempt_status"], "writer_busy")
        code, _ = self.run_case(service=Service(report={"status": "completed_with_errors", "failed": 1}))
        self.assertEqual(code, 4)
        self.assertEqual(self.outcomes[-1]["attempt_status"], "failed")
        self.assertEqual(self.outcomes[-1]["reason_code"], "item_failures")

    def test_scan_and_confirm_exceptions_have_sanitized_failure_stages(self):
        code, _ = self.run_case(service=Service(scan_error=RuntimeError("plain scan text")))
        self.assertEqual(code, 4)
        self.assertEqual(self.outcomes[-1]["reason_code"], "scan_execution_failed")
        self.assertEqual(self.outcomes[-1]["failure_stage"], "scan_enter")
        self.assertEqual(self.outcomes[-1]["exception_type"], "RuntimeError")
        self.assertEqual(self.outcomes[-1]["sanitized_error"], "plain scan text")
        self.assertIn("scan_enter", str(self.outcomes[-1]["stage_events"]))

        code, _ = self.run_case(service=Service(confirm_error=RuntimeError("secret writer text")))
        self.assertEqual(code, 4)
        self.assertEqual(self.outcomes[-1]["reason_code"], "confirm_execution_failed")
        self.assertNotIn("secret writer text", str(self.outcomes[-1]))

    def test_disabled_schedule_records_paused_without_constructing_writer(self):
        code, service = self.run_case(enabled=False)

        self.assertEqual(code, 0)
        self.assertEqual(service.scan_calls, [])
        self.assertEqual(self.outcomes[-1]["attempt_status"], "paused")
        self.assertEqual(self.outcomes[-1]["reason_code"], "policy_disabled")
        self.assertEqual(self.outcomes[-1]["attempted_at"], "2026-08-27T13:01:30+00:00")

    def test_cli_contains_no_independent_database_writer(self):
        source = inspect.getsource(scheduled)
        self.assertNotIn("INSERT INTO", source.upper())
        self.assertNotIn("UPDATE CONTENT_", source.upper())
        self.assertIn('.confirm(', source)
        self.assertIn('"automatic"', source)

    def test_scan_failure_diagnostics_never_start_confirm_or_writer(self):
        code, service = self.run_case(service=Service(scan_error=TimeoutError("Bearer secret timeout")))

        self.assertEqual(code, 4)
        outcome = self.outcomes[-1]
        self.assertEqual(outcome["reason_code"], "scan_execution_failed")
        self.assertEqual(outcome["attempt_status"], "failed")
        self.assertTrue(outcome["attempt_id"])
        self.assertEqual(outcome["failure_stage"], "scan_enter")
        self.assertEqual(outcome["exception_type"], "TimeoutError")
        self.assertEqual(outcome["retryability"], "transient_retryable")
        self.assertTrue(outcome["traceback_fingerprint"])
        self.assertEqual(service.confirm_calls, [])
        self.assertNotIn("secret", str(outcome))


if __name__ == "__main__":
    unittest.main()
