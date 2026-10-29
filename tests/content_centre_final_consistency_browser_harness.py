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


class SyntheticDriveRuntimeStore:
    def list_logs(self, limit=50, **_kwargs):
        return [
            {
                "started_at": "2026-08-31T10:00:00",
                "ended_at": "2026-08-31T10:01:00",
                "trigger": "manual_scan",
                "actor_user_id": 1,
                "scanned": 4,
                "created": 1,
                "updated": 1,
                "skipped": 2,
                "failed": 0,
                "status": "completed",
                "report_id": "safebox-c-visual",
            }
        ][:limit]

    def load_preview_for_confirmation(self, _scan_id):
        raise RuntimeError("SAFEBOX C visual harness must not read real Drive previews")


def seed_database(db):
    stamp = "2026-08-31T10:00:00"
    topic_id = db.execute(
        """INSERT INTO content_topics(
           topic_name,category,status,content_year,source,source_type,source_reference,tags_json,
           imported_at,imported_by,import_batch_id,potential_duplicate,migration_notes,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "最终一致性选题工作台 " + "LongTitle" * 7,
            "澳洲低龄留学",
            "Active",
            2026,
            "CRM",
            "manual",
            "synthetic-topic-reference",
            json.dumps(["选题", "最终验收"], ensure_ascii=False),
            stamp,
            1,
            "safebox-c-visual",
            0,
            "",
            stamp,
            stamp,
        ),
    )
    script_ids = []
    for index, status in enumerate(("Draft", "PendingRecording", "Recorded", "Published")):
        script_id = db.execute(
            """INSERT INTO content_scripts(
               title,body,category,topic_id,status,review_status,version_number,source_type,
               content_year,content_month,content_week,recorded_at,published_at,tags_json,notes,source_url,
               planned_recording_date,planned_publish_date,hook,original_text,cleaned_text,viral_analysis_json,
               created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"最终一致性文案 {index} " + "NoBreakTitle" * 5,
                "用于 SAFEBOX C 最终视觉和交互验收的长正文。" * (40 + index * 5),
                "澳洲低龄留学" if index % 2 == 0 else "申请规划",
                topic_id,
                status,
                "Approved" if status in {"Recorded", "Published"} else "Not Reviewed",
                index + 1,
                "crm_generated",
                2026,
                8,
                35,
                "2026-08-31" if status in {"Recorded", "Published"} else "",
                "2026-08-31" if status == "Published" else "",
                json.dumps(["签证", "陪读", "学校", f"Tag{index}"], ensure_ascii=False),
                "保留现有保存、版本和发布数据表单。",
                "https://example.invalid/source/" + "long-path-" * 8,
                "2026-09-01",
                "2026-09-05",
                "前三秒钩子",
                "原始文案：" + "原文。" * 18,
                "清洗文案：" + "清洗后内容。" * 18,
                json.dumps({"viral_reason": "结构清晰", "rewrite_direction": "员工审核后发布"}, ensure_ascii=False),
                stamp,
                stamp,
            ),
        )
        script_ids.append(script_id)
        db.execute(
            """INSERT INTO content_script_versions(script_id,title,body,version_number,created_at)
               VALUES(?,?,?,?,?)""",
            (script_id, f"历史版本 V{index + 1}", "版本正文", index + 1, stamp),
        )
        db.execute(
            """INSERT INTO content_performance_metrics(script_id,checkpoint,views,likes,saves,comments,shares,measured_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (script_id, "post_day_7", 1000 + index * 100, 80 + index, 40 + index, 12 + index, 3 + index, stamp, stamp, stamp),
        )
    db.execute(
        """INSERT INTO content_published_posts(script_id,topic_id,published_at,platform,status,post_title,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (script_ids[-1], topic_id, "2026-08-31", "Xiaohongshu", "Published", "发布记录", stamp, stamp),
    )
    for index, process_status in enumerate(("待处理", "转写中", "已完成", "失败")):
        db.execute(
            """INSERT INTO content_extraction_jobs(
               source_platform,source_url,source_type,content_type,note_title,pasted_text,
               process_status,status,error_message,category,hook,rewritten_script,confirmed_at,
               is_in_script_library,published_post_id,view_count,favorite_count,speech_transcript,
               subtitle_ocr_text,calibrated_text,topic_tags,needs_manual_review,extraction_confidence,
               uploaded_file_path,created_by,updated_by,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "Xiaohongshu",
                f"https://example.invalid/final-consistency/{index}",
                "text",
                "mixed",
                f"最终一致性提取任务 {index}",
                "Synthetic pasted text",
                process_status,
                "待确认",
                "Synthetic isolated failure detail " * 3 if process_status == "失败" else "",
                "澳洲低龄留学",
                "hook" if index % 2 else "",
                "rewritten script" if index % 2 else "",
                stamp if process_status == "已完成" else None,
                1 if process_status == "已完成" else 0,
                None,
                100 + index,
                20 + index,
                "Synthetic speech transcript",
                "Synthetic OCR transcript",
                "Synthetic calibrated text",
                '["Synthetic", "Final"]',
                1,
                0.82,
                f"/synthetic/storage/final-consistency-{index}.mp4",
                1,
                1,
                stamp,
                stamp,
            ),
        )
    staging_id = db.execute(
        """INSERT INTO content_import_staging(
           import_batch_id,legacy_system_name,legacy_table_name,legacy_record_id,
           old_title,old_body,old_category,old_status,old_year,old_tags,
           old_source_type,old_source_url,issue_flags_json,needs_review,
           duplicate_title,duplicate_group_id,missing_gdoc_source,imported_at,review_status
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "safebox-c-staging",
            "redbook",
            "scripts",
            9001,
            "最终一致性 Staging 详情 " + "LongTitle" * 7,
            "旧系统 staging 正文。" * 80,
            "旧分类",
            "待审核",
            2025,
            "留学,签证,素材",
            "url",
            "https://example.invalid/staging/" + "long-path-" * 8,
            json.dumps({"duplicate_title": True, "missing_gdoc_source": True}, ensure_ascii=False),
            1,
            1,
            "safebox-c-duplicate-group",
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
                "safebox-c-duplicates",
                "redbook",
                "scripts",
                9100 + index,
                "最终一致性重复标题组 " + "LongTitle" * 5,
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
                "safebox-c-duplicate-group",
                1 if index == 3 else 0,
                stamp,
                "Needs Review",
            ),
        )
    return {
        "topic": topic_id,
        "script": script_ids[0],
        "staging": staging_id,
        "duplicate": "safebox-c-duplicate-group",
    }


def block_external_calls(*_args, **_kwargs):
    raise RuntimeError("SAFEBOX C visual harness blocks external URL access")


def synthetic_drive_status():
    return {
        "connection_code": "connected",
        "connection_label": "已连接",
        "service_code": "paused",
        "service_label": "已暂停",
        "scheduler_health_code": "healthy",
        "scheduler_health_label": "健康",
        "carrier_status": "PAUSED",
        "is_enabled": False,
        "last_success_at": "2026-08-31T10:00:00",
        "last_scan_at": "2026-08-31T10:00:00",
        "last_attempt_at": "2026-08-31T10:00:00",
        "last_expected_run_at": "2026-08-31T23:00:00",
        "next_run_at": "2026-09-01T23:00:00",
        "last_result": "synthetic",
        "last_error": "",
        "last_attempt_reason": "synthetic SAFEBOX C fixture",
    }


def handler_for(theme, query=None):
    admin = dict(
        app_module.db.one(
            "SELECT u.*,r.name role_name,r.permissions_json "
            "FROM users u JOIN roles r ON r.id=u.role_id WHERE u.username='admin'"
        )
    )
    admin["module_permissions_json"] = json.dumps(["content_center"])
    handler = object.__new__(app_module.CRMHandler)
    handler.user = admin
    handler.csrf = "safebox-c-visual-csrf"
    handler.query = query or {}
    handler.client_address = ("127.0.0.1", 13200)
    handler.headers = {"User-Agent": "safebox-c-content-final-consistency-visual"}
    handler.message = lambda: ("", "success")
    handler.theme = theme
    return handler


def render_content_tab(tab, theme):
    handler = handler_for(theme, {"tab": [tab]})
    captured = {}
    handler.send_html = lambda html, *args, **kwargs: captured.setdefault("html", html)
    handler.content_center("GET")
    return captured["html"].replace('data-theme="light"', f'data-theme="{theme}"', 1).encode("utf-8")


def render_detail(method_name, record_id, theme):
    handler = handler_for(theme)
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
    db_path = runtime_dir / "content-centre-final-consistency-preview.sqlite3"
    if db_path.exists():
        db_path.unlink()
    isolated_db = Database(db_path)
    isolated_db.initialize()
    ids = seed_database(isolated_db)
    app_module.DB_PATH = db_path
    app_module.db = isolated_db
    app_module.nas.status = lambda: {
        "mode": "test",
        "status": "ok",
        "ready": True,
        "root": str(runtime_dir / "synthetic-nas"),
    }
    app_module.urllib.request.urlopen = block_external_calls
    app_module.google_drive_runtime_store = SyntheticDriveRuntimeStore()
    app_module.google_drive_workspace_status = synthetic_drive_status
    app_module.latest_content_daily_sync_report = lambda: {
        "status": "completed",
        "created": 4,
        "updated": 1,
        "skipped": 2,
        "failed": 0,
        "ended_at": "2026-08-31T10:30:00",
        "report_id": "safebox-c-visual",
    }

    rendered_pages = {}
    for theme in ("light", "dark"):
        for tab in ("dashboard", "create", "extract", "sync", "tracking", "analysis"):
            rendered_pages[f"{theme}/{tab}"] = render_content_tab(tab, theme)
        for name, method_name, record_id in (
            ("topic", "content_topic_detail", ids["topic"]),
            ("staging", "content_staging_detail", ids["staging"]),
            ("duplicate", "content_duplicate_group_detail", ids["duplicate"]),
            ("script", "content_script_detail", ids["script"]),
        ):
            rendered_pages[f"{theme}/{name}"] = render_detail(method_name, record_id, theme)
    HarnessHandler.rendered_pages = rendered_pages

    server = ThreadingHTTPServer(("127.0.0.1", args.port), HarnessHandler)
    host, port = server.server_address
    print(f"CONTENT_CENTRE_FINAL_CONSISTENCY_READY http://{host}:{port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
