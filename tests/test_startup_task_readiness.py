"""Real socket acceptance for optional startup work; no CRM import or data access.

Regression caught: running a slow optional backup on the serving thread prevents
health from responding. Compile the real run_server definition with boundary
substitutes so the test cannot initialize a Production database or read secrets.
"""
import ast
import json
import os
from pathlib import Path
from socketserver import TCPServer
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class StartupTaskReadinessTests(unittest.TestCase):
    def exercise(self, service='admin', *, safe=False, task_error=False):
        release = threading.Event()
        task_started = threading.Event()
        task_finished = threading.Event()
        bound = threading.Event()
        servers, errors, logs, calls = [], [], [], []

        class Health(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"status":"ok"}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            def server_bind(self):
                # This test measures application startup ordering. Avoid
                # HTTPServer's reverse-DNS lookup, which is unrelated to the
                # listener contract and can block in an isolated macOS VM.
                TCPServer.server_bind(self)
                self.server_name, self.server_port = self.server_address[:2]
            def __init__(self, address, handler):
                super().__init__(address, handler)
                servers.append(self)
                bound.set()

        def task():
            calls.append(service)
            task_started.set()
            release.wait(3)
            task_finished.set()
            if task_error:
                raise RuntimeError('synthetic startup task failure')

        noop = lambda *a, **k: None
        ns = dict(SAFE_PRODUCTION_ACTIVE=False, STAGING=False,
                  APP_WRITE_GATE=object(), startup_readonly=lambda gate: False,
                  ensure_dirs=noop,
                  recover_interrupted_content_extraction_jobs=lambda: 0,
                  run_reminder_engine=task, start_reminder_engine_thread=noop,
                  start_data_update_auto_check_thread=noop, startup_backup=task,
                  append_service_log=lambda *a, **k: logs.append((a, k)),
                  CLASS_TWO_TASKS=('reminder', 'backup'),
                  wechat_oauth_startup_messages=lambda: [],
                  crm_admin_lan_url=lambda: 'http://127.0.0.1',
                  ReusableThreadingHTTPServer=Server, CRMHandler=Health,
                  VERSION='test', os=os, threading=threading, print=noop)
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'app.py').read_text())
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name in {'run_server', 'start_optional_startup_task'}]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'app.py', 'exec'), ns)

        def run():
            try:
                ns['run_server']('127.0.0.1', 0, service,
                                 safe_production=safe, startup_preflight_done=True)
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            self.assertTrue(bound.wait(0.6), 'optional startup task blocked socket bind')
            with urllib.request.urlopen(f'http://127.0.0.1:{servers[0].server_port}/health', timeout=0.5) as r:
                self.assertEqual(json.load(r), {'status': 'ok'})
            if safe:
                self.assertFalse(task_started.is_set())
            else:
                self.assertTrue(task_started.wait(0.6))
                self.assertFalse(task_finished.is_set(), 'test task must still be pending at health PASS')
                release.set()
                self.assertTrue(task_finished.wait(1))
            self.assertEqual(calls, [] if safe else [service])
        finally:
            release.set()
            if bound.wait(3) and servers:
                servers[0].shutdown()
                servers[0].server_close()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        if task_error:
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and not any('startup_task_error' in str(x) for x in logs):
                time.sleep(0.01)
            self.assertTrue(any('startup_task_error' in str(x) for x in logs))

    def test_slow_admin_backup_does_not_block_health(self):
        self.exercise()

    def test_safe_production_does_not_start_optional_task(self):
        self.exercise(safe=True)

    def test_backup_failure_is_logged_without_stopping_service(self):
        self.exercise(task_error=True)

    def test_real_backup_oserror_is_logged(self):
        from datetime import datetime
        from types import SimpleNamespace
        logs = []
        def failed_backup(*args, **kwargs):
            raise OSError("synthetic backup write failure")
        ns = dict(SAFE_PRODUCTION_ACTIVE=False, datetime=datetime,
                  backup_storage_dir=lambda: SimpleNamespace(glob=lambda pattern: []),
                  create_system_backup=failed_backup,
                  append_service_log=lambda *args, **kwargs: logs.append((args, kwargs)))
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'app.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'startup_backup')
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'app.py', 'exec'), ns)
        self.assertIsNone(ns['startup_backup']())
        self.assertTrue(any('startup_task_error' in str(row) and 'synthetic backup write failure' in str(row) for row in logs))
