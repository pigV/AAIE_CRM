import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from tests.test_school_report_static_map import tile_bytes, ROOT


class MapTileServiceTests(unittest.TestCase):
    def setUp(self):
        import school_map_tiles
        self.tiles = school_map_tiles
        self.folder = tempfile.TemporaryDirectory(dir=ROOT/'STAGING_DATA/tmp')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def test_corrupt_cache_is_replaced_only_by_valid_tile(self):
        cache = self.root/'cache/map_tiles/osm/12/3697/2513.png'
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b'broken png' * 100)
        with patch('urllib.request.urlopen', return_value=io.BytesIO(tile_bytes())):
            result = self.tiles.load_tile(self.root, 12, 3697, 2513, relay_port=18893)
        self.assertEqual(result.size, (256, 256))
        self.assertEqual(cache.read_bytes(), tile_bytes())
        with patch('urllib.request.urlopen', side_effect=AssertionError('Cache should prevent network')):
            self.assertEqual(self.tiles.load_tile(self.root, 12, 3697, 2513, relay_port=18893).size, (256, 256))

    def test_invalid_response_never_poison_cache(self):
        for payload in (b'<html>unavailable</html>', b'X' * (1024*1024+1)):
            with patch('urllib.request.urlopen', return_value=io.BytesIO(payload)):
                with self.assertRaises(ValueError):
                    self.tiles.load_tile(self.root, 12, 3697, 2513, relay_port=18893)
        self.assertFalse((self.root/'cache/map_tiles/osm/12/3697/2513.png').exists())

    def test_network_failure_can_recover_on_next_request(self):
        with patch('urllib.request.urlopen', side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                self.tiles.load_tile(self.root, 12, 3697, 2513, relay_port=18893)
        with patch('urllib.request.urlopen', return_value=io.BytesIO(tile_bytes())):
            self.assertEqual(self.tiles.load_tile(self.root, 12, 3697, 2513, relay_port=18893).size, (256,256))

    def server(self):
        server = self.tiles.make_server(self.root, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}'

    def test_service_rejects_arbitrary_urls_paths_and_invalid_coordinates(self):
        base = self.server()
        for path in ('/tiles/12/3697/2513.png?url=https://example.com',
                     '/tiles/12/99999/0.png', '/tiles/15/1/1.png',
                     '/tiles/12/../secret', '/https://example.com'):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base+path, timeout=3)
            self.assertEqual(caught.exception.code, 400)

    def test_service_serves_validated_cached_png_and_health_identity(self):
        cache = self.root/'cache/map_tiles/osm/12/3697/2513.png'
        cache.parent.mkdir(parents=True)
        cache.write_bytes(tile_bytes())
        base = self.server()
        with urllib.request.urlopen(base+'/tiles/12/3697/2513.png', timeout=3) as response:
            self.assertEqual(response.headers['Content-Type'], 'image/png')
            self.assertEqual(response.read(), tile_bytes())
        with urllib.request.urlopen(base+'/health', timeout=3) as response:
            self.assertEqual(json.load(response)['root'], str(self.root))

    def test_redirect_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.tiles.NoRedirect().redirect_request(None, None, 302, 'redirect', {}, 'http://127.0.0.1:8780')


if __name__ == '__main__': unittest.main()
