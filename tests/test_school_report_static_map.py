"""Exercise report rendering without importing CRM or opening a business database."""
import base64
import ast
import importlib.util
import io
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request

from PIL import Image, ImageDraw, ImageFont
from tests.test_school_map_tiles import handler_methods

ROOT = Path(__file__).resolve().parents[1]


def tile_bytes():
    image = Image.new('RGB', (256, 256), '#dbedfa')
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 20, 200, 80), fill='#b8d2a0')
    draw.line((0, 100, 256, 210), fill='white', width=12)
    output = io.BytesIO()
    image.save(output, 'PNG')
    return output.getvalue()


class ReportStaticMapTests(unittest.TestCase):
    def render(self, root):
        ns = handler_methods('school_plan_snapshot_static_map_data_uri',
                             'school_plan_map_font', 'school_plan_validate_map_image_bytes')
        ns.update(APP_DIR=root, STAGING=SimpleNamespace(admin_port=18880),
                  Image=Image, ImageDraw=ImageDraw, ImageFont=ImageFont,
                  Path=Path, math=math, io=io, base64=base64, urllib=urllib,
                  SCHOOL_CBD_COORDS={'VIC': {'latitude': -37.8136, 'longitude': 144.9631, 'label': 'Melbourne CBD'}},
                  living_cost_haversine_km=lambda *args: 10.1,
                  school_fee_row_value=lambda row, key, default: row.get(key, default))
        # New helper is loaded when present; the baseline test still runs old code.
        if importlib.util.find_spec('school_map_tiles'):
            from school_map_tiles import load_tile
            ns['load_school_map_tile'] = load_tile
        methods = {name: value for name, value in ns.items() if name.startswith('school_plan_')}
        handler = type('MapHandler', (), methods)()
        return handler.school_plan_snapshot_static_map_data_uri({
            'school_name': 'Sample School', 'state': 'VIC',
            'latitude': -37.75, 'longitude': 145.04})

    def test_report_uses_local_relay_when_external_network_is_blocked(self):
        def local_only(request, **kwargs):
            url = request.full_url if hasattr(request, 'full_url') else request
            if not url.startswith('http://127.0.0.1:18893/tiles/'):
                raise PermissionError('CRM external network is blocked')
            return io.BytesIO(tile_bytes())
        with tempfile.TemporaryDirectory(dir=ROOT/'STAGING_DATA/tmp') as folder:
            with patch.object(urllib.request, 'urlopen', side_effect=local_only):
                try:
                    image_uri = self.render(Path(folder))
                except PermissionError as exc:
                    self.fail(f'Report bypassed the local map relay: {exc}')
        self.assertTrue(image_uri.startswith('data:image/png;base64,'))
        image = Image.open(io.BytesIO(base64.b64decode(image_uri.split(',', 1)[1])))
        self.assertEqual(image.size, (1200, 800))
        self.assertGreater(len(image.getcolors(1000000)), 30)

    def test_pdf_map_component_embeds_image_instead_of_placeholder(self):
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.lib.utils import ImageReader
        from reportlab.platypus import Flowable, SimpleDocTemplate
        from pypdf import PdfReader
        tree = ast.parse((ROOT/'app.py').read_text())
        component = next(node for node in ast.walk(tree)
                         if isinstance(node, ast.ClassDef) and node.name == 'RadiusMapFlowable')
        ns = dict(Flowable=Flowable, mm=mm, colors=colors, io=io,
                  base64=base64, ImageReader=ImageReader, font='Helvetica')
        exec(compile(ast.Module(body=[component], type_ignores=[]), '<pdf-map>', 'exec'), ns)
        with tempfile.TemporaryDirectory(dir=ROOT/'STAGING_DATA/tmp') as folder:
            with patch.object(urllib.request, 'urlopen', side_effect=lambda *a, **kw: io.BytesIO(tile_bytes())):
                uri = self.render(Path(folder))
            target = Path(folder)/'map.pdf'
            SimpleDocTemplate(str(target)).build([ns['RadiusMapFlowable'](uri)])
            reader = PdfReader(target)
            images = list(reader.pages[0].images)
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0].image.size, (1200, 800))


if __name__ == '__main__':
    unittest.main()
