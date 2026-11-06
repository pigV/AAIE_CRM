"""Exercise verification routing/files without importing production app or DB."""
import ast
import io
import re
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / "app.py").read_text())
HANDLER = next(n for n in TREE.body if isinstance(n, ast.ClassDef)
               and n.name == "CRMLegacyHandler")
FILENAME = "9edd4124efadbcd28f3214297b13630c.txt"
PAYLOAD = b"synthetic-site-owner-proof"


class WechatRecoveryVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.verify = self.root / "verification"
        self.verify.mkdir()
        self.ns = dict(Path=Path, re=re, urlparse=urlparse, parse_qs=parse_qs,
                       HTTPStatus=HTTPStatus, WECHAT_VERIFY_DIR=self.verify,
                       GOOGLE_DRIVE_OAUTH_CALLBACK_PATH="/unused-google-callback",
                       MaintenanceError=type("MaintenanceError", (Exception,), {}))
        for node in TREE.body:
            if (isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id in {"WECHAT_RECOVERY_VERIFY_FILENAME", "WECHAT_RECOVERY_VERIFY_FILENAMES"}
                    for t in node.targets)):
                exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), self.ns)
        methods = [n for n in HANDLER.body if isinstance(n, ast.FunctionDef)
                   and n.name in {"dispatch", "serve_wechat_verify_file", "text"}]
        for node in methods:
            exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), self.ns)
        self.cls = type("Handler", (), {n.name: self.ns[n.name] for n in methods})

    def request(self, path, method="GET"):
        h = self.cls()
        h.path = path
        h.server = SimpleNamespace(service_mode="parent")
        h.wfile = io.BytesIO()
        h.status = None
        h.response_headers = {}
        h.send_response = lambda status: setattr(h, "status", int(status))
        h.send_header = lambda key, value: h.response_headers.update({key: value})
        h.end_headers = lambda: None
        h.current_user = lambda: (None, "")
        h.dispatch_parent_portal = lambda method: h.text("business-route", 404)
        h.dispatch(method)
        return h.status, h.response_headers, h.wfile.getvalue()

    def test_recovery_file_returns_exact_bytes_without_login(self):
        (self.verify / FILENAME).write_bytes(PAYLOAD)
        status, headers, body = self.request("/" + FILENAME)
        self.assertEqual(status, 200)
        self.assertEqual(body, PAYLOAD)
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(int(headers["Content-Length"]), len(PAYLOAD))

    def test_current_application_file_returns_exact_bytes_without_login(self):
        name = "a15ebf69af69739a2d96635e629b73e9.txt"
        (self.verify / name).write_bytes(PAYLOAD)
        status, headers, body = self.request("/" + name)
        self.assertEqual(status, 200)
        self.assertEqual(body, PAYLOAD)
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(int(headers["Content-Length"]), len(PAYLOAD))

    def test_current_application_post_is_rejected(self):
        name = "a15ebf69af69739a2d96635e629b73e9.txt"
        (self.verify / name).write_bytes(PAYLOAD)
        self.assertEqual(self.request("/" + name, "POST")[0], 405)

    def test_current_application_missing_file_is_not_found(self):
        self.assertEqual(self.request("/a15ebf69af69739a2d96635e629b73e9.txt")[0], 404)

    def test_current_application_symlink_cannot_escape_directory(self):
        name = "a15ebf69af69739a2d96635e629b73e9.txt"
        outside = self.root / "private.txt"
        outside.write_bytes(b"private")
        (self.verify / name).symlink_to(outside)
        self.assertEqual(self.request("/" + name)[0], 404)

    def test_current_application_path_traversal_is_not_served(self):
        name = "a15ebf69af69739a2d96635e629b73e9.txt"
        (self.verify / name).write_bytes(PAYLOAD)
        for path in ["/../" + name, "/%2e%2e/" + name, "/other/" + name]:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_legacy_mp_verification_still_works(self):
        (self.verify / "MP_verify_example.txt").write_bytes(b"legacy-proof")
        status, _, body = self.request("/MP_verify_example.txt")
        self.assertEqual((status, body), (200, b"legacy-proof"))

    def test_missing_recovery_file_is_not_found(self):
        self.assertEqual(self.request("/" + FILENAME)[0], 404)

    def test_other_text_files_are_not_exposed(self):
        for name in ["private.txt", "a" * 32 + ".txt"]:
            (self.verify / name).write_bytes(b"private")
            self.assertEqual(self.request("/" + name)[0], 404)

    def test_recovery_file_symlink_cannot_escape_directory(self):
        outside = self.root / "private.txt"
        outside.write_bytes(b"private")
        (self.verify / FILENAME).symlink_to(outside)
        self.assertEqual(self.request("/" + FILENAME)[0], 404)

    def test_path_traversal_is_not_served(self):
        (self.verify / FILENAME).write_bytes(PAYLOAD)
        for path in ["/../" + FILENAME, "/%2e%2e/" + FILENAME, "/other/" + FILENAME]:
            self.assertEqual(self.request(path)[0], 404)

    def test_post_to_recovery_file_is_rejected(self):
        (self.verify / FILENAME).write_bytes(PAYLOAD)
        self.assertEqual(self.request("/" + FILENAME, "POST")[0], 405)


if __name__ == "__main__":
    unittest.main()
