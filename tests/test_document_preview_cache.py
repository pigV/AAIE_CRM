"""Exercise the real endpoint without importing CRM or accessing its database."""
import ast
import io
import mimetypes
from pathlib import Path
import re
import tempfile
import threading
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]


class DocumentPreviewCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT / 'tmp' / 'study-plan-preview-tests').mkdir(parents=True, exist_ok=True)
        tree = ast.parse((ROOT / 'app.py').read_text())
        handlers = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'document_preview']
        cls.endpoint = compile(ast.Module(body=[handlers[-1]], type_ignores=[]), 'app.py', 'exec')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / 'tmp' / 'study-plan-preview-tests')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'confirmed.pdf'
        self.source.write_bytes(b'pdf version one')
        self.doc = dict(id=1, student_id='test', stored_name='confirmed.pdf',
                        document_type='FinalConfirmedSchoolPlan', file_path=str(self.source))
        self.counts = []
        self.renders = []
        self.render_fails = False
        self.ns = dict(Path=Path, re=re, mimetypes=mimetypes, SAFE_PRODUCTION_ACTIVE=True,
                       PREVIEW_DIR=self.root, db=SimpleNamespace(one=lambda *a: self.doc, execute=lambda *a: None),
                       nas=SimpleNamespace(student_folder=lambda s: self.root),
                       ensure_within=lambda path, folder: Path(path),
                       effective_contract_document_for_output=lambda doc, user: doc,
                       append_service_log=lambda *a, **k: None, log_document_preview_fallback=lambda *a, **k: None,
                       preview_render_error_code=lambda exc: 'pdf_render_failed',
                       preview_target=lambda doc, size, page: self.root / f'{doc}_{size}_{page}.png',
                       preview_pdf_page_count=self.count, make_preview=self.render)
        try:
            import document_preview_cache
            document_preview_cache._page_counts.clear()
            self.ns.update({name: getattr(document_preview_cache, name)
                            for name in ('load_preview_cache', 'save_preview_cache', 'cached_pdf_page_count', 'preview_source_signature', 'synchronize_preview')})
        except ImportError:
            pass
        exec(self.endpoint, self.ns)

    def count(self, data, *args):
        self.counts.append(data)
        return 34

    def render(self, doc, name, data, content_type, **kwargs):
        self.renders.append(kwargs['page'])
        if self.render_fails:
            kwargs['error_collector'].append('pdf_render_failed')
        path = self.ns['preview_target'](doc, kwargs['size'], kwargs['page'])
        path.write_bytes(b'fallback' if self.render_fails else b'actual page')
        return str(path)

    def request(self, page=1):
        headers = {}
        handler = SimpleNamespace(query={'size': ['large'], 'page': [str(page)]}, user={},
                                  require=lambda p: True, send_response=lambda s: None,
                                  send_header=headers.__setitem__, end_headers=lambda: None,
                                  wfile=io.BytesIO())
        self.ns['document_preview'](handler, 'GET', '1')
        return headers, handler.wfile.getvalue()

    def test_repeat_page_reuses_successful_render_and_page_count(self):
        self.request(); self.request(); self.request(2)
        self.assertEqual(self.renders, [1, 2])
        self.assertEqual(len(self.counts), 1)

    def test_concurrent_failure_cannot_be_certified_by_successful_request(self):
        ready = threading.Event()
        release = threading.Event()
        started = threading.Event()
        completed = threading.Event()
        original_save = self.ns['save_preview_cache']
        def paused_save(*args):
            ready.set()
            if not release.wait(3):
                raise AssertionError('test coordinator did not release publication')
            return original_save(*args)
        self.ns['save_preview_cache'] = paused_save
        errors = []
        def request():
            try:
                self.request()
            except BaseException as error:
                errors.append(error)
        first = threading.Thread(target=request)
        first.start()
        self.assertTrue(ready.wait(3))
        self.render_fails = True
        def competing_request():
            started.set()
            try:
                request()
            finally:
                completed.set()
        second = threading.Thread(target=competing_request)
        second.start()
        try:
            self.assertTrue(started.wait(3))
            completed.wait(0.15)
        finally:
            release.set()
            first.join(3)
            second.join(3)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.render_fails = False
        headers, body = self.request()
        self.assertEqual(headers['X-Preview-Status'], 'ok')
        self.assertEqual(body, b'actual page', 'a failed placeholder must never get a success marker')

    def test_source_change_invalidates_count_and_images(self):
        self.request()
        self.source.write_bytes(b'pdf version two with changed contents')
        headers, body = self.request()
        self.assertEqual(headers['X-Preview-Page-Count'], '34')
        self.assertEqual(body, b'actual page')
        self.assertEqual(len(self.renders), 2)
        self.assertEqual(len(self.counts), 2)

    def test_failed_render_is_retried_and_page_count_survives(self):
        self.render_fails = True
        headers, _ = self.request(12)
        self.assertEqual(headers['X-Preview-Status'], 'error')
        self.assertEqual(headers['X-Preview-Page-Count'], '34')
        self.render_fails = False
        headers, body = self.request(12)
        self.assertEqual(headers['X-Preview-Status'], 'ok')
        self.assertEqual(body, b'actual page')
        self.assertEqual(self.renders, [12, 12])
        self.assertEqual(len(self.counts), 1)

    def test_legacy_fallback_without_metadata_is_never_trusted(self):
        self.ns['preview_target']('1', 'large', 1).write_bytes(b'old fallback')
        _, body = self.request()
        self.assertEqual(body, b'actual page')

    def test_legacy_image_fallback_is_regenerated_without_success_metadata(self):
        self.doc['stored_name'] = 'confirmed.png'
        self.ns['preview_target']('1', 'large', 1).write_bytes(b'old fallback')
        _, body = self.request()
        self.assertEqual(body, b'actual page')

    def test_replaced_image_invalidates_success_marker(self):
        self.request()
        self.ns['preview_target']('1', 'large', 1).write_bytes(b'replaced fallback')
        _, body = self.request()
        self.assertEqual(body, b'actual page')
        self.assertEqual(len(self.renders), 2)


if __name__ == '__main__':
    unittest.main()
