import gzip
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse


class StaticAssetEncodingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.asset = self.root / 'test.css'
        self.raw = b'body { color: #123456; background: white; }\n' * 400
        self.asset.write_bytes(self.raw)

    def request(self, encoding='', etag=None, filename='test.css'):
        import app
        h = object.__new__(app.CRMHandler)
        h.parsed = urlparse('/assets/' + filename)
        h.query = {'v': ['bundle-v1']}
        h.headers = {'Accept-Encoding': encoding}
        if etag:
            h.headers['If-None-Match'] = etag
        h.wfile = io.BytesIO()
        h.response_headers = []
        h.send_response = lambda status: setattr(h, 'status', int(status))
        h.send_header = lambda k, v: h.response_headers.append((k, v))
        h.end_headers = lambda: None
        with mock.patch.object(app, 'ASSET_DIR', self.root), mock.patch.object(app, 'ASSET_VERSION', 'bundle-v1'):
            h.serve_asset()
        return h, dict(h.response_headers), h.wfile.getvalue()

    def test_gzip_preserves_bytes_and_version_cache_policy(self):
        h, headers, payload = self.request('br, gzip')
        self.assertEqual(h.status, 200)
        self.assertEqual(headers.get('Content-Encoding'), 'gzip')
        self.assertEqual(gzip.decompress(payload), self.raw)
        self.assertEqual(int(headers['Content-Length']), len(payload))
        self.assertIn('Accept-Encoding', headers.get('Vary', ''))
        self.assertEqual(headers['Cache-Control'], 'public, max-age=31536000, immutable')

    def test_negotiation_and_original_fallback(self):
        for value in ('', 'br', 'gzip;q=0', '*;q=1, gzip;q=0', 'gzip;q=invalid'):
            with self.subTest(value=value):
                _, headers, payload = self.request(value)
                self.assertNotIn('Content-Encoding', headers)
                self.assertEqual(payload, self.raw)
        for value in ('GZip', 'gzip; q=0.5', '*;q=0.7'):
            with self.subTest(value=value):
                _, headers, payload = self.request(value)
                self.assertEqual(headers.get('Content-Encoding'), 'gzip')
                self.assertEqual(gzip.decompress(payload), self.raw)

    def test_representation_etags_and_conditional_requests(self):
        _, plain, _ = self.request()
        _, zipped, _ = self.request('gzip')
        self.assertNotEqual(plain['ETag'], zipped['ETag'])
        h, headers, payload = self.request('gzip', zipped['ETag'])
        self.assertEqual(h.status, 304)
        self.assertEqual(payload, b'')
        self.assertIn('Accept-Encoding', headers['Vary'])
        h, _, payload = self.request('', zipped['ETag'])
        self.assertEqual(h.status, 200)
        self.assertEqual(payload, self.raw)

    def test_file_change_invalidates_compressed_cache(self):
        _, first, _ = self.request('gzip')
        changed = self.raw.replace(b'white', b'black')
        self.asset.write_bytes(changed)
        _, second, payload = self.request('gzip', first['ETag'])
        self.assertNotEqual(first['ETag'], second['ETag'])
        self.assertEqual(gzip.decompress(payload), changed)

    def test_identity_304_never_reads_file_body(self):
        for name in ('test.css', 'test.pdf'):
            (self.root / name).write_bytes(self.raw)
            _, headers, _ = self.request(filename=name)
            with mock.patch.object(Path, 'read_bytes', side_effect=AssertionError('304 read body')):
                h, _, payload = self.request(etag=headers['ETag'], filename=name)
            self.assertEqual(h.status, 304)
            self.assertEqual(payload, b'')

    def test_binary_and_small_assets_remain_uncompressed(self):
        for name, data in [('test.pdf', self.raw), ('small.css', b'a{}')]:
            (self.root / name).write_bytes(data)
            _, headers, payload = self.request('gzip', filename=name)
            self.assertNotIn('Content-Encoding', headers)
            self.assertEqual(payload, data)

    def test_head_retains_headers_without_writing_body(self):
        import app
        _, get_headers, _ = self.request('gzip')
        h = object.__new__(app.CRMHandler)
        h.wfile = io.BytesIO()
        h._head_original_wfile = h.wfile
        h.parsed = urlparse('/assets/test.css')
        h.query = {'v': ['bundle-v1']}
        h.headers = {'Accept-Encoding': 'gzip'}
        h.response_headers = []
        h.send_response = lambda status: None
        h.send_header = lambda k, v: h.response_headers.append((k, v))
        h.end_headers = lambda: setattr(h, 'wfile', app._HeadBodySink(h._head_original_wfile))
        with mock.patch.object(app, 'ASSET_DIR', self.root), mock.patch.object(app, 'ASSET_VERSION', 'bundle-v1'):
            h.serve_asset()
        self.assertEqual(h._head_original_wfile.getvalue(), b'')
        self.assertEqual(dict(h.response_headers)['Content-Length'], get_headers['Content-Length'])

    def test_concurrent_cold_requests_compress_once(self):
        from concurrent.futures import ThreadPoolExecutor
        import static_asset_encoding as encoding
        with mock.patch.object(encoding.gzip, 'compress', wraps=gzip.compress) as compress:
            with ThreadPoolExecutor(max_workers=8) as pool:
                values = list(pool.map(lambda _: encoding.asset_representation(self.asset, 'gzip'), range(12)))
        self.assertEqual(compress.call_count, 1)
        self.assertTrue(all(gzip.decompress(value[0]) == self.raw for value in values))

    def test_cache_is_bounded_and_mtime_restore_does_not_reuse_old_bytes(self):
        import os
        import static_asset_encoding as encoding
        initial = self.asset.stat()
        encoding.asset_representation(self.asset, 'gzip')
        changed = self.raw.replace(b'white', b'black')
        self.asset.write_bytes(changed)
        os.utime(self.asset, ns=(initial.st_atime_ns, initial.st_mtime_ns))
        value = encoding.asset_representation(self.asset, 'gzip')
        self.assertEqual(gzip.decompress(value[0]), changed)
        with mock.patch.object(encoding, 'MAX_CACHE_BYTES', 500), mock.patch.object(encoding, 'MAX_CACHE_ENTRIES', 2):
            for i in range(6):
                asset = self.root / f'{i}.css'
                asset.write_bytes(self.raw + str(i).encode())
                encoding.asset_representation(asset, 'gzip')
            with encoding._lock:
                self.assertLessEqual(len(encoding._cache), 2)
                self.assertLessEqual(encoding._cache_bytes, 500)
                self.assertEqual(encoding._cache_bytes, sum(len(v[0]) for v in encoding._cache.values()))
