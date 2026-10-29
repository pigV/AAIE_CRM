import unittest
from datetime import datetime, timedelta, timezone

from system_health_authority import (
    NAS_RECOVERY_STALE_AFTER_SECONDS,
    parse_launchctl_disabled,
    parse_launchctl_job,
    resolve_system_health,
)


NOW = datetime(2026, 8, 27, 3, 30, 0, tzinfo=timezone.utc)


def launch_agent(*, state="running", pid=101, loaded=True, enabled=True, last_exit_code=None):
    return {
        "loaded": loaded,
        "enabled": enabled,
        "state": state,
        "pid": pid,
        "last_exit_code": last_exit_code,
        "runs": 12,
        "interval_seconds": 30,
    }


def service(*, online=True, state="running", pid=101, listeners=(101,), enabled=True, loaded=True):
    return {
        "launch_agent": launch_agent(
            state=state, pid=pid, enabled=enabled, loaded=loaded
        ),
        "listener_pids": listeners,
        "health": {
            "online": online,
            "status": 200 if online else 0,
            "data": {"version": "2.0"},
            "error": "" if online else "connection refused",
        },
        "uptime": "5分钟",
    }


def nas_recovery(*, age_seconds=20, state="not running", enabled=True, loaded=True):
    return {
        "launch_agent": launch_agent(
            state=state,
            pid=None,
            enabled=enabled,
            loaded=loaded,
            last_exit_code=0,
        ),
        "heartbeat": {
            "path": "/Users/test/Library/Application Support/AAIE CRM/nas-recovery/state.json",
            "last_run_at": (NOW - timedelta(seconds=age_seconds)).isoformat(),
            "last_success_at": (NOW - timedelta(seconds=age_seconds)).isoformat(),
            "last_result": "healthy",
            "error": "",
        },
    }


def external_link(*, online=True):
    return {
        "online": online,
        "status": 200 if online else 0,
        "url": "https://example.invalid/parent-form/test",
        "error": "" if online else "timeout",
    }


class SystemHealthAuthorityTests(unittest.TestCase):
    def snapshot(self, **overrides):
        values = {
            "admin_observation": service(pid=24425, listeners=(24425,)),
            "parent_observation": service(pid=24411, listeners=(24411,)),
            "nas_recovery_observation": nas_recovery(),
            "content_extraction_observation": service(pid=13288, listeners=(13288,)),
            "external_link": external_link(),
            "data_mode": "test",
            "legacy_watchdog": {"online": False, "pid": "1560"},
            "now": NOW,
        }
        values.update(overrides)
        return resolve_system_health(**values)

    def test_between_cycle_not_running_is_healthy_with_fresh_heartbeat(self):
        snapshot = self.snapshot()

        self.assertTrue(snapshot["monitor_ok"])
        self.assertTrue(snapshot["nas_recovery"]["online"])
        self.assertEqual(snapshot["nas_recovery"]["launch_agent_state"], "not running")
        self.assertFalse(snapshot["nas_recovery"]["running_now"])
        self.assertEqual(snapshot["headline"], "系统正常")

    def test_heartbeat_older_than_90_seconds_is_stale_and_warns(self):
        snapshot = self.snapshot(
            nas_recovery_observation=nas_recovery(
                age_seconds=NAS_RECOVERY_STALE_AFTER_SECONDS + 1
            )
        )

        self.assertTrue(snapshot["services_ok"])
        self.assertFalse(snapshot["monitor_ok"])
        self.assertTrue(snapshot["nas_recovery"]["stale"])
        self.assertEqual(snapshot["headline"], "服务在线，自动恢复异常")

    def test_exact_stale_threshold_remains_healthy(self):
        snapshot = self.snapshot(
            nas_recovery_observation=nas_recovery(
                age_seconds=NAS_RECOVERY_STALE_AFTER_SECONDS
            )
        )

        self.assertFalse(snapshot["nas_recovery"]["stale"])
        self.assertTrue(snapshot["monitor_ok"])

    def test_fractional_age_over_90_seconds_is_stale(self):
        heartbeat = nas_recovery(age_seconds=NAS_RECOVERY_STALE_AFTER_SECONDS)
        heartbeat["heartbeat"]["last_run_at"] = (
            NOW - timedelta(seconds=NAS_RECOVERY_STALE_AFTER_SECONDS, milliseconds=1)
        ).isoformat()

        snapshot = self.snapshot(nas_recovery_observation=heartbeat)

        self.assertTrue(snapshot["nas_recovery"]["stale"])
        self.assertFalse(snapshot["monitor_ok"])

    def test_admin_service_failure_is_not_reported_as_monitor_failure(self):
        snapshot = self.snapshot(admin_observation=service(online=False, pid=24425))

        self.assertFalse(snapshot["services_ok"])
        self.assertTrue(snapshot["monitor_ok"])
        self.assertEqual(snapshot["headline"], "服务异常")

    def test_non_object_health_payload_fails_closed_without_crashing_resolver(self):
        observation = service()
        observation["health"] = {
            "online": False,
            "status": 200,
            "data": ["malformed"],
            "error": "health payload is not an object",
        }

        snapshot = self.snapshot(admin_observation=observation)

        self.assertFalse(snapshot["admin"]["online"])
        self.assertEqual(snapshot["headline"], "服务异常")

    def test_long_running_service_requires_launchagent_running_state(self):
        snapshot = self.snapshot(
            admin_observation=service(
                online=True,
                state="not running",
                pid=None,
                listeners=(24425,),
            )
        )

        self.assertFalse(snapshot["admin"]["online"])
        self.assertFalse(snapshot["services_ok"])
        self.assertEqual(snapshot["headline"], "服务异常")

    def test_external_parent_link_failure_is_separate_from_monitor(self):
        snapshot = self.snapshot(external_link=external_link(online=False))

        self.assertTrue(snapshot["services_ok"])
        self.assertTrue(snapshot["monitor_ok"])
        self.assertFalse(snapshot["external_link_ok"])
        self.assertEqual(snapshot["headline"], "核心服务正常，外部家长链接异常")
        self.assertNotIn("监控", snapshot["headline"])

    def test_legacy_watchdog_never_controls_current_health(self):
        snapshot = self.snapshot(legacy_watchdog={"online": False, "pid": "1560"})

        self.assertTrue(snapshot["core_ok"])
        self.assertTrue(snapshot["overall_ok"])
        self.assertFalse(snapshot["legacy_watchdog"]["online"])

    def test_launchagent_pid_is_authoritative_then_listener_is_fallback(self):
        snapshot = self.snapshot(
            admin_observation=service(pid=24425, listeners=(99999,)),
            parent_observation=service(pid=None, listeners=(24411,)),
        )

        self.assertEqual(snapshot["admin"]["pid"], "24425")
        self.assertEqual(snapshot["admin"]["pid_source"], "LaunchAgent")
        self.assertEqual(snapshot["parent"]["pid"], "24411")
        self.assertEqual(snapshot["parent"]["pid_source"], "listener")

    def test_service_without_launchagent_or_listener_pid_is_not_authoritative(self):
        snapshot = self.snapshot(
            admin_observation=service(pid=None, listeners=()),
        )

        self.assertFalse(snapshot["admin"]["online"])
        self.assertEqual(snapshot["admin"]["pid_source"], "unavailable")
        self.assertIn("PID", snapshot["admin"]["error"])

    def test_launchagent_probe_error_is_preserved_for_diagnostics(self):
        observation = service(enabled=False)
        observation["launch_agent"]["error"] = "LaunchAgent enabled state unavailable"

        snapshot = self.snapshot(admin_observation=observation)

        self.assertFalse(snapshot["admin"]["online"])
        self.assertEqual(
            snapshot["admin"]["error"],
            "LaunchAgent enabled state unavailable",
        )

    def test_content_extraction_failure_is_monitor_failure_not_service_failure(self):
        snapshot = self.snapshot(
            content_extraction_observation=service(online=False, pid=13288)
        )

        self.assertTrue(snapshot["services_ok"])
        self.assertFalse(snapshot["monitor_ok"])
        self.assertFalse(snapshot["content_extraction"]["online"])
        self.assertEqual(snapshot["headline"], "服务在线，自动恢复异常")

    def test_test_mode_is_data_mode_not_a_separate_runtime(self):
        snapshot = self.snapshot(data_mode="test")

        self.assertEqual(snapshot["data_mode"]["key"], "TEST")
        self.assertEqual(snapshot["data_mode"]["label"], "TEST MODE")
        self.assertFalse(snapshot["data_mode"]["separate_runtime"])
        self.assertIn("数据/NAS模式", snapshot["data_mode"]["description"])

    def test_production_mode_resolver_is_explicit(self):
        snapshot = self.snapshot(data_mode="production")

        self.assertEqual(snapshot["data_mode"]["key"], "PRODUCTION")
        self.assertEqual(snapshot["data_mode"]["label"], "Production Mode")
        self.assertFalse(snapshot["data_mode"]["separate_runtime"])

    def test_launchctl_parser_extracts_running_and_one_shot_states(self):
        running = parse_launchctl_job(
            "state = running\nruns = 4\npid = 24425\nlast exit code = (never exited)\n"
        )
        one_shot = parse_launchctl_job(
            "state = not running\nruns = 121\nlast exit code = 0\nrun interval = 30 seconds\n"
        )

        self.assertEqual(running["pid"], 24425)
        self.assertEqual(running["state"], "running")
        self.assertEqual(one_shot["state"], "not running")
        self.assertEqual(one_shot["last_exit_code"], 0)
        self.assertEqual(one_shot["interval_seconds"], 30)

    def test_launchctl_disabled_parser_defaults_to_enabled(self):
        text = '"com.aaie.crm.watchdog" => disabled\n"com.aaie.crm.production.admin" => enabled\n'

        self.assertFalse(parse_launchctl_disabled(text, "com.aaie.crm.watchdog"))
        self.assertTrue(parse_launchctl_disabled(text, "com.aaie.crm.production.admin"))
        self.assertTrue(parse_launchctl_disabled(text, "com.aaie.crm.nas-recovery"))


if __name__ == "__main__":
    unittest.main()
