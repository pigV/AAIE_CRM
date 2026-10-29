import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import formal_service_restart_control as control
import watchdog


class FakeClock:
    def __init__(self):
        self.seconds = 0.0
        self.started_at = datetime(2026, 7, 29, tzinfo=timezone.utc)

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        self.seconds += seconds

    def now_text(self):
        return (self.started_at + timedelta(seconds=self.seconds)).isoformat(
            timespec="seconds"
        )


class ReadinessScenario:
    def __init__(
        self,
        *,
        ready_at,
        service="admin",
        port=8780,
        pid=1234,
        listener_pid=None,
        health_values=None,
    ):
        self.clock = FakeClock()
        self.ready_at = ready_at
        self.service = service
        self.port = port
        self.pid = pid
        self.listener_pid = pid if listener_pid is None else listener_pid
        self.health_values = list(health_values or [])
        self.progress = []

    def pid_alive(self, pid):
        return pid == self.pid

    def listeners(self, config):
        return [self.listener_pid]

    def health(self, config, service):
        if self.health_values:
            ok = self.health_values.pop(0)
        else:
            ok = self.clock.seconds >= self.ready_at
        return {
            "ok": ok,
            "status": 200 if ok else None,
            "service": service if ok else None,
            "port": config["port"] if ok else None,
            "error": "" if ok else "connection_refused",
        }

    def progress_writer(self, payload):
        self.progress.append(dict(payload))


class WatchdogRestartReadinessTests(unittest.TestCase):
    def api(self, module, name):
        value = getattr(module, name, None)
        self.assertTrue(
            callable(value),
            f"{module.__name__}.{name} must implement the R5E protocol",
        )
        return value

    def wait_ready(self, scenario, deadline):
        wait = self.api(watchdog, "wait_for_service_ready")
        config = {
            "port": scenario.port,
            "health_url": f"http://127.0.0.1:{scenario.port}/health",
            "stdout": Path("/nonexistent/stdout.log"),
            "stderr": Path("/nonexistent/stderr.log"),
        }
        return wait(
            "req-r5e",
            scenario.service,
            config,
            scenario.pid,
            deadline_seconds=deadline,
            interval_seconds=5,
            stable_successes=3,
            monotonic=scenario.clock.monotonic,
            sleep=scenario.clock.sleep,
            now_text_func=scenario.clock.now_text,
            pid_check=scenario.pid_alive,
            listener_check=scenario.listeners,
            health_check=scenario.health,
            progress_writer=scenario.progress_writer,
            failure_evidence_builder=lambda **values: values,
        )

    def test_deadline_contract_and_three_successes_cover_real_delays(self):
        cases = (
            ("admin-30", "admin", 8780, 30, 240, True),
            ("admin-157", "admin", 8780, 157, 240, True),
            ("admin-239", "admin", 8780, 239, 240, True),
            ("admin-241", "admin", 8780, 241, 240, False),
            ("parent-41", "parent", 8781, 41, 120, True),
            ("parent-119", "parent", 8781, 119, 120, True),
            ("parent-121", "parent", 8781, 121, 120, False),
        )
        for label, service, port, ready_at, deadline, expected in cases:
            with self.subTest(label=label):
                scenario = ReadinessScenario(
                    ready_at=ready_at, service=service, port=port
                )
                result = self.wait_ready(scenario, deadline)
                self.assertEqual(result["ok"], expected)
                if expected:
                    self.assertEqual(result["stage"], "completed")
                    self.assertGreaterEqual(scenario.clock.seconds, ready_at + 10)
                else:
                    self.assertEqual(result["stage"], "failed")
                    self.assertEqual(result["failure_reason"], "ready_deadline_exceeded")

    def test_health_requires_three_consecutive_successes_and_resets(self):
        scenario = ReadinessScenario(
            ready_at=0,
            health_values=[True, True, False, True, True, True],
        )
        result = self.wait_ready(scenario, 240)
        self.assertTrue(result["ok"])
        candidates = [
            item["consecutive_successes"]
            for item in scenario.progress
            if item["stage"] == "health_candidate"
        ]
        self.assertEqual(candidates, [1, 2, 1, 2, 3])
        self.assertEqual(scenario.progress[-2]["stage"], "health_stable")
        self.assertEqual(scenario.progress[-1]["stage"], "completed")

    def test_listener_ready_and_health_waiting_are_separate_states(self):
        scenario = ReadinessScenario(ready_at=15)
        result = self.wait_ready(scenario, 240)
        self.assertTrue(result["ok"])
        stages = [item["stage"] for item in scenario.progress]
        self.assertIn("listener_ready", stages)
        self.assertIn("health_waiting", stages)
        self.assertLess(
            stages.index("listener_ready"), stages.index("health_candidate")
        )

    def test_listener_pid_mismatch_is_rejected_with_full_failure_evidence(self):
        scenario = ReadinessScenario(ready_at=0, listener_pid=9999)
        result = self.wait_ready(scenario, 240)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure_reason"], "listener_pid_mismatch")
        required = {
            "request_id",
            "service",
            "pid",
            "elapsed_seconds",
            "deadline_seconds",
            "deadline_at",
            "process_alive",
            "listener_state",
            "listener_pids",
            "last_health",
            "last_health_error",
            "consecutive_successes",
            "startup_log_tail",
            "startup_error_log_tail",
            "restart_history",
            "gate",
            "database_lock_evidence",
            "failed_stage",
        }
        self.assertTrue(required.issubset(result["failure_evidence"]))

    def test_formal_lock_rejection_does_not_consume_restart_history(self):
        config = {
            "port": 8780,
            "health_url": "http://127.0.0.1:8780/health",
            "pid_file": Path("/nonexistent/admin.pid"),
            "stdout": Path("/nonexistent/admin.log"),
            "stderr": Path("/nonexistent/admin.err.log"),
        }
        with (
            patch.object(watchdog, "write_restart_progress"),
            patch.object(watchdog, "read_pid", return_value=123),
            patch.object(watchdog, "restart_allowed") as allowed,
            patch.object(
                watchdog,
                "service_lock_path",
                return_value=Path(tempfile.gettempdir()) / "r5e-lock-test",
            ),
            patch.object(
                watchdog.fcntl,
                "flock",
                side_effect=BlockingIOError,
            ),
            patch.object(
                watchdog,
                "_formal_failure",
                return_value={"ok": False, "stage": "failed"},
            ),
        ):
            result = watchdog.restart_formal_service_until_ready(
                "req-lock", "admin", config
            )
        self.assertFalse(result["ok"])
        allowed.assert_not_called()

    def test_formal_restart_is_admin_then_parent_and_releases_mutex(self):
        execute = self.api(watchdog, "execute_formal_restart")
        calls = []

        def restart(request_id, service, config):
            calls.append(service)
            if service == "admin":
                self.assertNotIn("parent", calls)
            return {
                "ok": True,
                "stage": "completed",
                "service": service,
                "pid": 100 if service == "admin" else 200,
            }

        watchdog.FORMAL_RESTART_IN_PROGRESS.clear()
        watchdog.FAILURE_COUNTS.update({"admin": 2, "parent": 2})
        result = execute(
            "req-sequence",
            restart_service_func=restart,
            progress_writer=lambda payload: None,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(calls, ["admin", "parent"])
        self.assertEqual(watchdog.FORMAL_RESTART_IN_PROGRESS, set())
        self.assertEqual(watchdog.FAILURE_COUNTS["admin"], 0)
        self.assertEqual(watchdog.FAILURE_COUNTS["parent"], 0)

    def test_real_delay_simulation_completes_admin_before_parent(self):
        events = []
        clocks = {}

        def restart(request_id, service, config):
            ready_at = 157 if service == "admin" else 41
            deadline = 240 if service == "admin" else 120
            scenario = ReadinessScenario(
                ready_at=ready_at,
                service=service,
                port=config["port"],
                pid=100 if service == "admin" else 200,
            )
            clocks[service] = scenario.clock
            result = watchdog.wait_for_service_ready(
                request_id,
                service,
                config,
                scenario.pid,
                deadline_seconds=deadline,
                interval_seconds=5,
                stable_successes=3,
                monotonic=scenario.clock.monotonic,
                sleep=scenario.clock.sleep,
                now_text_func=scenario.clock.now_text,
                pid_check=scenario.pid_alive,
                listener_check=scenario.listeners,
                health_check=scenario.health,
                progress_writer=lambda payload: events.append(dict(payload)),
                failure_evidence_builder=lambda **values: values,
            )
            events.append(
                {
                    "request_id": request_id,
                    "stage": f"{service}_returned",
                    "service": service,
                }
            )
            return result

        result = watchdog.execute_formal_restart(
            "req-real-delays",
            restart_service_func=restart,
            progress_writer=lambda payload: events.append(dict(payload)),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "completed")
        admin_returned = next(
            index
            for index, item in enumerate(events)
            if item["stage"] == "admin_returned"
        )
        parent_started = next(
            index
            for index, item in enumerate(events)
            if item["stage"] == "parent_restarting"
        )
        self.assertLess(admin_returned, parent_started)
        for service, minimum_elapsed in (("admin", 167), ("parent", 51)):
            service_stages = [
                item["stage"]
                for item in events
                if item.get("service") == service
            ]
            self.assertIn("listener_ready", service_stages)
            self.assertIn("health_waiting", service_stages)
            self.assertIn("health_candidate", service_stages)
            self.assertIn("health_stable", service_stages)
            self.assertIn("completed", service_stages)
            self.assertGreaterEqual(clocks[service].seconds, minimum_elapsed)

    def test_parent_is_not_restarted_when_admin_never_stabilizes(self):
        execute = self.api(watchdog, "execute_formal_restart")
        calls = []

        def restart(request_id, service, config):
            calls.append(service)
            return {
                "ok": False,
                "stage": "failed",
                "service": service,
                "failure_reason": "ready_deadline_exceeded",
                "failure_evidence": {"service": service},
            }

        result = execute(
            "req-admin-fails",
            restart_service_func=restart,
            progress_writer=lambda payload: None,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(calls, ["admin"])
        self.assertEqual(watchdog.FORMAL_RESTART_IN_PROGRESS, set())

    def test_scheduled_recovery_is_suppressed_during_formal_restart(self):
        in_progress = getattr(watchdog, "FORMAL_RESTART_IN_PROGRESS", None)
        self.assertIsInstance(
            in_progress,
            set,
            "watchdog.FORMAL_RESTART_IN_PROGRESS must track the R5E mutex",
        )
        in_progress.clear()
        in_progress.add("admin")
        watchdog.FAILURE_COUNTS["admin"] = 2
        config = watchdog.SERVICES["admin"]
        with (
            patch.object(watchdog, "health_ok") as health,
            patch.object(watchdog, "restart_allowed") as allowed,
            patch.object(watchdog, "restart_service") as restart,
        ):
            self.assertFalse(
                watchdog.ensure_service(
                    "admin", config, "scheduled_health_check"
                )
            )
        health.assert_not_called()
        allowed.assert_not_called()
        restart.assert_not_called()
        self.assertEqual(watchdog.FAILURE_COUNTS["admin"], 0)
        in_progress.clear()

    def test_restart_history_counts_actual_start_once_and_prunes_window(self):
        watchdog.RESTART_HISTORY.clear()
        clock = FakeClock()
        with (
            patch.object(watchdog.time, "monotonic", clock.monotonic),
            patch.object(watchdog, "log_event"),
        ):
            for _ in range(5):
                self.assertTrue(watchdog.restart_allowed("admin", "test"))
            self.assertFalse(watchdog.restart_allowed("admin", "test"))
            self.assertEqual(len(watchdog.RESTART_HISTORY["admin"]), 5)
            clock.sleep(601)
            self.assertTrue(watchdog.restart_allowed("admin", "test"))
            self.assertEqual(len(watchdog.RESTART_HISTORY["admin"]), 1)

    def test_progress_marker_has_stable_auditable_fields(self):
        build = self.api(watchdog, "restart_progress_payload")
        payload = build(
            "req-progress",
            "health_candidate",
            service="admin",
            pid=42,
            deadline_at="2026-07-29T08:00:00+10:00",
            last_health={"ok": True, "status": 200},
            consecutive_successes=2,
            failure_reason="",
        )
        required = {
            "request_id",
            "stage",
            "recorded_at",
            "service",
            "pid",
            "deadline_at",
            "last_health",
            "consecutive_successes",
            "failure_reason",
        }
        self.assertTrue(required.issubset(payload))
        self.assertEqual(payload["request_id"], "req-progress")

    def test_every_readiness_stage_keeps_current_request_id(self):
        scenario = ReadinessScenario(
            ready_at=0,
            health_values=[False, True, True, True],
        )
        result = self.wait_ready(scenario, 240)
        self.assertTrue(result["ok"])
        self.assertTrue(scenario.progress)
        self.assertTrue(
            all(
                item["request_id"] == "req-r5e"
                for item in scenario.progress
            )
        )

    def test_health_probe_requires_http_json_service_and_port_identity(self):
        class Response:
            def __init__(self, status, payload):
                self.status = status
                self._stream = BytesIO(json.dumps(payload).encode())

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                return self._stream.read(size)

        config = {
            "health_url": "http://127.0.0.1:8780/health",
            "port": 8780,
        }
        cases = (
            (200, {"service": "admin", "port": 8780}, True),
            (200, {"service": "parent", "port": 8780}, False),
            (200, {"service": "admin", "port": 8781}, False),
            (503, {"service": "admin", "port": 8780}, False),
        )
        for status, payload, expected in cases:
            with (
                self.subTest(status=status, payload=payload),
                patch.object(
                    watchdog.urllib.request,
                    "urlopen",
                    return_value=Response(status, payload),
                ),
            ):
                result = watchdog.health_probe(config, "admin")
                self.assertEqual(result["ok"], expected)

    def test_failure_log_tails_are_bounded_without_file_contents_elsewhere(self):
        with tempfile.TemporaryDirectory() as root:
            log = Path(root) / "startup.log"
            log.write_text(
                "\n".join(f"line-{number:03d}-abcdefghij" for number in range(100)),
                encoding="utf-8",
            )
            tail = watchdog.tail_text(log, lines=40, max_chars=120)
        self.assertLessEqual(len("\n".join(tail)), 120)
        self.assertIn("line-099", tail[-1])
        self.assertNotIn("line-000", "\n".join(tail))

    def test_helper_default_timeout_progress_and_stable_timeout_error(self):
        default_timeout = getattr(
            control, "DEFAULT_RESTART_TIMEOUT_SECONDS", None
        )
        self.assertIsInstance(
            default_timeout,
            int,
            "formal control must expose a stable helper timeout",
        )
        self.assertGreaterEqual(default_timeout, 600)
        paths = control.RestartPaths(Path("/tmp/r5e"), Path("/tmp/r5e-state"))
        outcomes = [
            {
                "state": "admin_restarting",
                "marker": {
                    "request_id": "req-helper",
                    "stage": "admin_restarting",
                },
            },
            {
                "state": "completed",
                "marker": {
                    "request_id": "req-helper",
                    "stage": "completed",
                },
            },
        ]
        with patch.object(
            control, "read_request_outcome", side_effect=outcomes
        ):
            progress = control.wait_for_request_outcome(
                paths,
                "req-helper",
                timeout_seconds=600,
                monotonic=lambda: 0,
                sleep=lambda seconds: None,
            )
        self.assertEqual(progress["state"], "completed")
        self.assertEqual(
            [item["stage"] for item in progress["progress"]],
            ["admin_restarting", "completed"],
        )

        clock = FakeClock()
        with patch.object(
            control,
            "read_request_outcome",
            return_value={"state": "pending", "marker": None},
        ):
            timed_out = control.wait_for_request_outcome(
                paths,
                "req-timeout",
                timeout_seconds=2,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
            )
        self.assertEqual(timed_out["state"], "timeout")
        self.assertEqual(
            timed_out["error"], "timeout_waiting_for_watchdog"
        )
        self.assertEqual(timed_out["request_id"], "req-timeout")


if __name__ == "__main__":
    unittest.main()
