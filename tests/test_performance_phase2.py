import io
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse


class StaticAssetCacheTests(unittest.TestCase):
    def test_default_page_asset_urls_are_stable_across_render_times(self):
        import ui

        first = ui.page("First", "<section></section>")
        second = ui.page("Second", "<section></section>")

        first_style = first.split('/assets/style.css?v=', 1)[1].split('"', 1)[0]
        second_style = second.split('/assets/style.css?v=', 1)[1].split('"', 1)[0]
        self.assertEqual(first_style, second_style)

    def test_asset_version_changes_when_asset_content_changes(self):
        import ui

        with tempfile.TemporaryDirectory() as directory:
            asset_root = Path(directory)
            asset = asset_root / "test.css"
            asset.write_text("body{color:#123}", encoding="utf-8")
            first = ui.asset_content_version(asset_root)
            asset.write_text("body{color:#456}", encoding="utf-8")
            second = ui.asset_content_version(asset_root)

        self.assertNotEqual(first, second)

    def asset_handler(self, app_module, url, headers=None):
        handler = object.__new__(app_module.CRMHandler)
        handler.parsed = urlparse(url)
        handler.query = parse_qs(handler.parsed.query)
        handler.headers = headers or {}
        handler.wfile = io.BytesIO()
        handler.response_status = None
        handler.response_headers = []
        handler.send_response = lambda status: setattr(
            handler, "response_status", int(status)
        )
        handler.send_header = lambda key, value: handler.response_headers.append(
            (key, value)
        )
        handler.end_headers = lambda: None
        return handler

    def test_versioned_asset_is_immutable_and_matching_etag_returns_304(self):
        import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            asset_root = Path(directory)
            (asset_root / "test.css").write_text("body{color:#123}", encoding="utf-8")
            with mock.patch.object(app_module, "ASSET_DIR", asset_root), mock.patch.object(
                app_module, "ASSET_VERSION", "bundle-v1", create=True
            ):
                first = self.asset_handler(
                    app_module, "/assets/test.css?v=bundle-v1"
                )
                first.serve_asset()
                headers = dict(first.response_headers)

                self.assertEqual(first.response_status, 200)
                self.assertEqual(
                    headers["Cache-Control"], "public, max-age=31536000, immutable"
                )
                self.assertTrue(headers["ETag"].startswith('"'))
                self.assertEqual(first.wfile.getvalue(), b"body{color:#123}")

                second = self.asset_handler(
                    app_module,
                    "/assets/test.css?v=bundle-v1",
                    {"If-None-Match": headers["ETag"]},
                )
                second.serve_asset()

                self.assertEqual(second.response_status, 304)
                self.assertEqual(second.wfile.getvalue(), b"")
                self.assertEqual(dict(second.response_headers)["ETag"], headers["ETag"])


class TopbarHealthCacheTests(unittest.TestCase):
    def setUp(self):
        import app as app_module

        app_module._TOPBAR_HEALTH_CACHE = {
            "value": None,
            "expires_at": 0.0,
            "refreshing": False,
        }

    def handler(self, app_module):
        handler = object.__new__(app_module.CRMHandler)
        handler.responses = []
        handler.json_response = lambda payload, status=200: handler.responses.append(
            (status, payload)
        )
        handler.error_page = lambda message, status=400: handler.responses.append(
            (status, {"error": str(message)})
        )
        return handler

    def test_topbar_caches_health_but_keeps_active_users_live(self):
        import app as app_module

        handler = self.handler(app_module)
        with mock.patch.object(
            app_module,
            "system_health_snapshot",
            return_value={"headline": "系统正常", "overall_ok": True},
        ) as health, mock.patch.object(
            app_module,
            "active_users_snapshot",
            side_effect=[{"count": 1}, {"count": 2}],
        ) as active, mock.patch.object(app_module.time, "monotonic", return_value=100.0):
            handler.api_topbar_status("GET")
            handler.api_topbar_status("GET")

        self.assertEqual(health.call_count, 1)
        self.assertEqual(active.call_count, 2)
        self.assertEqual(handler.responses[0][1]["active_users"]["count"], 1)
        self.assertEqual(handler.responses[1][1]["active_users"]["count"], 2)

    def test_concurrent_topbar_requests_coalesce_one_health_refresh(self):
        import app as app_module

        started = threading.Event()
        release = threading.Event()
        results = []
        calls = 0
        calls_lock = threading.Lock()

        def expensive_health():
            nonlocal calls
            with calls_lock:
                calls += 1
            started.set()
            release.wait(2)
            return {"headline": "系统正常", "overall_ok": True}

        def worker():
            results.append(app_module.cached_system_health_snapshot())

        with mock.patch.object(app_module, "system_health_snapshot", expensive_health):
            threads = [threading.Thread(target=worker) for _ in range(5)]
            for thread in threads:
                thread.start()
            self.assertTrue(started.wait(1))
            release.set()
            for thread in threads:
                thread.join(2)

        self.assertEqual(calls, 1)
        self.assertEqual(len(results), 5)
        self.assertTrue(all(result["overall_ok"] for result in results))

    def test_expired_cache_refreshes_and_unhealthy_result_uses_short_ttl(self):
        import app as app_module

        app_module._TOPBAR_HEALTH_CACHE = {
            "value": {"headline": "stale", "overall_ok": True},
            "expires_at": 99.0,
            "refreshing": False,
        }
        unhealthy = {"headline": "数据库异常", "overall_ok": False}
        with mock.patch.object(
            app_module, "system_health_snapshot", return_value=unhealthy
        ) as health, mock.patch.object(
            app_module.time, "monotonic", side_effect=[100.0, 100.0]
        ):
            result = app_module.cached_system_health_snapshot()

        self.assertEqual(health.call_count, 1)
        self.assertEqual(result, unhealthy)
        self.assertEqual(app_module._TOPBAR_HEALTH_CACHE["expires_at"], 103.0)


class SessionTouchThrottleTests(unittest.TestCase):
    def test_repeated_session_touches_within_window_create_one_sqlite_write(self):
        import app as app_module

        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE sessions(token TEXT PRIMARY KEY,last_seen_at TEXT)"
        )
        connection.execute(
            "INSERT INTO sessions(token,last_seen_at) VALUES(?,?)",
            ("phase2-session", "2026-01-01T00:00:00+11:00"),
        )
        connection.commit()

        class CountingDatabase:
            def __init__(self, db_connection):
                self.connection = db_connection
                self.writes = 0
                self.execute_calls = 0

            def execute(self, sql, params=()):
                self.execute_calls += 1
                before = self.connection.total_changes
                cursor = self.connection.execute(sql, params)
                self.connection.commit()
                self.writes += self.connection.total_changes - before
                return cursor.lastrowid

        database = CountingDatabase(connection)
        handler = object.__new__(app_module.CRMHandler)
        handler.user = {
            "_session_token": "phase2-session",
            "_session_last_seen_at": "2026-01-01T00:00:00+11:00",
        }
        with mock.patch.object(app_module, "db", database):
            handler.touch_current_session()
            handler.touch_current_session()
            handler.touch_current_session()

        self.assertEqual(database.writes, 1)
        self.assertEqual(database.execute_calls, 1)
        self.assertNotEqual(
            connection.execute(
                "SELECT last_seen_at FROM sessions WHERE token='phase2-session'"
            ).fetchone()[0],
            "2026-01-01T00:00:00+11:00",
        )
        connection.close()


class ContractCentreQueryTests(unittest.TestCase):
    def test_contract_table_bulk_loads_latest_signed_signatures_once(self):
        import app as app_module
        from database import Database

        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "contract-centre.sqlite3")
            database.initialize()
            stamp = "2026-08-17T06:00:00+10:00"
            database.execute(
                """INSERT INTO students(
                       student_id,passport_last_name,passport_first_name,current_status,
                       date_of_birth,created_date,updated_at,form_data_json
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                ("TPERF001", "Query", "Contract", "Active", "2010-01-01", stamp, stamp, "{}"),
            )
            contract_ids = []
            for number in range(3):
                contract_ids.append(
                    database.execute(
                        """INSERT INTO contracts(
                               student_id,contract_uid,contract_category,contract_type,
                               contract_title,contract_status,service_scope_json,generated_by,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            "TPERF001",
                            f"TPERF001-{number}",
                            "留学合同",
                            "StudyAbroadContract",
                            "AAIE 留学合同",
                            "Draft",
                            "[]",
                            1,
                            stamp,
                        ),
                    )
                )
            signature_ids = []
            for contract_id in contract_ids:
                signature_ids.append(
                    database.execute(
                        """INSERT INTO contract_signatures(
                               contract_id,student_id,signed_at,signed_ip,user_agent,
                               signature_image_path,confirmation_statement,status,
                               archive_status,created_at,updated_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            contract_id,
                            "TPERF001",
                            stamp,
                            "127.0.0.1",
                            "phase2-test",
                            "/tmp/signature.png",
                            "confirmed",
                            "signed",
                            "archived",
                            stamp,
                            stamp,
                        ),
                    )
                )

            class CountingDatabase:
                def __init__(self, delegate):
                    self.delegate = delegate
                    self.signature_queries = 0

                def one(self, sql, params=()):
                    if "FROM contract_signatures" in sql:
                        self.signature_queries += 1
                    return self.delegate.one(sql, params)

                def all(self, sql, params=()):
                    if "FROM contract_signatures" in sql:
                        self.signature_queries += 1
                    return self.delegate.all(sql, params)

            counted = CountingDatabase(database)
            handler = object.__new__(app_module.CRMHandler)
            handler.user = {
                "id": 1,
                "username": "admin",
                "display_name": "Admin",
                "role_name": "Admin",
                "permissions_json": '["*"]',
            }
            handler.csrf = "phase2-contract-csrf"
            handler.query = {}
            with mock.patch.object(app_module, "db", counted):
                html = handler.contract_center_table("合同列表")

            self.assertEqual(counted.signature_queries, 1)
            for signature_id in signature_ids:
                self.assertIn(f"/contracts/signatures/{signature_id}/image", html)


if __name__ == "__main__":
    unittest.main()
