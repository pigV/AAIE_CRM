"""Local-only browser harness for the Accurate Transcript CRM review surface.

This is acceptance-test infrastructure only.  It never writes review resolutions
and deliberately serves one synthetic, path-free review projection.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[1]


REVIEW = {
    "queue_hash": "browser-acceptance-only",
    "revision": 0,
    "items": [
        {
            "review_id": "review-browser-1",
            "clip_id": "clip-browser-1.mp4",
            "status": "PENDING",
            "start_ms": 1200,
            "end_ms": 4650,
            "carrier_text": "孩子最好在10岁以前进入英语环境VISA",
            "start_offset": 5,
            "end_offset": 10,
            "context_before": "请听取前后语境后确认。",
            "context_after": "只回答视频里实际说了什么。",
            "alternatives": [
                {"text": "10岁以前"},
                {"text": "十岁以前"},
            ],
            "evidence": {"risk_level": "P0", "reason_for_review": "关键数字存在差异"},
        }
    ],
}


PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>确认文案 · Browser Acceptance</title>
<link rel="stylesheet" href="/assets/style.css">
<link rel="stylesheet" href="/assets/ui-premium-v2.css"></head>
<body class="ui-premium-v2"><div class="app-shell">
<aside class="sidebar" id="app-sidebar"><div class="brand"><strong>AAIE CRM</strong></div></aside>
<main class="main">
<section class="content-accurate-review-page" data-accurate-review
 data-review-url="/data" data-submit-base="/submit"
 data-media-base="/media" data-csrf="browser-acceptance-only">
 <div class="content-workspace-title">
  <div><span class="eyebrow">Accurate Transcript Review</span><h1>确认文案</h1>
  <p>这里只显示机器仍无法安全确认的位置。</p></div>
  <a class="secondary button-link" href="#task">返回提取任务</a>
 </div>
 <div class="content-accurate-review-progress" data-review-progress>正在读取审核进度…</div>
 <div class="content-accurate-review-items" data-review-items></div>
 <details class="content-accurate-review-completed" data-review-completed hidden>
  <summary data-review-completed-summary>已完成 0 处</summary>
  <div class="content-accurate-review-items" data-review-completed-items></div>
 </details>
 <div class="content-accurate-review-status" data-review-status aria-live="polite"></div>
</section></main></div><script src="/assets/app.js"></script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib callback name
        if self.path == "/":
            return self._send(PAGE.encode(), "text/html; charset=utf-8")
        if self.path == "/data":
            return self._send(json.dumps(REVIEW).encode(), "application/json")
        if self.path == "/assets/app.js":
            return self._send((WORKSPACE / "assets/app.js").read_bytes(), "text/javascript")
        if self.path == "/assets/style.css":
            return self._send((WORKSPACE / "assets/style.css").read_bytes(), "text/css")
        if self.path == "/assets/ui-premium-v2.css":
            return self._send((WORKSPACE / "assets/ui-premium-v2.css").read_bytes(), "text/css")
        if self.path.startswith("/media/"):
            return self._send(b"", "video/mp4")
        self.send_error(404)

    def log_message(self, _format, *_args):
        return

    def _send(self, body: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8876), Handler).serve_forever()
