#!/usr/bin/env python3
from __future__ import annotations

import argparse
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google_drive_sync_views import render_google_drive_workspace


def workspace_html() -> str:
    status = {
        "connection_code": "connected",
        "connection_label": "已连接",
        "service_code": "abnormal",
        "service_label": "异常",
        "scheduler_health_code": "blocked",
        "scheduler_health_label": "最近自动任务被阻止",
        "carrier_status": "ACTIVE",
        "policy_status": "RUNNING",
        "is_enabled": True,
        "current_plan": "每天 23:00，Australia/Melbourne",
        "last_success_at": "2026-08-15T23:34:30+10:00",
        "last_scan_at": "2026-08-15T23:34:30+10:00",
        "last_attempt_at": "2026-08-26T23:01:35+10:00",
        "last_expected_run_at": "2026-08-26T23:00:00+10:00",
        "next_run_at": "2026-08-27T23:00:00+10:00",
        "last_attempt_reason": "无法连接 Admin 健康检查",
        "last_result": "运行完成",
        "last_error": "无",
    }
    return render_google_drive_workspace(
        status=status,
        preview=None,
        report=None,
        logs=[],
        csrf="browser-harness",
        permissions={"can_control": True, "can_scan": True, "can_confirm": True},
    )


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            body = (
                "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<link rel='stylesheet' href='/assets/style.css'></head><body>"
                + workspace_html()
                + "</body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/assets/"):
            path = ROOT / self.path.lstrip("/").split("?", 1)[0]
            try:
                path.relative_to(ROOT / "assets")
            except ValueError:
                self.send_error(404)
                return
            if path.is_file():
                body = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        self.send_error(404)

    def log_message(self, _format, *_args):
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18827)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
