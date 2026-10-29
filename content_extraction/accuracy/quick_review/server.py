"""Loopback-only HTTP service for private SAFEBOX 181 Quick Review."""

from datetime import datetime, timezone
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from urllib.parse import unquote, urlsplit


_ASSET_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
}
_POST_FIELDS = {
    "source_queue_hash", "expected_revision", "action", "selected_text",
    "candidate_references", "reviewer_source",
}
_MAX_POST_BYTES = 16 * 1024


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Handler(BaseHTTPRequestHandler):
    server_version = "AccurateTranscriptQuickReview/1"

    def log_message(self, _format, *_args):
        """Do not write transcript, token, or resolution text to ordinary logs."""

    def _headers(self, content_type, length):
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; media-src 'self'; script-src 'self'; "
            "style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send(self, status, body, content_type):
        self.send_response(status)
        self._headers(content_type, len(body))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status, code):
        self._json(status, {"error": code})

    def _queue_projection(self):
        queue = self.server.queue_store.load()
        state = self.server.resolution_store.load()
        resolutions = {item["review_id"]: item for item in state["resolutions"]}
        items = []
        for item in queue.items:
            payload = item.to_dict()
            name = Path(item.video_clip_path).name
            payload["clip_url"] = f"/media/{name}" if name in self.server.clip_map else ""
            payload.pop("audio_clip_path", None)
            payload.pop("video_clip_path", None)
            resolution = resolutions.get(item.review_id)
            payload["status"] = resolution["status"] if resolution else "PENDING"
            payload["selected_text"] = resolution["selected_text"] if resolution else ""
            items.append(payload)
        return {
            "schema_version": queue.schema_version,
            "queue_version": queue.queue_version,
            "queue_hash": queue.queue_hash,
            "source_video_id": queue.source_video_id,
            "revision": int(state.get("revision", 0)),
            "items": items,
        }

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/queue":
            try:
                self._json(200, self._queue_projection())
            except (OSError, ValueError, json.JSONDecodeError):
                self._error(500, "quick_review_store_unavailable")
            return
        name = "index.html" if path == "/" else path[1:] if path in {"/app.js", "/styles.css"} else None
        if name:
            try:
                body = (self.server.asset_root / name).read_bytes()
            except OSError:
                self._error(404, "asset_not_found")
                return
            if name == "index.html":
                body = body.replace(b"__REVIEW_TOKEN__", self.server.review_token.encode("utf-8"))
            self._send(200, body, _ASSET_TYPES[name])
            return
        if path.startswith("/media/"):
            name = unquote(path[len("/media/"):])
            if not name or Path(name).name != name or name not in self.server.clip_map:
                self._error(404, "media_not_found")
                return
            try:
                body = self.server.clip_map[name].read_bytes()
            except OSError:
                self._error(404, "media_not_found")
                return
            self._send(200, body, "video/mp4")
            return
        self._error(404, "not_found")

    def do_POST(self):
        path = urlsplit(self.path).path
        prefix = "/api/review/"
        if not path.startswith(prefix):
            self._error(404, "not_found")
            return
        if self.headers.get("Origin") != self.server.expected_origin:
            self._error(403, "origin_rejected")
            return
        if not hmac.compare_digest(self.headers.get("X-Review-Token", ""), self.server.review_token):
            self._error(403, "review_token_rejected")
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            self._error(400, "json_required")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= _MAX_POST_BYTES:
            self._error(400, "invalid_content_length")
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._error(400, "invalid_json")
            return
        if not isinstance(payload, dict) or set(payload) != _POST_FIELDS:
            self._error(400, "invalid_review_fields")
            return
        review_id = unquote(path[len(prefix):])
        if not review_id or "/" in review_id or review_id in {".", ".."}:
            self._error(404, "unknown_review_id")
            return
        before = self.server.resolution_store.load()
        existed = any(item.get("review_id") == review_id for item in before["resolutions"])
        try:
            resolution = self.server.resolution_store.submit(
                review_id, str(payload["source_queue_hash"]), int(payload["expected_revision"]),
                str(payload["action"]), str(payload["selected_text"]),
                tuple(payload["candidate_references"]), str(payload["reviewer_source"]),
                datetime.now(timezone.utc).isoformat(),
            )
        except KeyError:
            self._error(404, "unknown_review_id")
            return
        except ValueError as error:
            code = str(error)
            status = 409 if code in {"STALE_RESOLUTION", "revision_conflict", "resolution_conflict"} else 400
            self._error(status, code)
            return
        except (OSError, json.JSONDecodeError):
            self._error(500, "quick_review_store_unavailable")
            return
        self._json(200, {"resolution": resolution.to_dict(), "idempotent": existed})


def build_quick_review_server(host, port, queue_store, resolution_store,
                              asset_root, clip_map, token=None):
    if host != "127.0.0.1":
        raise ValueError("quick_review_server_must_be_loopback")
    root = Path(asset_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError("quick_review_asset_root_missing")
    server = _Server((host, int(port)), _Handler)
    server.queue_store = queue_store
    server.resolution_store = resolution_store
    server.asset_root = root
    server.clip_map = {str(name): Path(path).resolve() for name, path in dict(clip_map).items()}
    server.review_token = str(token or secrets.token_urlsafe(32))
    server.expected_origin = f"http://127.0.0.1:{server.server_port}"
    return server
