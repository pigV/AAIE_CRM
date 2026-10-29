"""Isolated map policy checks; never import CRM or open its database."""
import ast
import io
import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
NODE = '/Users/faqiang/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'


def handler_methods(*names):
    tree = ast.parse((ROOT / 'app.py').read_text())
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    functions = []
    for name in names:
        function = next(n for owner in ('CRMHandler', 'CRMLegacyHandler')
                        if owner in classes for n in classes[owner].body
                        if isinstance(n, ast.FunctionDef) and n.name == name)
        functions.append(function)
    namespace = {"STAGING": True}
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<map-only>', 'exec'), namespace)
    return namespace


def map_script():
    source = (ROOT / 'assets/app.js').read_text()
    start = source.index('  qa("[data-school-radius-leaflet], [data-school-living-cost-leaflet]")')
    end = source.index('  qa("[data-living-cost-toggle]")', start)
    return source[start:end]


class SchoolMapTileTests(unittest.TestCase):
    def headers(self, body, explicit=None):
        methods = handler_methods('send_html')
        class Capture:
            user = None
            wfile = io.BytesIO()
            result = {}
            def prepare_html_content(self, content, **kwargs): return content
            def send_response(self, status): self.status = status
            def send_header(self, key, value): self.result[key] = value
            def end_headers(self): pass
        capture = Capture()
        methods['send_html'](capture, body, headers=explicit)
        return capture.result

    def test_map_csp_exact_host_and_privacy(self):
        for marker in ('data-school-radius-leaflet-map', 'data-school-living-cost-leaflet'):
            headers = self.headers('<div '+marker+'></div>')
            self.assertEqual(headers['Referrer-Policy'], 'same-origin')
            self.assertEqual(headers['Content-Security-Policy'], "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; form-action 'self'; frame-src 'self'")

    def test_non_map_policy_unchanged(self):
        headers = self.headers('<p>Student / Contracts</p>')
        self.assertEqual(headers['Referrer-Policy'], 'same-origin')
        self.assertNotIn('https:', headers['Content-Security-Policy'])

    def test_explicit_csp_preserved(self):
        self.assertEqual(self.headers('<div data-school-radius-leaflet-map></div>', [('Content-Security-Policy', "default-src 'none'")])['Content-Security-Policy'], "default-src 'none'")

    def run_map(self, no_leaflet=False):
        harness = r'''
const classes=new Set();const notices=[];let listener=null;let tileOptions=null;let tileURL=null;let markers=0;
const data={center:{latitude:-34.927,longitude:138.587,label:'Adelaide High School'},radius_km:10,schools:[{latitude:-34.91,longitude:138.57,name:'Synthetic School',distance_text:'2.30 km',direction:'NW',coordinate_source:'fixture'}]};
const container={};const shell={classList:{add:x=>classes.add(x),remove:x=>classes.delete(x)},closest:()=>null,appendChild:x=>notices.push(x)};
const document={body:{classList:{contains:()=>false}},createElement:()=>({setAttribute(k,v){this[k]=v;}})};
const q=(s)=>s==='[data-school-radius-leaflet-map]'?container:s==='#school-radius-leaflet-data'?{textContent:JSON.stringify(data)}:null;
const qa=()=>[shell];const escapeHtml=String;const setTimeout=()=>{};
const map={setView(){return this},fitBounds(){},invalidateSize(){}};
const layer={on(event,fn){if(event==='tileerror')listener=fn;return this},addTo(){return this}};
const shape={addTo(){return this},bindPopup(){return this},getBounds(){return []}};
const L={map:()=>map,tileLayer:(url,opts)=>{tileURL=url;tileOptions=opts;return layer},circleMarker:()=>{markers++;return shape},circle:()=>shape,polyline:()=>shape,latLngBounds:()=>({extend(){},isValid:()=>true,pad(){return this}})};
const window={L: NO_LEAFLET ? null : L};
'''.replace('NO_LEAFLET', json.dumps(no_leaflet))
        harness += map_script()
        harness += "\nconst initialReady=classes.has('is-ready');if(listener){listener({error:new Error('HTTP 403 fixture')});listener({error:new Error('network failure fixture')});}console.log(JSON.stringify({tileURL,tileOptions,initialReady,finalReady:classes.has('is-ready'),notices,markers,schools:data.schools}));"
        result = subprocess.run([NODE, '-e', harness], capture_output=True, text=True, timeout=10, check=True)
        return json.loads(result.stdout)

    def test_real_map_component_tile_policy(self):
        result = self.run_map()
        self.assertEqual(result['tileURL'], 'https://tile.openstreetmap.org/{z}/{x}/{y}.png')
        self.assertEqual(result['tileOptions']['referrerPolicy'], 'origin')
        self.assertIn('openstreetmap.org/copyright', result['tileOptions']['attribution'])
        self.assertNotIn('errorTileUrl', result['tileOptions'])
        self.assertTrue(result['initialReady'])

    def test_failed_tile_notice_preserves_markers_and_schools(self):
        result = self.run_map()
        self.assertFalse(result['finalReady'])
        self.assertEqual(len(result['notices']), 1)
        self.assertEqual(result['notices'][0]['role'], 'status')
        self.assertIn('学校信息', result['notices'][0]['textContent'])
        self.assertEqual(result['markers'], 2)
        self.assertEqual(result['schools'][0]['distance_text'], '2.30 km')

    def test_leaflet_unavailable_shows_notice(self):
        result = self.run_map(no_leaflet=True)
        self.assertEqual(len(result['notices']), 1)
        self.assertFalse(result['finalReady'])
        self.assertEqual(result['markers'], 0)


if __name__ == '__main__': unittest.main()
