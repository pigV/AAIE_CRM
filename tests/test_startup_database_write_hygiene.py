import sys
import os
import json
import select
import socket
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
import unittest
from pathlib import Path
from unittest import mock


class _FakeServer:
    def __init__(self, address, handler):
        self.server_port = address[1]
        self.service_mode = None

    def serve_forever(self):
        return None

    def server_close(self):
        return None


class StartupDatabaseWriteHygieneTests(unittest.TestCase):
    _SEED_TABLES = (
        "academic_results_update_rules",
        "data_sources",
        "school_feature_categories",
        "school_feature_tags",
        "school_geocode_cache",
        "school_suburb_data",
        "school_suburb_data_sources",
        "school_suburbs",
        "user_module_permissions",
    )

    def _seed_snapshot(self, path):
        with sqlite3.connect(path) as connection:
            return {
                table: connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY 1'
                ).fetchall()
                for table in self._SEED_TABLES
            }

    def test_main_performs_one_preflight_and_marks_server_preflight_done(self):
        import app

        argv = [
            "app.py",
            "--host",
            "127.0.0.1",
            "--port",
            "19880",
            "--service",
            "admin",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(app, "ensure_dirs"),
            mock.patch.object(app.db, "initialize") as initialize,
            mock.patch.object(app, "ensure_template_management_seed") as template_seed,
            mock.patch.object(app, "run_server") as run_server,
        ):
            app.main()
        initialize.assert_called_once_with()
        template_seed.assert_called_once_with()
        run_server.assert_called_once_with(
            "127.0.0.1",
            19880,
            "admin",
            safe_production=False,
            startup_preflight_done=True,
        )

    def test_second_initialization_is_noop_for_seed_rows(self):
        from database import Database

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "startup.sqlite3"
            database = Database(path)
            with mock.patch.dict(os.environ, {"AAIE_CRM_DB_PERF_LOG": "0"}):
                database.initialize()
                before = self._seed_snapshot(path)
                with mock.patch("database.now_iso", return_value="2099-01-01T00:00:00+00:00"):
                    database.initialize()
                after = self._seed_snapshot(path)
        self.assertEqual(before, after)

    def test_direct_run_server_performs_exactly_one_preflight(self):
        import app

        with (
            mock.patch.object(app, "ensure_dirs"),
            mock.patch.object(app.db, "initialize") as initialize,
            mock.patch.object(app, "ensure_template_management_seed") as template_seed,
            mock.patch.object(
                app, "recover_interrupted_content_extraction_jobs", return_value=0
            ),
            mock.patch.object(app, "run_reminder_engine"),
            mock.patch.object(app, "start_reminder_engine_thread"),
            mock.patch.object(app, "start_data_update_auto_check_thread"),
            mock.patch.object(app, "startup_backup"),
            mock.patch.object(app, "append_service_log"),
            mock.patch.object(app, "wechat_oauth_startup_messages", return_value=[]),
            mock.patch.object(app, "crm_admin_lan_url", return_value="http://127.0.0.1"),
            mock.patch.object(app, "ReusableThreadingHTTPServer", _FakeServer),
        ):
            app.run_server(
                "127.0.0.1",
                19880,
                "admin",
                safe_production=False,
                startup_preflight_done=False,
            )
        initialize.assert_called_once_with()
        template_seed.assert_called_once_with()

    def test_admin_listener_is_ready_while_initial_reminder_scan_is_still_running(self):
        self._assert_listener_ready_during_initial_reminder_scan("admin")

    def test_parent_listener_is_ready_while_initial_reminder_scan_is_still_running(self):
        self._assert_listener_ready_during_initial_reminder_scan("parent")

    def _assert_listener_ready_during_initial_reminder_scan(self, service):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        child = Path(__file__).with_name("startup_readiness_child.py")
        process = subprocess.Popen(
            [sys.executable, str(child), str(port), service],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            ready, _, _ = select.select([process.stdout], [], [], 30)
            self.assertTrue(ready, "startup child did not finish importing app.py")
            self.assertEqual(process.stdout.readline().strip(), "PROBE_READY")
            started = time.monotonic()
            payload = None
            while time.monotonic() - started < 2.0:
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=0.25
                    ) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                        if response.status == 200 and payload.get("reminder_started"):
                            break
                except OSError:
                    time.sleep(0.05)
            self.assertIsNotNone(
                payload,
                "listener waited for the deliberately slow initial reminder scan",
            )
            self.assertEqual(payload, {"status": "ok", "reminder_started": True})
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
