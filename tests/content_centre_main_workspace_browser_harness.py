import argparse
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module
from database import Database
from ui import page


def seed_database(db):
    stamp = "2026-08-31T10:00:00"
    draft_id = db.execute(
        """INSERT INTO content_scripts(title,body,category,status,content_year,content_month,content_week,
           recorded_at,published_at,tags_json,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "澳洲低龄留学开学季内容",
            "这是一条用于 Content Centre SAFEBOX A 视觉验收的隔离文案。",
            "澳洲低龄留学",
            "Draft",
            2026,
            8,
            35,
            "",
            "",
            '["签证","陪读","学校"]',
            stamp,
            stamp,
        ),
    )
    db.execute(
        """INSERT INTO content_scripts(title,body,category,status,content_year,content_month,content_week,
           recorded_at,published_at,tags_json,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "私校申请时间线提醒",
            "面向家长说明材料准备、申请节点和后续跟进。",
            "申请规划",
            "PendingRecording",
            2026,
            8,
            35,
            "",
            "",
            '["申请","材料"]',
            stamp,
            stamp,
        ),
    )
    db.execute(
        """INSERT INTO content_published_posts(script_id,published_at,platform,status,created_at,updated_at)
           VALUES(?,?,?,?,?,?)""",
        (draft_id, "2026-08-31", "Xiaohongshu", "Published", stamp, stamp),
    )
    db.execute(
        """INSERT INTO content_extraction_jobs(source_platform,content_type,process_status,status,note_title,
           created_by,updated_by,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        ("Xiaohongshu", "video", "待处理", "待拆解", "待处理任务", 1, 1, stamp, stamp),
    )
    db.execute(
        """INSERT INTO content_extraction_jobs(source_platform,content_type,process_status,status,note_title,
           created_by,updated_by,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        ("Xiaohongshu", "video", "失败", "失败", "失败任务", 1, 1, stamp, stamp),
    )


def render_content_page(theme):
    handler = object.__new__(app_module.CRMHandler)
    handler.query = {}
    handler.csrf = "csrf-token"
    handler.user = {
        "id": 1,
        "username": "sonny",
        "role_name": "Admin",
        "display_name": "Sonny Zhang",
    }
    body = f"""<div class="content-centre-page">
      <nav class="link-status-tabs content-tabs content-centre-tabs">{handler.content_tabs("dashboard")}</nav>
      {handler.content_dashboard_panel()}
    </div>"""
    html = page(
        "内容管理中心",
        body,
        handler.user,
        handler.csrf,
        "content",
        {"mode": "test", "ready": True},
        asset_version="content-centre-safebox-a",
    )
    return html.replace('data-theme="light"', f'data-theme="{theme}"', 1).encode("utf-8")


class HarnessHandler(BaseHTTPRequestHandler):
    rendered_pages = {}

    def log_message(self, _format, *_args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/light", "/dark"}:
            theme = "dark" if parsed.path == "/dark" else "light"
            return self.send_bytes(self.rendered_pages[theme], "text/html; charset=utf-8")
        if parsed.path.startswith("/assets/"):
            asset = ROOT / parsed.path.lstrip("/")
            if asset.is_file():
                content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
                return self.send_bytes(asset.read_bytes(), content_type)
        self.send_error(404)

    def send_bytes(self, payload, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--runtime-dir", required=True)
    args = parser.parse_args()

    runtime_dir = Path(args.runtime_dir).resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    db_path = runtime_dir / "content-centre-main-workspace-preview.sqlite3"
    if db_path.exists():
        db_path.unlink()
    isolated_db = Database(db_path)
    isolated_db.initialize()
    seed_database(isolated_db)
    app_module.DB_PATH = db_path
    app_module.db = isolated_db
    app_module.latest_content_daily_sync_report = lambda: {
        "status": "completed",
        "created": 3,
        "updated": 1,
        "skipped": 2,
        "failed": 0,
        "ended_at": "2026-08-31T10:30:00",
        "report_id": "safebox-a-preview",
    }

    HarnessHandler.rendered_pages = {
        "light": render_content_page("light"),
        "dark": render_content_page("dark"),
    }

    server = ThreadingHTTPServer(("127.0.0.1", args.port), HarnessHandler)
    host, port = server.server_address
    print(f"CONTENT_CENTRE_MAIN_WORKSPACE_READY http://{host}:{port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
