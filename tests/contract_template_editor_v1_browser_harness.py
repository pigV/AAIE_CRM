#!/usr/bin/env python3
"""Isolated browser harness for Contract Template Editor V1."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contract_template_editor import (
    ContractTemplateEditorService,
    render_template_editor_detail,
    render_template_editor_index,
)


def page(body: str) -> bytes:
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<link rel="stylesheet" href="/assets/style.css"><title>合同模板编辑器 V1</title>'
        f'</head><body><main class="content">{body}</main><script src="/assets/app.js"></script></body></html>'
    ).encode("utf-8")


def handler_for(database: Path, *, manager_mode: bool = False):
    class Handler(BaseHTTPRequestHandler):
        actor_user_id = 2 if manager_mode else 1
        is_admin = not manager_mode

        def service(self):
            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            return connection, ContractTemplateEditorService(connection)

        def send_bytes(self, payload: bytes, content_type: str, status=200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def redirect(self, location: str):
            self.send_response(303)
            self.send_header("Location", location)
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/assets/style.css":
                return self.send_bytes((ROOT / "assets/style.css").read_bytes(), "text/css")
            if parsed.path == "/assets/app.js":
                return self.send_bytes((ROOT / "assets/app.js").read_bytes(), "text/javascript")
            connection, service = self.service()
            try:
                if parsed.path in {"/", "/templates", "/templates/v1"}:
                    body = render_template_editor_index(
                        service.list_templates(), csrf="browser-csrf", can_edit=True,
                        can_publish=True, is_admin=self.is_admin,
                    )
                elif parsed.path.startswith("/templates/v1/"):
                    template_type = unquote(parsed.path.split("/")[3])
                    query = parse_qs(parsed.query)
                    version_id = int((query.get("version_id") or ["0"])[0] or 0) or None
                    workspace = service.workspace(template_type, version_id)
                    comparison = None
                    compare_left = int((query.get("compare_left") or ["0"])[0] or 0)
                    compare_right = int((query.get("compare_right") or ["0"])[0] or 0)
                    if compare_left and compare_right:
                        comparison = service.compare_versions(compare_left, compare_right)
                    lock = service.live_lock(
                        int(workspace["active_version"]["id"]),
                        self.actor_user_id,
                    )
                    body = render_template_editor_detail(
                        workspace, csrf="browser-csrf", can_edit=True, can_publish=True,
                        lock=lock, comparison=comparison,
                        is_admin=self.is_admin,
                    )
                else:
                    return self.send_bytes(b"not found", "text/plain", 404)
            finally:
                connection.close()
            self.send_bytes(page(body), "text/html; charset=utf-8")

        def do_POST(self):
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0") or 0)
            form = {key: values[-1] for key, values in parse_qs(self.rfile.read(length).decode("utf-8")).items()}
            parts = parsed.path.split("/")
            template_type = unquote(parts[3])
            action = parts[-1]
            connection, service = self.service()
            try:
                if action == "draft":
                    result = service.create_draft(template_type, self.actor_user_id)
                    connection.close()
                    return self.redirect(
                        f"/templates/v1/{template_type}?version_id={result['id']}"
                    )
                if action == "lock":
                    result = service.acquire_lock(
                        template_type, int(form["version_id"]), self.actor_user_id
                    )
                elif action == "autosave-block":
                    result = service.autosave_block(
                        int(form["version_id"]), form["block_key"], title=form.get("title", ""),
                        body=form.get("body", ""), enabled=form.get("enabled") == "1",
                        expected_revision=int(form["revision"]), lock_token=form["lock_token"],
                        actor_user_id=self.actor_user_id,
                    )
                elif action == "autosave-settings":
                    result = service.autosave_settings(
                        int(form["version_id"]),
                        commercial=json.loads(form.get("commercial_json", "{}")),
                        services=json.loads(form.get("services_json", "[]")),
                        expected_revision=int(form["revision"]),
                        lock_token=form["lock_token"], actor_user_id=self.actor_user_id,
                    )
                elif action == "reorder":
                    result = service.reorder_blocks(
                        int(form["version_id"]), json.loads(form["ordered_keys_json"]),
                        expected_revision=int(form["revision"]), lock_token=form["lock_token"],
                        actor_user_id=self.actor_user_id,
                    )
                elif action == "apply":
                    result = service.apply_template(
                        int(form["version_id"]), actor_user_id=self.actor_user_id,
                        lock_token=form["lock_token"],
                        expected_revision=int(form["revision"]),
                        output_root=database.parent / "browser-apply-previews",
                        actor_is_admin=self.is_admin,
                    )
                else:
                    result = {"status": "harness-noop", "action": action}
                payload = {"ok": True, **dict(result)}
                self.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json")
            except Exception as exc:
                self.send_bytes(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False).encode("utf-8"), "application/json", 400)
            finally:
                connection.close()

        def log_message(self, _format, *_args):
            return

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--port", type=int, default=8891)
    parser.add_argument("--manager", action="store_true")
    args = parser.parse_args()
    database = args.database.expanduser().resolve()
    if not database.is_file() or not database.is_relative_to((ROOT / "SAFEBOX").resolve()):
        raise SystemExit("browser harness requires a SAFEBOX rehearsal database")
    if args.manager:
        permission_connection = sqlite3.connect(database)
        permission_connection.row_factory = sqlite3.Row
        permission_service = ContractTemplateEditorService(permission_connection)
        permission_service.set_user_permission(
            2, can_edit=False, can_publish=True,
            actor_user_id=1, actor_is_admin=True,
        )
        permission_connection.close()
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port),
        handler_for(database, manager_mode=args.manager),
    )
    print(f"CONTRACT_TEMPLATE_EDITOR_BROWSER=http://127.0.0.1:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
