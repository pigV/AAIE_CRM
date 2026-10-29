"""Loopback-only HTTP server for private Human Ground Truth review."""

from datetime import datetime, timezone
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from urllib.parse import unquote, urlsplit


_ASSET_CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
}
_POST_FIELDS = {"stage", "action", "text"}
_MAX_POST_BYTES = 16 * 1024


class _ReviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _ReviewRequestHandler(BaseHTTPRequestHandler):
    server_version = "GroundTruthReview/1"

    def log_message(self, _format, *_args):
        """Keep review text and the per-process token out of ordinary logs."""

    def _security_headers(self, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; media-src 'self'; script-src 'self'; "
            "style-src 'self'; connect-src 'self'; base-uri 'none'; "
            "frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self._security_headers(content_type, len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_error_code(self, status: int, code: str) -> None:
        self._send_json(status, {"error": code})

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/review-set":
            try:
                self._send_json(200, self.server.review_store.project())
            except (OSError, ValueError, json.JSONDecodeError):
                self._send_error_code(500, "review_dataset_unavailable")
            return

        if path == "/":
            name = "index.html"
        elif path in {"/app.js", "/styles.css"}:
            name = path[1:]
        else:
            name = None
        if name:
            asset = self.server.asset_root / name
            try:
                body = asset.read_bytes()
            except OSError:
                self._send_error_code(404, "asset_not_found")
                return
            if name == "index.html":
                body = body.replace(
                    b"__REVIEW_TOKEN__", self.server.review_token.encode("utf-8"),
                )
            self._send_bytes(200, body, _ASSET_CONTENT_TYPES[name])
            return

        if path.startswith("/media/"):
            filename = unquote(path[len("/media/"):])
            if not filename or Path(filename).name != filename:
                self._send_error_code(404, "media_not_found")
                return
            media = self.server.clip_map.get(filename)
            if not media:
                self._send_error_code(404, "media_not_found")
                return
            try:
                body = media.read_bytes()
            except OSError:
                self._send_error_code(404, "media_not_found")
                return
            self._send_bytes(200, body, "video/x-m4v")
            return

        self._send_error_code(404, "not_found")

    def do_POST(self):
        path = urlsplit(self.path).path
        prefix = "/api/review/"
        if not path.startswith(prefix):
            self._send_error_code(404, "not_found")
            return
        if self.headers.get("Origin") != self.server.expected_origin:
            self._send_error_code(403, "origin_rejected")
            return
        supplied_token = self.headers.get("X-Review-Token", "")
        if not hmac.compare_digest(supplied_token, self.server.review_token):
            self._send_error_code(403, "review_token_rejected")
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            self._send_error_code(400, "json_required")
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if not 0 < content_length <= _MAX_POST_BYTES:
            self._send_error_code(400, "invalid_content_length")
            return
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_error_code(400, "invalid_json")
            return
        if not isinstance(payload, dict) or set(payload) != _POST_FIELDS:
            self._send_error_code(400, "invalid_review_fields")
            return
        sample_id = unquote(path[len(prefix):])
        if not sample_id or "/" in sample_id or sample_id in {".", ".."}:
            self._send_error_code(404, "unknown_review_sample")
            return
        try:
            updated = self.server.review_store.apply(
                sample_id,
                str(payload["stage"]),
                str(payload["action"]),
                str(payload["text"]),
                datetime.now(timezone.utc).isoformat(),
            )
        except KeyError:
            self._send_error_code(404, "unknown_review_sample")
            return
        except ValueError as error:
            code = str(error)
            status = 409 if code in {"review_stage_mismatch", "review_already_complete"} else 400
            self._send_error_code(status, code)
            return
        except (OSError, json.JSONDecodeError):
            self._send_error_code(500, "review_store_unavailable")
            return
        self._send_json(200, {"sample": updated})


def build_review_server(host, port, store, asset_root, clip_map, token=None):
    """Build a server that cannot be exposed beyond IPv4 loopback."""
    if host != "127.0.0.1":
        raise ValueError("review_server_must_be_loopback")
    root = Path(asset_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError("review_asset_root_missing")
    server = _ReviewHTTPServer((host, int(port)), _ReviewRequestHandler)
    server.review_store = store
    server.asset_root = root
    server.clip_map = {
        str(name): Path(path).resolve() for name, path in dict(clip_map).items()
    }
    server.review_token = str(token or secrets.token_urlsafe(32))
    server.expected_origin = f"http://127.0.0.1:{server.server_port}"
    return server
