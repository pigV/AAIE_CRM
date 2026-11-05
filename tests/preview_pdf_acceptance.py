"""Offline endpoint acceptance. Read one supplied PDF, write only under the project tmp directory."""
import ast
from concurrent.futures import ThreadPoolExecutor
import io
import json
import mimetypes
from pathlib import Path
import re
import subprocess
import sys
import time
from types import SimpleNamespace
from PIL import Image, ImageDraw
import document_preview_cache as cache

ROOT = Path(__file__).resolve().parents[1]


def run(source):
    output = ROOT / 'tmp' / 'study-plan-preview-tests' / 'real-pdf-preview'
    output.mkdir(parents=True, exist_ok=True)
    tree = ast.parse((ROOT / 'app.py').read_text())
    functions = {'_run_pdf_preview_worker', 'pdf_page_count', 'preview_pdf_page_count',
                 'render_pdf_page', 'fit_preview_image', 'preview_target',
                 'preview_render_error_code', 'preview_error_message', 'make_preview'}
    names = {'PREVIEW_CANVAS', 'PREVIEW_MAX_PDF_BYTES', 'PREVIEW_MAX_PDF_PAGES',
             'PREVIEW_PDF_WORKER_TIMEOUT_SECONDS', 'PREVIEW_FALLBACK_MESSAGE', 'PREVIEW_ERROR_MESSAGES'}
    nodes = [n for n in tree.body if
             isinstance(n, ast.FunctionDef) and n.name in functions
             or isinstance(n, ast.ClassDef) and n.name == 'PreviewRenderError'
             or isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets)]
    endpoint = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'document_preview'][-1]
    doc = dict(id=1, student_id='offline', stored_name=source.name,
               file_path=str(source), document_type='FinalConfirmedSchoolPlan')
    ns = dict(Path=Path, io=io, re=re, subprocess=subprocess, sys=sys, Image=Image,
              ImageDraw=ImageDraw, mimetypes=mimetypes, PREVIEW_DIR=output,
              SAFE_PRODUCTION_ACTIVE=True, append_service_log=lambda *a, **k: None,
              log_document_preview_fallback=lambda *a, **k: None,
              db=SimpleNamespace(one=lambda *a: doc, execute=lambda *a: None),
              nas=SimpleNamespace(student_folder=lambda student: source.parent),
              ensure_within=lambda path, folder: Path(path),
              effective_contract_document_for_output=lambda doc, user: doc)
    ns.update({name: getattr(cache, name) for name in
               ('PDF_PREVIEW_SLOTS', 'cached_pdf_page_count', 'load_preview_cache',
                'preview_source_signature', 'save_preview_cache', 'save_preview_image', 'synchronize_preview')})
    exec(compile(ast.Module(body=nodes + [endpoint], type_ignores=[]), 'app.py', 'exec'), ns)
    calls = []
    worker = ns['_run_pdf_preview_worker']
    def tracked_worker(*args):
        calls.append(args[0])
        return worker(*args)
    ns['_run_pdf_preview_worker'] = tracked_worker

    def request(page, size='large'):
        headers = {}
        handler = SimpleNamespace(query=dict(size=[size], page=[str(page)]), user={},
                                  require=lambda permission: True, send_response=lambda status: None,
                                  send_header=headers.__setitem__, end_headers=lambda: None,
                                  wfile=io.BytesIO())
        start = time.monotonic()
        ns['document_preview'](handler, 'GET', '1')
        assert headers['X-Preview-Status'] == 'ok', headers
        with Image.open(io.BytesIO(handler.wfile.getvalue())) as image:
            image.verify()
        return dict(page=page, size=size, total=int(headers['X-Preview-Page-Count']),
                    seconds=round(time.monotonic() - start, 3))

    first = request(1)
    total = first['total']
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(request, range(2, total + 1)))
    thumbs = [request(page, 'thumb') for page in range(1, total + 1)]
    before = len(calls)
    for page in range(1, total + 1):
        request(page)
    assert len(calls) == before, 'Repeat views must not start PDF workers'
    report = dict(pages=total, first=first, rendered_pages=len(results)+1,
                  rendered_thumbnails=len(thumbs), thumbnails=[thumbs[i-1] for i in sorted({1, (total+1)//2, total})], worker_calls=len(calls), count_workers=calls.count('count'),
                  repeated_views_started_workers=len(calls)-before,
                  slowest_request_seconds=max(r['seconds'] for r in [first]+results+thumbs))
    (output.parent / 'real-pdf-acceptance.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    run(Path(sys.argv[1]).resolve(strict=True))
