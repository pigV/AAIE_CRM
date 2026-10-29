import io
import json
import sqlite3
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlencode


def link_state(row):
    if not row or not row["share_url"]:
        return "missing"
    if row["link_status"] == "revoked":
        return "revoked"
    if row["link_confirmed_at"] or row["confirmed_at"]:
        return "confirmed"
    if row["link_bound_wechat_openid"]:
        return "logged_in"
    if row["link_first_viewed_at"]:
        return "opened"
    return "not_opened"


class SchoolPlanReportWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE students (
          student_id TEXT PRIMARY KEY, passport_first_name TEXT, passport_last_name TEXT
        );
        CREATE TABLE school_plan_reports (
          id INTEGER PRIMARY KEY, student_id TEXT, application_id TEXT,
          report_title TEXT, report_version INTEGER, selected_school_ids TEXT,
          file_name TEXT, file_path TEXT, status TEXT, active_share_link_id INTEGER,
          city_id INTEGER, city_name_snapshot TEXT, plan_sector TEXT,
          is_current INTEGER, superseded_by_report_id INTEGER,
          scope_review_status TEXT, confirmed_at TEXT, confirmed_by_name TEXT,
          created_by_name TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE school_plan_report_links (
          id INTEGER PRIMARY KEY, status TEXT, share_url TEXT, expires_at TEXT,
          viewed_count INTEGER, first_viewed_at TEXT, last_viewed_at TEXT,
          confirmed_at TEXT, bound_wechat_openid TEXT
        );
        INSERT INTO students VALUES ('S001','Jia','Yu'),('S002','Alex','Chen');
        INSERT INTO school_plan_report_links VALUES
          (10,'active','https://example.test/mel','2099-08-14T00:00:00+10:00',2,'2026-08-07T09:00:00+10:00','2026-08-07T10:00:00+10:00',NULL,'wx-mel'),
          (11,'active','https://example.test/syd','2099-08-14T00:00:00+10:00',0,NULL,NULL,NULL,NULL),
          (12,'confirmed','https://example.test/private-a','2099-08-14T00:00:00+10:00',3,'2026-08-06T09:00:00+10:00','2026-08-06T11:00:00+10:00','2026-08-06T12:00:00+10:00','wx-a'),
          (13,'active','https://example.test/private-b','2099-08-14T00:00:00+10:00',1,'2026-08-07T08:00:00+10:00','2026-08-07T08:30:00+10:00',NULL,'wx-b');
        INSERT INTO school_plan_reports VALUES
          (1,'S001','A001','Melbourne Public v1',1,'[1,2]','mel-v1.pdf','/reports/mel-v1.pdf','superseded',NULL,1,'Melbourne','public',0,2,'resolved',NULL,NULL,'Ted','2026-08-01','2026-08-01'),
          (2,'S001','A001','Melbourne Public v2',2,'[1,2,3]','mel-v2.pdf','/reports/mel-v2.pdf','viewed',10,1,'Melbourne','public',1,NULL,'resolved',NULL,NULL,'Ted','2026-08-02','2026-08-07T10:00:00+10:00'),
          (3,'S001','A001','Sydney Public v1',1,'[4,5]','syd-v1.pdf','/reports/syd-v1.pdf','shared',11,2,'Sydney','public',1,NULL,'resolved',NULL,NULL,'Ted','2026-08-03','2026-08-07T09:30:00+10:00'),
          (4,'S001','A001','Sydney Private A',1,'[6]','private-a.pdf','/reports/private-a.pdf','confirmed',12,2,'Sydney','private',1,NULL,'resolved','2026-08-06T12:00:00+10:00','Parent A','Ted','2026-08-04','2026-08-06T12:00:00+10:00'),
          (5,'S001','A001','Sydney Private B',2,'[7]',NULL,NULL,'viewed',13,2,'Sydney','private',1,NULL,'resolved',NULL,NULL,'Ted','2026-08-05','2026-08-07T08:30:00+10:00'),
          (6,'S002','A002','Adelaide Public',1,'[8]',NULL,NULL,'generated',NULL,3,'Adelaide','public',1,NULL,'resolved',NULL,NULL,'Sonny','2026-08-06','2026-08-06');
        """)

    def tearDown(self):
        self.db.close()

    def payload(self, **overrides):
        from school_plan_report_workspace import load_school_plan_report_workspace

        class ReadDatabase:
            def __init__(self, connection):
                self.connection = connection

            def all(self, sql, params=()):
                return self.connection.execute(sql, params).fetchall()

        return load_school_plan_report_workspace(
            ReadDatabase(self.db),
            link_status=link_state,
            display_datetime=lambda value: str(value or ""),
            **overrides,
        )

    def dispatch_workspace(self, method="GET", query=None, *, allowed=True):
        import app as app_module

        class DispatchDatabase:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, params=()):
                return self.connection.execute(sql, params)

            def all(self, sql, params=()):
                return self.connection.execute(sql, params).fetchall()

            def audit(self, *_args, **_kwargs):
                return None

        user = {
            "id": 1,
            "username": "workspace-test",
            "role_name": "View Only",
            "module_permissions_json": json.dumps(
                ["school_plan_reports"] if allowed else []
            ),
        }
        handler = object.__new__(app_module.CRMHandler)
        handler.path = "/school-plan-reports/workspace-data"
        if query:
            handler.path += "?" + urlencode(query)
        handler.server = SimpleNamespace(service_mode="admin", server_port=0)
        handler.client_address = ("127.0.0.1", 16600)
        handler.headers = {
            "User-Agent": "school-plan-workspace-dispatch-test",
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": "0",
        }
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.response_status = None
        handler.response_headers = []
        handler.current_user = lambda: (user, "valid-csrf")
        handler.send_response = lambda status: setattr(
            handler, "response_status", int(status)
        )
        handler.send_header = lambda key, value: handler.response_headers.append(
            (key, value)
        )
        handler.end_headers = lambda: None

        def html_error(message, status=400):
            payload = str(message).encode("utf-8")
            handler.send_response(status)
            handler.send_header("Content-Type", "text/html; charset=utf-8")
            handler.end_headers()
            handler.wfile.write(payload)

        handler.error_page = html_error
        with mock.patch.object(app_module, "db", DispatchDatabase(self.db)), mock.patch.object(
            app_module, "maybe_run_auto_backup", return_value=None
        ):
            handler.dispatch(method)
        return handler

    def test_dispatch_returns_json_403_for_signed_in_user_without_module_access(self):
        handler = self.dispatch_workspace(allowed=False)

        self.assertEqual(handler.response_status, 403)
        self.assertIn(
            ("Content-Type", "application/json; charset=utf-8"),
            handler.response_headers,
        )
        self.assertEqual(
            json.loads(handler.wfile.getvalue()),
            {"ok": False, "error": "access_denied"},
        )

    def test_dispatch_returns_json_405_for_post_before_global_csrf_gate(self):
        handler = self.dispatch_workspace("POST")

        self.assertEqual(handler.response_status, 405)
        self.assertIn(
            ("Content-Type", "application/json; charset=utf-8"),
            handler.response_headers,
        )
        self.assertEqual(
            json.loads(handler.wfile.getvalue()),
            {"ok": False, "error": "method_not_allowed"},
        )

    def test_dispatch_authorised_get_forwards_workspace_query(self):
        handler = self.dispatch_workspace(
            query={"q": " Jia ", "status": "all", "page": "1", "report": "2"}
        )
        response = json.loads(handler.wfile.getvalue())
        workspace = response["workspace"]

        self.assertEqual(handler.response_status, 200)
        self.assertTrue(response["ok"])
        self.assertEqual(workspace["search"], "Jia")
        self.assertEqual(workspace["status_filter"], "all")
        self.assertEqual(workspace["page"], 1)
        self.assertEqual(workspace["selected_report_id"], 2)
        self.assertEqual(workspace["selected"]["selected_report"]["report_id"], 2)

    def test_dispatch_invalid_query_is_normalised_by_adapter_and_view_model(self):
        handler = self.dispatch_workspace(
            query={"q": " nobody ", "status": "invalid", "page": "bad", "report": "bad"}
        )
        response = json.loads(handler.wfile.getvalue())
        workspace = response["workspace"]

        self.assertEqual(handler.response_status, 200)
        self.assertTrue(response["ok"])
        self.assertEqual(workspace["search"], "nobody")
        self.assertEqual(workspace["status_filter"], "needs_followup")
        self.assertEqual(workspace["page"], 1)
        self.assertEqual(workspace["groups"], [])
        self.assertIsNone(workspace["selected_report_id"])

    def test_dispatch_history_only_selection_preserves_non_actionable_anchor(self):
        self.db.execute("UPDATE school_plan_reports SET is_current=0 WHERE id=6")
        handler = self.dispatch_workspace(
            query={"q": "Alex", "status": "all", "report": "6"}
        )
        workspace = json.loads(handler.wfile.getvalue())["workspace"]

        self.assertEqual(handler.response_status, 200)
        self.assertEqual(workspace["selected_report_id"], 6)
        self.assertEqual(workspace["selected"]["student_id"], "S002")
        self.assertIsNone(workspace["selected"]["selected_report"])

    def test_preserves_all_simultaneous_current_plans_and_public_history(self):
        selected = self.payload(status_filter="all", selected_report_id=2)["selected"]
        self.assertEqual([plan["report_id"] for plan in selected["current_plans"]], [2, 3, 5, 4])
        self.assertEqual(selected["selected_report"]["report_id"], 2)
        self.assertEqual([row["report_id"] for row in selected["records"]], [2, 1, 3, 5, 4])
        self.assertTrue(selected["records"][0]["is_current"])
        self.assertFalse(selected["records"][1]["is_current"])

    def test_filters_grouped_students_and_prioritises_follow_up(self):
        follow_up = self.payload(status_filter="needs_followup")
        self.assertEqual([group["student_id"] for group in follow_up["groups"]], ["S001"])
        self.assertEqual(follow_up["groups"][0]["summary_report_id"], 2)
        all_rows = self.payload(status_filter="all")
        self.assertEqual([group["student_id"] for group in all_rows["groups"]], ["S001", "S002"])
        self.assertEqual(all_rows["groups"][1]["summary_status"], "no_link")

    def test_search_pagination_and_invalid_selection_are_server_authoritative(self):
        result = self.payload(search="alex", status_filter="all", page=99, page_size=1, selected_report_id=999)
        self.assertEqual(result["page"], 1)
        self.assertEqual(result["page_count"], 1)
        self.assertEqual(result["groups"][0]["student_id"], "S002")
        self.assertEqual(result["selected"]["selected_report"]["report_id"], 6)

    def test_version_actions_are_bound_to_the_exact_report(self):
        records = self.payload(status_filter="all", selected_report_id=2)["selected"]["records"]
        old = next(row for row in records if row["report_id"] == 1)
        current = next(row for row in records if row["report_id"] == 2)
        self.assertEqual(old["detail_url"], "/school-plan-reports/1")
        self.assertEqual(current["download_url"], "/school-plan-reports/2/download")
        self.assertNotEqual(old["detail_url"], current["detail_url"])

    def test_filter_counts_apply_after_search_but_before_active_filter(self):
        result = self.payload(search="jia", status_filter="confirmed")
        counts = {item["key"]: item["count"] for item in result["filters"]}
        self.assertEqual(counts, {
            "needs_followup": 1, "opened": 1, "unopened": 1, "confirmed": 1, "all": 1,
        })

    def test_selected_plan_switches_atomically_inside_same_student_group(self):
        selected = self.payload(status_filter="all", selected_report_id=5)["selected"]
        report = selected["selected_report"]
        self.assertEqual(report["report_id"], 5)
        self.assertEqual(report["share_url"], "https://example.test/private-b")
        self.assertEqual(report["download_url"], "")
        self.assertEqual(report["parent_status"], "logged_in")

    def test_missing_link_is_not_counted_as_unopened(self):
        result = self.payload(status_filter="unopened")
        self.assertEqual([group["student_id"] for group in result["groups"]], ["S001"])
        self.assertNotIn("S002", [group["student_id"] for group in result["groups"]])

    def test_loader_uses_production_database_read_api(self):
        from school_plan_report_workspace import load_school_plan_report_workspace

        class ProductionStyleDatabase:
            def __init__(self, connection):
                self.connection = connection

            def all(self, sql, params=()):
                return self.connection.execute(sql, params).fetchall()

            def execute(self, _sql, _params=()):
                return 1

        result = load_school_plan_report_workspace(
            ProductionStyleDatabase(self.db),
            link_status=link_state,
            display_datetime=lambda value: str(value or ""),
            status_filter="all",
        )

        self.assertEqual(result["total_groups"], 2)

    def test_loader_never_materializes_snapshot_json_and_pages_in_sql(self):
        """Break caught: a list loader selecting wide report rows or paging only in Python."""
        from school_plan_report_workspace import load_school_plan_report_workspace

        self.db.execute("ALTER TABLE school_plan_reports ADD COLUMN snapshot_json TEXT")
        self.db.execute(
            "UPDATE school_plan_reports SET snapshot_json=?",
            ("x" * 1_000_000,),
        )

        class RecordingDatabase:
            def __init__(self, connection):
                self.connection = connection
                self.calls = []

            def all(self, sql, params=()):
                rows = self.connection.execute(sql, params).fetchall()
                self.calls.append((sql, tuple(params), rows))
                return rows

        database = RecordingDatabase(self.db)
        result = load_school_plan_report_workspace(
            database,
            link_status=link_state,
            display_datetime=lambda value: str(value or ""),
            status_filter="all",
            page=1,
            page_size=1,
        )

        self.assertEqual(1, len(result["groups"]))
        self.assertGreaterEqual(len(database.calls), 2)
        self.assertTrue(any(" LIMIT " in sql.upper() for sql, _params, _rows in database.calls))
        for _sql, _params, rows in database.calls:
            for row in rows:
                self.assertNotIn("snapshot_json", row.keys())

    def test_loader_paginates_group_keys_in_sql_without_materializing_all_groups(self):
        """Break caught: the loader fetches every metadata row before slicing groups."""
        from school_plan_report_workspace import (
            build_school_plan_report_workspace,
            load_school_plan_report_workspace,
        )

        for number in range(3, 128):
            student_id = f"S{number:03d}"
            application_id = f"A{number:03d}"
            report_id = 1000 + number
            link_id = 2000 + number
            state = number % 4
            self.db.execute(
                "INSERT INTO students VALUES (?,?,?)",
                (student_id, f"First{number:03d}", f"Last{number:03d}"),
            )
            if state != 3:
                self.db.execute(
                    "INSERT INTO school_plan_report_links VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        link_id,
                        "confirmed" if state == 2 else "active",
                        f"https://example.test/{number}",
                        "2099-08-14T00:00:00+10:00",
                        1 if state in (1, 2) else 0,
                        "2026-08-10T08:00:00+10:00" if state == 1 else None,
                        f"2026-08-{10 + number % 9:02d}T09:00:00+10:00"
                        if state in (1, 2)
                        else None,
                        "2026-08-10T10:00:00+10:00" if state == 2 else None,
                        None,
                    ),
                )
            self.db.execute(
                """INSERT INTO school_plan_reports VALUES
                   (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    report_id,
                    student_id,
                    application_id,
                    f"Plan {number}",
                    1,
                    "[]",
                    None,
                    None,
                    "confirmed" if state == 2 else "generated",
                    link_id if state != 3 else None,
                    number,
                    f"City {number:03d}",
                    "public" if number % 2 else "private",
                    1,
                    None,
                    "resolved",
                    "2026-08-10T10:00:00+10:00" if state == 2 else None,
                    "Parent" if state == 2 else None,
                    "Tester",
                    "2026-08-01T00:00:00+10:00",
                    f"2026-08-{10 + number % 9:02d}T09:00:00+10:00",
                ),
            )

        reference_rows = self.db.execute(
            """SELECT r.id,r.student_id,r.application_id,r.report_title,
                      r.report_version,r.selected_school_ids,r.file_name,r.file_path,
                      r.status,r.city_id,r.city_name_snapshot,r.plan_sector,
                      r.is_current,r.superseded_by_report_id,r.scope_review_status,
                      r.confirmed_at,r.confirmed_by_name,r.created_by_name,
                      r.created_at,r.updated_at,
                      s.passport_first_name,s.passport_last_name,
                      l.status link_status,l.share_url,l.expires_at,
                      l.viewed_count,l.last_viewed_at,
                      l.confirmed_at link_confirmed_at,
                      l.bound_wechat_openid link_bound_wechat_openid,
                      l.first_viewed_at link_first_viewed_at
                 FROM school_plan_reports r
                 LEFT JOIN students s ON s.student_id=r.student_id
                 LEFT JOIN school_plan_report_links l ON l.id=r.active_share_link_id
                ORDER BY r.updated_at DESC,r.id DESC"""
        ).fetchall()

        class RecordingDatabase:
            def __init__(self, connection):
                self.connection = connection
                self.calls = []

            def all(self, sql, params=()):
                rows = self.connection.execute(sql, params).fetchall()
                self.calls.append((sql, tuple(params), rows))
                return rows

        page_size = 17
        for status_filter in ("needs_followup", "opened", "confirmed", "all"):
            reference = build_school_plan_report_workspace(
                reference_rows,
                link_status=link_state,
                display_datetime=lambda value: str(value or ""),
                status_filter=status_filter,
                page=1,
                page_size=page_size,
            )
            expected_keys = []
            for page_number in range(1, reference["page_count"] + 1):
                expected_page = build_school_plan_report_workspace(
                    reference_rows,
                    link_status=link_state,
                    display_datetime=lambda value: str(value or ""),
                    status_filter=status_filter,
                    page=page_number,
                    page_size=page_size,
                )
                expected_keys.extend(group["group_key"] for group in expected_page["groups"])

            actual_keys = []
            last_result = None
            page_number = 1
            while last_result is None or page_number <= last_result["page_count"]:
                database = RecordingDatabase(self.db)
                last_result = load_school_plan_report_workspace(
                    database,
                    link_status=link_state,
                    display_datetime=lambda value: str(value or ""),
                    status_filter=status_filter,
                    page=page_number,
                    page_size=page_size,
                )
                actual_keys.extend(group["group_key"] for group in last_result["groups"])
                self.assertTrue(
                    any(
                        " LIMIT ? OFFSET ?" in " ".join(sql.upper().split())
                        for sql, _params, _rows in database.calls
                    )
                )
                for _sql, _params, rows in database.calls:
                    if rows and {"student_id", "application_id"}.issubset(rows[0].keys()):
                        returned_groups = {
                            (row["student_id"], row["application_id"]) for row in rows
                        }
                        self.assertLessEqual(len(returned_groups), page_size)
                page_number += 1

            self.assertEqual(actual_keys, expected_keys)
            self.assertEqual(len(actual_keys), len(set(actual_keys)))
            self.assertEqual(last_result["total_groups"], len(expected_keys))
            self.assertEqual(
                {item["key"]: item["count"] for item in last_result["filters"]},
                {item["key"]: item["count"] for item in reference["filters"]},
            )

    def test_unknown_filter_is_normalised_and_empty_search_is_stable(self):
        result = self.payload(search="nobody", status_filter="unexpected", page=-4)
        self.assertEqual(result["status_filter"], "needs_followup")
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["selected"], None)
        self.assertEqual(result["page"], 1)
        self.assertEqual(result["page_count"], 1)

    def test_all_history_group_has_no_current_plan_or_selected_report(self):
        self.db.execute("UPDATE school_plan_reports SET is_current=0 WHERE id=6")
        result = self.payload(status_filter="all", selected_report_id=6)
        group = next(item for item in result["groups"] if item["student_id"] == "S002")
        self.assertEqual(group["current_plans"], [])
        self.assertEqual([record["report_id"] for record in group["records"]], [6])
        self.assertEqual(result["selected"]["student_id"], "S002")
        self.assertIsNone(result["selected"]["selected_report"])
        self.assertEqual(result["selected_report_id"], 6)
        for status_filter in ("needs_followup", "opened", "unopened", "confirmed"):
            filtered = self.payload(status_filter=status_filter)
            self.assertNotIn("S002", [group["student_id"] for group in filtered["groups"]])


class SchoolPlanReportWorkspaceRendererTests(unittest.TestCase):
    def renderer_payload(self):
        current = {
            "report_id": 2,
            "student_id": "S001",
            "application_id": "A001",
            "title": "Melbourne Public v2",
            "version": 2,
            "sector": "public",
            "city_name": "Melbourne",
            "is_current": True,
            "status": "viewed",
            "parent_status": "opened",
            "operational_state": "needs_followup",
            "share_url": "https://example.test/current",
            "can_copy_link": True,
            "viewed_count": 2,
            "last_viewed_display": "2026-08-07 10:00",
            "confirmed_display": "",
            "confirmed_by_name": "",
            "school_count": 3,
            "file_name": "mel-v2.pdf",
            "has_pdf": True,
            "detail_url": "/school-plan-reports/2",
            "download_url": "/school-plan-reports/2/download",
            "created_by_name": "Ted",
            "created_display": "2026-08-02 09:00",
        }
        other_current = {
            **current,
            "report_id": 3,
            "title": "Sydney Private v1",
            "version": 1,
            "sector": "private",
            "city_name": "Sydney",
            "status": "confirmed",
            "parent_status": "confirmed",
            "operational_state": "confirmed",
            "share_url": "https://example.test/sydney",
            "viewed_count": 3,
            "last_viewed_display": "2026-08-06 11:00",
            "confirmed_display": "2026-08-06 12:00",
            "confirmed_by_name": "Parent A",
            "school_count": 1,
            "file_name": "sydney-v1.pdf",
            "detail_url": "/school-plan-reports/3",
            "download_url": "/school-plan-reports/3/download",
        }
        history = {
            **current,
            "report_id": 1,
            "title": "Melbourne Public v1",
            "version": 1,
            "is_current": False,
            "status": "superseded",
            "parent_status": "expired",
            "operational_state": "no_link",
            "share_url": "https://example.test/history",
            "can_copy_link": True,
            "viewed_count": 1,
            "last_viewed_display": "2026-08-01 10:00",
            "file_name": "mel-v1.pdf",
            "detail_url": "/school-plan-reports/1",
            "download_url": "/school-plan-reports/1/download",
        }
        unclassified_history = {
            **history,
            "report_id": 4,
            "title": "Unclassified v1",
            "city_name": "",
            "sector": "",
            "share_url": "",
            "can_copy_link": False,
            "file_name": "",
            "has_pdf": False,
            "detail_url": "/school-plan-reports/4",
            "download_url": "",
        }
        return {
            "search": "<script>alert(1)</script>",
            "status_filter": "all",
            "filters": [{"key": "all", "label": "全部", "count": 1}],
            "groups": [
                {
                    "group_key": "S001::A001",
                    "student_id": "S001",
                    "application_id": "A001",
                    "student_name": "Jia <Yu>",
                    "summary_report_id": 2,
                    "summary_status": "needs_followup",
                    "summary_activity_display": "2026-08-07 10:00",
                    "current_plan_count": 2,
                }
            ],
            "page": 1,
            "page_count": 1,
            "total_groups": 1,
            "selected": {
                "group_key": "S001::A001",
                "student_id": "S001",
                "application_id": "A001",
                "student_name": "Jia <Yu>",
                "create_url": "/school-plan-reports/create?student_id=S001",
                "current_plans": [current, other_current],
                "records": [current, history, other_current, unclassified_history],
                "selected_report": current,
            },
        }

    def test_renderer_escapes_content_and_exposes_isolated_contract(self):
        from school_plan_report_workspace_views import render_school_plan_report_workspace

        payload = self.renderer_payload()

        html = render_school_plan_report_workspace(payload)

        self.assertIn("data-school-plan-report-workspace", html)
        self.assertIn("data-school-plan-master-list", html)
        self.assertIn("data-school-plan-detail-workspace", html)
        self.assertIn("data-school-plan-report-workspace-source", html)
        self.assertIn("Jia &lt;Yu&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)

    def test_renderer_shows_atomic_selected_plan_switcher_actions_and_records(self):
        from school_plan_report_workspace_views import render_school_plan_report_workspace

        html = render_school_plan_report_workspace(self.renderer_payload())

        self.assertIn("data-workspace-plan", html)
        self.assertIn("公校 · Melbourne · v2", html)
        self.assertIn("私校 · Sydney · v1", html)
        self.assertIn('data-workspace-copy="https://example.test/current"', html)
        self.assertIn('href="/school-plan-reports/2"', html)
        self.assertIn('href="/school-plan-reports/2/download"', html)
        self.assertIn('href="/school-plan-reports/create?student_id=S001"', html)
        self.assertIn("家长打开", html)
        self.assertIn("家长确认", html)
        self.assertIn("v2 · Melbourne · 公校", html)
        self.assertIn("v1 · Sydney · 私校", html)
        self.assertIn("当前", html)
        self.assertIn("已过期 · 保留记录", html)
        self.assertIn("v1 · 待确认城市 · 待分类", html)
        self.assertIn("data-workspace-back", html)

    def test_renderer_keeps_each_record_identity_and_actions_on_that_version(self):
        from school_plan_report_workspace_views import render_school_plan_report_workspace

        html = render_school_plan_report_workspace(self.renderer_payload())

        self.assertIn('aria-label="v2 · Melbourne · 公校 · 当前"', html)
        self.assertIn(
            'aria-label="v1 · Melbourne · 公校 · 已过期 · 保留记录"', html
        )
        self.assertIn(
            'aria-label="v1 · 待确认城市 · 待分类 · 已过期 · 保留记录"',
            html,
        )
        self.assertIn('data-workspace-copy="https://example.test/history"', html)
        self.assertIn('href="/school-plan-reports/1"', html)
        self.assertIn('href="/school-plan-reports/1/download"', html)

    def test_renderer_history_only_group_never_promotes_history_to_current_actions(self):
        from school_plan_report_workspace_views import render_school_plan_report_workspace

        payload = self.renderer_payload()
        historical = deepcopy(payload["selected"]["records"][1])
        payload["selected"]["current_plans"] = []
        payload["selected"]["records"] = [historical]
        payload["selected"]["selected_report"] = None

        html = render_school_plan_report_workspace(payload)

        self.assertIn("暂无当前有效方案", html)
        self.assertIn('aria-label="v1 · Melbourne · 公校 · 已过期 · 保留记录"', html)
        selected_actions = html.split('data-workspace-selected-actions', 1)[1].split(
            "</div>", 1
        )[0]
        self.assertIn("新建方案", selected_actions)
        self.assertNotIn("data-workspace-copy=", selected_actions)
        self.assertNotIn('/download"', selected_actions)

    def test_renderer_shows_safe_missing_link_and_missing_pdf_states(self):
        from school_plan_report_workspace_views import render_school_plan_report_workspace

        no_link = self.renderer_payload()
        no_link_report = no_link["selected"]["selected_report"]
        no_link_report["share_url"] = ""
        no_link_report["can_copy_link"] = False
        no_link["selected"]["current_plans"] = [no_link_report]
        no_link["selected"]["records"] = [no_link_report]
        no_link_html = render_school_plan_report_workspace(no_link)

        self.assertIn("待生成链接", no_link_html)
        self.assertNotIn("data-workspace-copy=", no_link_html)

        missing_pdf = self.renderer_payload()
        missing_pdf_report = missing_pdf["selected"]["selected_report"]
        missing_pdf_report["file_name"] = ""
        missing_pdf_report["has_pdf"] = False
        missing_pdf_report["download_url"] = ""
        missing_pdf["selected"]["current_plans"] = [missing_pdf_report]
        missing_pdf["selected"]["records"] = [missing_pdf_report]
        missing_pdf_html = render_school_plan_report_workspace(missing_pdf)

        self.assertIn("PDF 未生成", missing_pdf_html)
        self.assertNotIn('/download"', missing_pdf_html)

    def test_renderer_ignores_stale_filename_when_pdf_is_missing(self):
        from school_plan_report_workspace_views import render_school_plan_report_workspace

        payload = self.renderer_payload()
        report = payload["selected"]["selected_report"]
        report["file_name"] = "stale-report.pdf"
        report["has_pdf"] = False
        report["download_url"] = ""
        payload["selected"]["current_plans"] = [report]
        payload["selected"]["records"] = [report]

        html = render_school_plan_report_workspace(payload)
        record_file = html.split(
            '<div class="school-plan-workspace__record-file">', 1
        )[1].split("</div>", 1)[0]

        self.assertIn("PDF 未生成", record_file)
        self.assertNotIn("stale-report.pdf", record_file)

    def test_renderer_escapes_selected_and_record_visible_and_attribute_values(self):
        from school_plan_report_workspace_views import render_school_plan_report_workspace

        payload = self.renderer_payload()
        report = payload["selected"]["selected_report"]
        report["city_name"] = 'Melbourne <img src=x onerror="boom">'
        report["share_url"] = 'https://example.test/?q="<bad>"'
        report["detail_url"] = '/school-plan-reports/2?next="<bad>"'
        report["download_url"] = '/school-plan-reports/2/download?name="<bad>"'
        report["file_name"] = '<report "bad">.pdf'
        report["created_by_name"] = '<Admin "bad">'
        report["last_viewed_display"] = '<time>10:00</time>'
        payload["selected"]["current_plans"] = [report]
        payload["selected"]["records"] = [report]

        html = render_school_plan_report_workspace(payload)

        self.assertNotIn('<img src=x onerror="boom">', html)
        self.assertNotIn('q="<bad>"', html)
        self.assertNotIn('<report "bad">.pdf', html)
        self.assertNotIn('<Admin "bad">', html)
        self.assertNotIn("<time>10:00</time>", html)
        self.assertIn("Melbourne &lt;img src=x onerror=&quot;boom&quot;&gt;", html)
        self.assertIn("&lt;report &quot;bad&quot;&gt;.pdf", html)
