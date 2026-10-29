import argparse
import json
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module
from database import Database


def seed_database(db):
    stamp = "2026-08-31T10:00:00"
    topic_id = db.execute(
        """INSERT INTO content_topics(
           topic_name,category,status,content_year,source,source_type,source_reference,tags_json,
           imported_at,imported_by,import_batch_id,potential_duplicate,migration_notes,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "内容选题工作台长标题 " + "LongTitle" * 8,
            "澳洲低龄留学",
            "Active",
            2026,
            "CRM",
            "manual",
            "synthetic-topic-reference",
            json.dumps(["选题", "视觉验收"], ensure_ascii=False),
            stamp,
            1,
            "safebox-b-visual",
            0,
            "",
            stamp,
            stamp,
        ),
    )
    script_id = db.execute(
        """INSERT INTO content_scripts(
           title,body,category,topic_id,status,review_status,version_number,source_type,
           content_year,tags_json,notes,source_url,planned_recording_date,planned_publish_date,published_at,
           hook,original_text,cleaned_text,viral_analysis_json,review_summary,next_topic_recommendations,
           second_rewrite_suggestions,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "脚本详情工作台长标题 " + "NoBreakTitle" * 8,
            "这是用于 SAFEBOX B detail workspace 视觉验收的长正文。" * 70,
            "澳洲低龄留学",
            topic_id,
            "Draft",
            "Not Reviewed",
            3,
            "crm_generated",
            2026,
            json.dumps(["签证", "陪读", "学校"], ensure_ascii=False),
            "保留现有保存、版本和发布数据表单。",
            "https://example.invalid/source/" + "long-path-" * 8,
            "2026-09-01",
            "2026-09-05",
            "",
            "前三秒钩子",
            "原始文案：" + "原文。" * 20,
            "清洗文案：" + "清洗后内容。" * 20,
            json.dumps({"viral_reason": "结构清晰", "rewrite_direction": "员工审核后发布"}, ensure_ascii=False),
            "复盘摘要",
            "下一批推荐选题",
            "可二次改写建议",
            stamp,
            stamp,
        ),
    )
    for version in range(1, 4):
        db.execute(
            """INSERT INTO content_script_versions(script_id,title,body,version_number,created_at)
               VALUES(?,?,?,?,?)""",
            (script_id, f"脚本历史版本 V{version}", f"版本 {version} 正文", version, stamp),
        )
    staging_id = db.execute(
        """INSERT INTO content_import_staging(
           import_batch_id,legacy_system_name,legacy_table_name,legacy_record_id,
           old_title,old_body,old_category,old_status,old_year,old_tags,
           old_source_type,old_source_url,issue_flags_json,needs_review,
           duplicate_title,duplicate_group_id,missing_gdoc_source,imported_at,review_status
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "safebox-b-staging",
            "redbook",
            "scripts",
            9001,
            "Staging 只读详情长标题 " + "LongTitle" * 8,
            "旧系统 staging 正文。" * 90,
            "旧分类",
            "待审核",
            2025,
            "留学,签证,素材",
            "url",
            "https://example.invalid/staging/" + "long-path-" * 8,
            json.dumps({"duplicate_title": True, "missing_gdoc_source": True}, ensure_ascii=False),
            1,
            1,
            "visual-duplicate-group",
            1,
            stamp,
            "Needs Review",
        ),
    )
    for index in range(4):
        db.execute(
            """INSERT INTO content_import_staging(
               import_batch_id,legacy_system_name,legacy_table_name,legacy_record_id,
               old_title,old_body,old_category,old_status,old_year,old_tags,
               old_source_type,old_source_url,issue_flags_json,needs_review,
               duplicate_title,duplicate_group_id,missing_gdoc_source,imported_at,review_status
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "safebox-b-duplicates",
                "redbook",
                "scripts",
                9100 + index,
                "重复标题对比组 " + "LongTitle" * 5,
                "" if index == 3 else f"重复正文 {index}。" * 60,
                "旧分类",
                "待审核",
                2025,
                "留学",
                "url",
                f"https://example.invalid/duplicate/{index}/" + "long-path-" * 6,
                "{}",
                1,
                1,
                "visual-duplicate-group",
                1 if index == 3 else 0,
                stamp,
                "Needs Review",
            ),
        )
    return {
        "topic": topic_id,
        "script": script_id,
        "staging": staging_id,
        "duplicate": "visual-duplicate-group",
    }


def render_handler_page(method_name, record_id, theme):
    handler = object.__new__(app_module.CRMHandler)
    handler.user = {
        "id": 1,
        "username": "admin",
        "role_name": "Admin",
        "display_name": "Admin",
    }
    handler.csrf = "visual-csrf"
    handler.client_address = ("127.0.0.1", 12700)
    handler.headers = {"User-Agent": "safebox-b-content-detail-visual"}
    handler.message = lambda: ("", "success")
    captured = {}
    handler.send_html = lambda html, *args, **kwargs: captured.setdefault("html", html)
    getattr(handler, method_name)("GET", record_id)
    return captured["html"].replace('data-theme="light"', f'data-theme="{theme}"', 1).encode("utf-8")


class HarnessHandler(BaseHTTPRequestHandler):
    rendered_pages = {}

    def log_message(self, _format, *_args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        key = parsed.path.strip("/")
        if key in self.rendered_pages:
            return self.send_bytes(self.rendered_pages[key], "text/html; charset=utf-8")
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
    db_path = runtime_dir / "content-detail-workspace-preview.sqlite3"
    if db_path.exists():
        db_path.unlink()
    isolated_db = Database(db_path)
    isolated_db.initialize()
    ids = seed_database(isolated_db)
    app_module.DB_PATH = db_path
    app_module.db = isolated_db
    app_module.nas.status = lambda: {
        "status": "ok",
        "ready": True,
        "root": str(runtime_dir / "synthetic-nas"),
    }

    page_specs = {
        "topic": ("content_topic_detail", ids["topic"]),
        "staging": ("content_staging_detail", ids["staging"]),
        "duplicate": ("content_duplicate_group_detail", ids["duplicate"]),
        "script": ("content_script_detail", ids["script"]),
    }
    rendered_pages = {}
    for name, (method_name, record_id) in page_specs.items():
        for theme in ("light", "dark"):
            rendered_pages[f"{theme}/{name}"] = render_handler_page(method_name, record_id, theme)
    HarnessHandler.rendered_pages = rendered_pages

    server = ThreadingHTTPServer(("127.0.0.1", args.port), HarnessHandler)
    host, port = server.server_address
    print(f"CONTENT_DETAIL_WORKSPACE_READY http://{host}:{port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
