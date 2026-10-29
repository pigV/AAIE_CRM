#!/usr/bin/env python3
"""Serve the contract creation redirect Toast against candidate assets only."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Contract Signing Link manual Clipboard acceptance</title>
  <link rel="stylesheet" href="/assets/style.css">
</head>
<body><main><h1>Contract redirect acceptance</h1>
<form method="post" action="/contracts/generate" data-contract-generation-form>
  <button type="submit">生成合同</button>
</form>
</main>
<script src="/assets/app.js"></script></body>
</html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            payload = HTML
            content_type = "text/html; charset=utf-8"
        elif path in {"/assets/style.css", "/assets/app.js"}:
            target = ROOT / path.lstrip("/")
            payload = target.read_bytes()
            content_type = "text/css" if target.suffix == ".css" else "application/javascript"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format, *_args):
        return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8898)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"CONTRACT_AUTO_SIGNING_BROWSER_READY http://127.0.0.1:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
