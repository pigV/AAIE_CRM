#!/usr/bin/env python3
"""Subprocess fixture for the Production-like listener readiness regression."""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


REMINDER_STARTED = threading.Event()


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        payload = json.dumps(
            {"status": "ok", "reminder_started": REMINDER_STARTED.is_set()}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format, *_args):
        return


def slow_initial_reminder_scan():
    REMINDER_STARTED.set()
    time.sleep(5)


def main() -> None:
    port = int(sys.argv[1])
    service = sys.argv[2] if len(sys.argv) > 2 else "admin"
    print("PROBE_READY", flush=True)
    with (
        mock.patch.object(app, "CRMHandler", HealthHandler),
        mock.patch.object(app, "ensure_dirs"),
        mock.patch.object(
            app, "recover_interrupted_content_extraction_jobs", return_value=0
        ),
        mock.patch.object(app, "run_reminder_engine", slow_initial_reminder_scan),
        mock.patch.object(app, "start_data_update_auto_check_thread"),
        mock.patch.object(app, "startup_backup"),
        mock.patch.object(app, "append_service_log"),
        mock.patch.object(app, "wechat_oauth_startup_messages", return_value=[]),
        mock.patch.object(app, "crm_admin_lan_url", return_value="http://127.0.0.1"),
        mock.patch("builtins.print"),
    ):
        app.run_server(
            "127.0.0.1",
            port,
            service,
            safe_production=False,
            startup_preflight_done=True,
        )


if __name__ == "__main__":
    main()
