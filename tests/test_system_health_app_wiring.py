import unittest
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import app


class SystemHealthAppWiringTests(unittest.TestCase):
    def test_public_parent_link_uses_canonical_health_endpoint(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = (
            b'{"status":"ok","service":"parent","port":8781}'
        )

        with (
            mock.patch.object(
                app,
                "parent_form_base_url",
                return_value="https://form.goabroad.com.au",
            ),
            mock.patch.object(
                app.urllib.request,
                "urlopen",
                return_value=response,
            ) as urlopen,
        ):
            result = app.public_link_check()

        request = urlopen.call_args.args[0]
        self.assertIsInstance(request, app.urllib.request.Request)
        self.assertEqual(
            request.full_url,
            "https://form.goabroad.com.au/health",
        )
        self.assertEqual(
            request.get_header("User-agent"),
            "AAIECRM-Parent-Health/1.0",
        )
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 2.5})
        self.assertTrue(result["online"])
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["url"], "https://form.goabroad.com.au/health")

    def test_public_parent_link_rejects_wrong_health_identity(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = (
            b'{"status":"ok","service":"admin","port":8780}'
        )

        with (
            mock.patch.object(
                app,
                "parent_form_base_url",
                return_value="https://form.goabroad.com.au",
            ),
            mock.patch.object(
                app.urllib.request,
                "urlopen",
                return_value=response,
            ),
        ):
            result = app.public_link_check()

        self.assertFalse(result["online"])
        self.assertIn("identity mismatch", result["error"])

    def test_public_parent_link_does_not_treat_http_errors_as_healthy(self):
        for status, reason in ((404, "Not Found"), (410, "Gone")):
            with self.subTest(status=status):
                error = app.urllib.error.HTTPError(
                    "https://form.goabroad.com.au/health",
                    status,
                    reason,
                    hdrs=None,
                    fp=None,
                )
                with (
                    mock.patch.object(
                        app,
                        "parent_form_base_url",
                        return_value="https://form.goabroad.com.au",
                    ),
                    mock.patch.object(
                        app.urllib.request,
                        "urlopen",
                        side_effect=error,
                    ),
                ):
                    result = app.public_link_check()

                self.assertFalse(result["online"])
                self.assertEqual(result["status"], status)

    def test_authoritative_health_rejects_wrong_service_or_degraded_8792(self):
        wrong_admin = {
            "online": True,
            "status": 200,
            "data": {"status": "ok", "service": "parent", "port": 8780},
            "error": "",
        }
        degraded_content = {
            "online": True,
            "status": 200,
            "data": {
                "ok": False,
                "service_name": "content_extraction",
                "port": 8792,
                "source_mode": "production",
            },
            "error": "",
        }

        with mock.patch.object(app, "local_health_check", return_value=wrong_admin):
            result = app.authoritative_health_check(
                "http://127.0.0.1:8780/health",
                {"status": "ok", "service": "admin", "port": 8780},
            )
        self.assertFalse(result["online"])
        self.assertIn("identity mismatch", result["error"])

        with mock.patch.object(app, "local_health_check", return_value={
            "online": True, "status": 200, "data": ["not", "an", "object"], "error": "",
        }):
            result = app.authoritative_health_check(
                "http://127.0.0.1:8780/health",
                {"status": "ok", "service": "admin", "port": 8780},
            )
        self.assertFalse(result["online"])
        self.assertIn("not an object", result["error"])
        self.assertEqual(result["data"], {})

        with mock.patch.object(app, "local_health_check", return_value=degraded_content):
            result = app.authoritative_health_check(
                "http://127.0.0.1:8792/lifecycle-health",
                {
                    "ok": True,
                    "service_name": "content_extraction",
                    "port": 8792,
                    "source_mode": "production",
                },
            )
        self.assertFalse(result["online"])
        self.assertIn("identity mismatch", result["error"])

    def test_launchagent_enabled_probe_fails_closed_when_print_disabled_fails(self):
        responses = [
            subprocess.CompletedProcess([], 1, "", "permission denied"),
            subprocess.CompletedProcess([], 0, "state = running\npid = 24425\n", ""),
        ]
        with mock.patch.object(app.subprocess, "run", side_effect=responses):
            status = app.launch_agent_status(app.ADMIN_LAUNCH_AGENT_LABEL)

        self.assertTrue(status["loaded"])
        self.assertFalse(status["enabled"])
        self.assertIn("enabled state unavailable", status["error"])

    def test_snapshot_uses_launchagents_and_heartbeat_not_legacy_watchdog(self):
        jobs = {
            app.ADMIN_LAUNCH_AGENT_LABEL: {
                "loaded": True, "enabled": True, "state": "running", "pid": 24425,
            },
            app.PARENT_LAUNCH_AGENT_LABEL: {
                "loaded": True, "enabled": True, "state": "running", "pid": 24411,
            },
            app.NAS_RECOVERY_LAUNCH_AGENT_LABEL: {
                "loaded": True, "enabled": True, "state": "not running", "pid": None,
                "last_exit_code": 0, "runs": 44, "interval_seconds": 30,
            },
            app.CONTENT_EXTRACTION_LAUNCH_AGENT_LABEL: {
                "loaded": True, "enabled": True, "state": "running", "pid": 13288,
            },
        }
        health = {
            "http://127.0.0.1:8780/health": {
                "online": True, "status": 200, "data": {
                    "status": "ok", "service": "admin", "port": 8780,
                    "version": "test",
                }, "error": "",
            },
            "http://127.0.0.1:8781/health": {
                "online": True, "status": 200, "data": {
                    "status": "ok", "service": "parent", "port": 8781,
                    "version": "test",
                }, "error": "",
            },
            "http://127.0.0.1:8792/lifecycle-health": {
                "online": True, "status": 200, "data": {
                    "ok": True, "service_name": "content_extraction", "port": 8792,
                    "source_mode": "production",
                }, "error": "",
            },
        }
        heartbeat = {
            "path": str(app.NAS_RECOVERY_STATE_PATH),
            "last_run_at": "2026-08-27T03:29:40+00:00",
            "last_success_at": "2026-08-27T03:29:40+00:00",
            "last_result": "healthy",
            "error": "",
        }
        enabled_snapshot = {"known": True, "text": "all enabled", "error": ""}
        received_enabled_snapshots = []

        def launch_status(label, **kwargs):
            received_enabled_snapshots.append(kwargs.get("enabled_snapshot"))
            return jobs[label]

        with (
            mock.patch.object(
                app,
                "launch_agent_enabled_snapshot",
                return_value=enabled_snapshot,
            ) as enabled_probe,
            mock.patch.object(app, "launch_agent_status", side_effect=launch_status),
            mock.patch.object(app, "listener_pids_for_port", side_effect=lambda port: {
                8780: (24425,), 8781: (24411,), 8792: (13288,),
            }[port]),
            mock.patch.object(app, "local_health_check", side_effect=lambda url: health[url]),
            mock.patch.object(app, "nas_recovery_heartbeat", return_value=heartbeat),
            mock.patch.object(app, "public_link_check", return_value={
                "online": True, "status": 200, "url": "https://example.invalid", "error": "",
            }),
            mock.patch.object(app.nas, "status", return_value={"mode": "test"}),
            mock.patch.object(app, "watchdog_pid_alive", return_value=(False, "1560")),
            mock.patch.object(app, "watchdog_lock_holder_pid", return_value=""),
            mock.patch.object(app, "watchdog_log_lines", return_value=[]),
            mock.patch.object(app, "latest_service_start", return_value=None),
            mock.patch.object(app, "health_snapshot_now", return_value=datetime(
                2026, 8, 27, 3, 30, 0, tzinfo=timezone.utc
            )),
        ):
            snapshot = app.system_health_snapshot()

        self.assertTrue(snapshot["overall_ok"])
        self.assertTrue(snapshot["monitor_ok"])
        self.assertEqual(snapshot["admin"]["pid"], "24425")
        self.assertEqual(snapshot["parent"]["pid"], "24411")
        self.assertFalse(snapshot["legacy_watchdog"]["online"])
        self.assertEqual(snapshot["data_mode"]["key"], "TEST")
        enabled_probe.assert_called_once_with()
        self.assertEqual(received_enabled_snapshots, [enabled_snapshot] * 4)

    def test_ui_copy_exposes_each_authority_and_data_mode_semantics(self):
        source = Path("app.py").read_text(encoding="utf-8")

        for expected in (
            "Admin LaunchAgent + 8780 /health",
            "Parent LaunchAgent + 8781 /health",
            "NAS Recovery",
            "Content Extraction 8792",
            "Data Mode",
            "不是独立 runtime",
            "外部家长表单链接",
            "Legacy watchdog（历史兼容）",
        ):
            self.assertIn(expected, source)
        self.assertNotIn('headline = "服务在线，监控未运行"', source)
        self.assertNotIn(
            'overall_ok = admin_health["online"] and parent_health["online"] and watchdog_alive and public_link["online"]',
            source,
        )


if __name__ == "__main__":
    unittest.main()
