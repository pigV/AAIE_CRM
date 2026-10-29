import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import database as database_module
from database import Database


class ConnectionTracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.opened = []
        self.closed = []

    def connection_class(self):
        tracker = self

        class TrackingConnection(sqlite3.Connection):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._perf004_closed = False
                with tracker.lock:
                    tracker.opened.append(id(self))

            def close(self):
                if not self._perf004_closed:
                    self._perf004_closed = True
                    with tracker.lock:
                        tracker.closed.append(id(self))
                return super().close()

        return TrackingConnection


class RequestScopedDatabaseTests(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("PERF004_TEST_TMP", "tmp/perf-004-runtime/unit")).resolve()
        base.mkdir(parents=True, exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(prefix="request-scope-", dir=base)
        self.db = Database(Path(self.folder.name) / "request-scope.db")
        with self.db.transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE items (
                    id INTEGER PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO items(value) VALUES ('baseline');
                """
            )
        self.tracker = ConnectionTracker()
        self.real_connect = database_module.sqlite3.connect
        tracking_connection = self.tracker.connection_class()

        def tracked_connect(*args, **kwargs):
            kwargs["factory"] = tracking_connection
            return self.real_connect(*args, **kwargs)

        self.connect_patch = mock.patch.object(database_module.sqlite3, "connect", tracked_connect)
        self.connect_patch.start()

    def tearDown(self):
        self.connect_patch.stop()
        self.folder.cleanup()

    def test_one_request_reuses_one_physical_connection_for_one_all_and_execute(self):
        with self.db.request_scope():
            self.assertEqual(self.db.one("SELECT value FROM items WHERE id=1")["value"], "baseline")
            self.assertEqual(len(self.db.all("SELECT id FROM items")), 1)
            self.db.execute("INSERT INTO items(value) VALUES (?)", ("created",))
            self.assertEqual(self.db.one("SELECT COUNT(*) AS count FROM items")["count"], 2)

        self.assertEqual(len(self.tracker.opened), 1)
        self.assertEqual(self.tracker.closed, self.tracker.opened)

    def test_two_concurrent_requests_use_distinct_physical_connections(self):
        barrier = threading.Barrier(2)
        connection_ids = []
        errors = []

        def worker():
            try:
                with self.db.request_scope():
                    connection = self.db.connect()
                    connection_ids.append(id(connection))
                    barrier.wait(timeout=5)
                    self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
                    barrier.wait(timeout=5)
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(set(connection_ids)), 2)
        self.assertEqual(len(self.tracker.opened), 2)
        self.assertCountEqual(self.tracker.closed, self.tracker.opened)

    def test_request_connection_is_not_shared_with_another_thread(self):
        main_ready = threading.Event()
        worker_done = threading.Event()
        connection_ids = {}

        def worker():
            with self.db.request_scope():
                connection_ids["worker"] = id(self.db.connect())
                main_ready.wait(timeout=5)
            worker_done.set()

        with self.db.request_scope():
            connection_ids["main"] = id(self.db.connect())
            thread = threading.Thread(target=worker)
            thread.start()
            main_ready.set()
            worker_done.wait(timeout=5)
            thread.join(timeout=5)

        self.assertNotEqual(connection_ids["main"], connection_ids["worker"])

    def test_normal_request_exit_closes_connection_and_invalidates_cursor(self):
        with self.db.request_scope():
            connection = self.db.connect()
            cursor = connection.execute("SELECT value FROM items")
            self.assertEqual(cursor.fetchone()[0], "baseline")

        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        with self.assertRaises(sqlite3.ProgrammingError):
            cursor.fetchone()
        self.assertEqual(self.tracker.closed, self.tracker.opened)

    def test_exception_request_exit_closes_connection(self):
        with self.assertRaisesRegex(RuntimeError, "synthetic request error"):
            with self.db.request_scope():
                connection = self.db.connect()
                connection.execute("SELECT 1").fetchone()
                raise RuntimeError("synthetic request error")

        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        self.assertEqual(self.tracker.closed, self.tracker.opened)

    def test_broken_pipe_request_exit_closes_connection(self):
        with self.assertRaises(BrokenPipeError):
            with self.db.request_scope():
                connection = self.db.connect()
                connection.execute("SELECT 1").fetchone()
                raise BrokenPipeError("synthetic client disconnect")

        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
        self.assertEqual(self.tracker.closed, self.tracker.opened)

    def test_transaction_commit_and_rollback_remain_request_local(self):
        with self.db.request_scope():
            with self.db.transaction() as connection:
                connection.execute("INSERT INTO items(value) VALUES ('committed')")
            self.assertEqual(self.db.one("SELECT COUNT(*) AS count FROM items")["count"], 2)

            with self.assertRaisesRegex(ValueError, "rollback"):
                with self.db.transaction() as connection:
                    connection.execute("INSERT INTO items(value) VALUES ('rolled-back')")
                    raise ValueError("rollback")

            self.assertEqual(self.db.one("SELECT COUNT(*) AS count FROM items")["count"], 2)

        self.assertEqual(len(self.tracker.opened), 1)
        self.assertEqual(self.tracker.closed, self.tracker.opened)

    def test_nested_helpers_do_not_open_extra_connections(self):
        def nested_helper():
            return self.db.one("SELECT value FROM items WHERE id=1")["value"]

        with self.db.request_scope():
            self.assertEqual(nested_helper(), "baseline")
            self.assertEqual(nested_helper(), "baseline")
            self.assertEqual(len(self.db.all("SELECT id FROM items")), 1)

        self.assertEqual(len(self.tracker.opened), 1)

    def test_repeated_requests_do_not_leak_connections(self):
        for _ in range(20):
            with self.db.request_scope():
                self.db.one("SELECT 1")

        self.assertEqual(len(self.tracker.opened), 20)
        self.assertCountEqual(self.tracker.closed, self.tracker.opened)

    def test_concurrent_writes_have_no_locked_errors(self):
        errors = []

        def worker(index):
            try:
                with self.db.request_scope():
                    self.db.execute("INSERT INTO items(value) VALUES (?)", (f"worker-{index}",))
                    self.db.all("SELECT id FROM items")
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(self.db.one("SELECT COUNT(*) AS count FROM items")["count"], 4)

    def test_connection_pragmas_and_integrity_are_preserved(self):
        with self.db.request_scope():
            connection = self.db.connect()
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_handler_get_and_broken_pipe_post_close_request_connections(self):
        import app

        class GetHandler:
            def dispatch(inner_self, method):
                self.assertEqual(method, "GET")
                self.db.one("SELECT 1")
                self.db.all("SELECT 1")

        class PostHandler:
            def dispatch(inner_self, method):
                self.assertEqual(method, "POST")
                self.db.one("SELECT 1")
                raise BrokenPipeError("synthetic post disconnect")

        with mock.patch.object(app, "db", self.db):
            app.CRMHandler.do_GET(GetHandler())
            with self.assertRaises(BrokenPipeError):
                app.CRMHandler.do_POST(PostHandler())

        self.assertEqual(len(self.tracker.opened), 2)
        self.assertCountEqual(self.tracker.closed, self.tracker.opened)


if __name__ == "__main__":
    unittest.main()
