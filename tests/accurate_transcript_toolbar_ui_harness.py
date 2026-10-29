"""Local-only SAFEBOX 186 browser harness for post-poll toolbar binding."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[1]
PORT = 8877
TRANSCRIPT = "当前完整文案包含一个疑点"
UNCERTAINTIES = [{
    "review_id": "toolbar-r1",
    "start_offset": 10,
    "end_offset": 12,
    "current_text": "疑点",
    "candidate_texts": ["疑点", "疑问"],
}]


def editor_html() -> str:
    metadata = json.dumps(UNCERTAINTIES, ensure_ascii=False).replace("</", "<\\/")
    return f"""<section data-content-extraction-detail="30" data-detail-updated-at="ready">
      <div data-accurate-transcript-editor data-save-url="/never-save" data-csrf="test-only">
        <button type="button" data-uncertainty-toggle>显示疑点</button>
        <button type="button" data-transcript-copy>复制全文</button>
        <button type="button" data-transcript-save>保存文案</button>
        <div data-transcript-highlights hidden></div>
        <textarea data-transcript-input>{TRANSCRIPT}</textarea>
        <div data-uncertainty-list hidden></div>
        <div data-transcript-status aria-live="polite"></div>
        <script type="application/json" data-transcript-uncertainties>{metadata}</script>
      </div>
      <pre data-raw-provider>RAW OCR / RAW ASR PRESERVED</pre>
    </section>"""


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>SAFEBOX 186 Toolbar Harness</title>
<link rel="stylesheet" href="/assets/ui-premium-v2.css"></head><body>
<script src="/capture.js"></script>
<main><section data-content-extraction-progress="30" data-should-poll="true" data-status-url="/status">
  <span data-progress-status>处理中</span><span data-progress-message></span><div data-progress-bar></div>
</section><section data-content-extraction-detail="30" data-detail-updated-at="initial">等待结果</section></main>
<script src="/assets/app.js"></script><script src="/probe.js"></script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/":
            return self.send_bytes(PAGE.encode(), "text/html; charset=utf-8")
        if self.path == "/assets/app.js":
            return self.send_bytes((WORKSPACE / "assets/app.js").read_bytes(), "text/javascript")
        if self.path == "/capture.js":
            return self.send_bytes(
                b"window.addEventListener('error',function(e){document.documentElement.dataset.toolbarError=String(e.message||e.error);});",
                "text/javascript",
            )
        if self.path == "/probe.js":
            return self.send_bytes(
                b"document.documentElement.dataset.toolbarUi=String(!!window.AccurateTranscriptCRMUI);",
                "text/javascript",
            )
        if self.path == "/assets/ui-premium-v2.css":
            return self.send_bytes((WORKSPACE / "assets/ui-premium-v2.css").read_bytes(), "text/css")
        if self.path == "/status":
            payload = {
                "ok": True,
                "job_id": 30,
                "process_status": "处理中",
                "progress_percent": 100,
                "detail_updated_at": "ready",
                "detail_html": editor_html(),
            }
            return self.send_bytes(json.dumps(payload, ensure_ascii=False).encode(), "application/json")
        self.send_error(404)

    def log_message(self, _format, *_args):
        return

    def send_bytes(self, body: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
