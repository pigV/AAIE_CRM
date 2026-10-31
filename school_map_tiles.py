"""Validated OSM tiles and a loopback-only relay for isolated report generation.

The relay accepts only z/x/y coordinates, never a destination URL or headers.
It does not import CRM, open a database, or accept student/report data.
"""
import argparse
import io
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from PIL import Image

MAX_TILE_BYTES = 1024 * 1024
USER_AGENT = 'AAIE-CRM-School-Plan/1.0 (+https://goabroad.com.au; report map renderer)'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(newurl, code, 'Map tile redirects are not allowed', headers, fp)


def validate_coordinates(zoom, x, y):
    if any(type(value) is not int for value in (zoom, x, y)):
        raise ValueError('Map tile coordinates must be integers')
    if not 10 <= zoom <= 14 or not (0 <= x < 2**zoom and 0 <= y < 2**zoom):
        raise ValueError('Map tile coordinates outside report range')


def decode_tile(payload):
    if len(payload) > MAX_TILE_BYTES:
        raise ValueError('Oversized map tile')
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if image.format != 'PNG' or image.size != (256, 256):
                raise ValueError('Map tile must be a 256x256 PNG')
            image.load()
            return image.convert('RGB')
    except (OSError, SyntaxError) as exc:
        raise ValueError('Invalid map tile image') from exc


def tile_payload(root, zoom, x, y, *, relay_port=None):
    validate_coordinates(zoom, x, y)
    cache = Path(root)/'cache/map_tiles/osm'/str(zoom)/str(x)/f'{y}.png'
    # Retain valid tiles (at least OSM's minimum 7 days); no prefetch/background scan.
    try:
        with cache.open('rb') as source:
            payload = source.read(MAX_TILE_BYTES + 1)
        decode_tile(payload)
        return payload
    except (OSError, ValueError):
        pass
    suffix = f'{zoom}/{x}/{y}.png'
    if relay_port is not None:
        if type(relay_port) is not int or not 1024 <= relay_port <= 65535:
            raise ValueError('Invalid local map relay port')
        request = urllib.request.Request(f'http://127.0.0.1:{relay_port}/tiles/{suffix}')
        # Local requests cannot send business credentials; service only returns PNG.
        response = urllib.request.urlopen(request, timeout=15)
    else:
        request = urllib.request.Request('https://tile.openstreetmap.org/'+suffix,
                                         headers={'User-Agent': USER_AGENT, 'Accept': 'image/png'})
        # Do not inherit system proxies or follow redirects to another destination.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        response = opener.open(request, timeout=10)
    with response:
        payload = response.read(MAX_TILE_BYTES + 1)
    decode_tile(payload)
    cache.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=cache.parent, suffix='.part', delete=False) as file:
            pending = Path(file.name)
            file.write(payload)
        os.replace(pending, cache)
    finally:
        if pending is not None and pending.exists():
            pending.unlink()
    return payload


def load_tile(root, zoom, x, y, *, relay_port=None):
    return decode_tile(tile_payload(root, zoom, x, y, relay_port=relay_port))


def make_server(root, port):
    root = Path(root).resolve()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def do_GET(self):
            if self.headers.get('Origin') or self.headers.get('Sec-Fetch-Site'):
                self.send_error(403, 'Server-side map requests only')
                return
            if self.headers.get('Host') not in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}:
                self.send_error(403, 'Loopback host required')
                return
            if self.path == '/health':
                payload = json.dumps({'status': 'ok', 'service': 'school-map-tiles',
                                      'root': str(root), 'pid': os.getpid()}).encode()
                content_type = 'application/json'
            else:
                match = re.fullmatch(r'/tiles/(\d{1,2})/(\d{1,5})/(\d{1,5})\.png', self.path)
                try:
                    if not match:
                        raise ValueError('Expected report tile coordinates')
                    coordinates = tuple(map(int, match.groups()))
                    validate_coordinates(*coordinates)
                except ValueError:
                    self.send_error(400, 'Invalid map tile request')
                    return
                try:
                    payload = tile_payload(root, *coordinates)
                except (OSError, ValueError) as exc:
                    self.log_error('Map tile failed: %s', exc)
                    self.send_error(502, 'Map tile unavailable')
                    return
                content_type = 'image/png'
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(payload)

    # Serial downloads keep upstream concurrency bounded and cache publication simple.
    return HTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--runtime-config')
    args = parser.parse_args()
    expected = Path(__file__).resolve().parent/'STAGING_DATA/map_tile_service'
    if args.runtime_config:
        from runtime_configuration import RuntimeConfiguration, canonical
        config = RuntimeConfiguration.load(args.runtime_config, source_root=Path(__file__).resolve().parent)
        expected = config.root / 'map_tile_service'
        if canonical(args.root) != expected or args.port != config.map_port:
            parser.error('Managed map service identity mismatch')
    elif args.root.resolve() != expected or args.root.is_symlink():
        parser.error('Map service must use this checkout STAGING_DATA/map_tile_service')
    with make_server(args.root, args.port) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
