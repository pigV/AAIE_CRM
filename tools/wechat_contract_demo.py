"""Synthetic phone acceptance using the real contract handlers, never live data.

Run only through wechat_contract_demo_control.py's native Staging sandbox.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from http.server import HTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
PORT = 18883
MAX_BODY = 2 * 1024 * 1024


def route(path, tokens):
    match = re.fullmatch(r'/contract-sign/([A-Za-z0-9_-]+)(/login|/submit|/final/view|/final/download)?', path)
    if not match or match[1] not in tokens:
        return None
    return match[1], match[2] or ''


def label_demo(content):
    content = content.replace('微信身份已验证', '模拟家长身份（未使用微信 OAuth）')
    content = content.replace('微信身份', '模拟家长身份')
    banner = ('<div style="background:#fff1c7;color:#563d00;padding:14px;text-align:center;'
              'font:15px/1.6 sans-serif">手机签署测试 · 模拟家长身份 · 测试合同，不具法律效力</div>')
    return content.replace('<body>', '<body>' + banner, 1)


def configure(run_root):
    sys.path.insert(0, str(ROOT))
    from staging_environment import ACTIVE as c
    if c is None:
        raise RuntimeError('Native Staging sandbox required')
    c.assert_sandbox()
    run_root = c.check(run_root)
    allowed = c.root / 'wechat-contract-demo'
    if run_root.parent != allowed or not run_root.is_dir():
        raise RuntimeError('Unexpected demo run root')
    import app
    from database import Database
    from nas import NasManager

    class DemoNas(NasManager):
        def active_root(self):
            return run_root / 'storage'

        def status(self):
            return {'mounted': True, 'mode': 'staging', 'production_enabled': False,
                    'production_locked': True, 'root': str(self.active_root()),
                    'root_exists': self.active_root().is_dir(), 'ready': self.active_root().is_dir()}

    app.DB_PATH = run_root / 'demo.db'
    app.db = Database(app.DB_PATH)
    app.nas = DemoNas(app.db)
    app.CONTRACT_SIGNING_DIR = run_root / 'signing'
    app.PREVIEW_DIR = run_root / 'previews'
    app.LOG_DIR = run_root / 'logs'
    # Assert isolation before database initialization or any business operation.
    assert app.db.path.resolve() == run_root / 'demo.db'
    assert app.nas.active_root().resolve() == run_root / 'storage'
    assert app.db.path != c.db
    for name in ('storage', 'signing', 'previews', 'logs'):
        (run_root / name).mkdir(exist_ok=True)
    return app, c


def seed(app, run_root):
    manifest = run_root / 'contracts.json'
    if manifest.exists():
        return json.loads(manifest.read_text())
    if app.db.path.exists():
        raise RuntimeError('Partial demo seed retained; start a new isolated run')
    app.db.initialize()
    assets = app.active_contract_asset_paths()
    if not all(p.is_file() for p in assets):
        raise RuntimeError('Approved company signature/seal unavailable')
    app.db.set_setting('PARENT_FORM_PUBLIC_BASE_URL', f'http://127.0.0.1:{PORT}', None)
    app.db.set_setting('workflow_v2_enabled', 'false', None)
    import contract_domain
    import contract_template_editor as editor
    from nas import STANDARD_FOLDERS
    stamp = app.now_iso()
    output = {'mode': 'synthetic_identity', 'database': str(app.db.path),
              'created_at': stamp, 'contracts': {},
              'asset_hashes': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in assets}}
    for purpose in ('manual', 'automated'):
        student_id = 'WXDEMO-' + purpose.upper()
        folder = run_root / 'storage' / student_id
        folder.mkdir()
        for category in STANDARD_FOLDERS:
            (folder / category).mkdir(exist_ok=True)
        app.db.execute('INSERT INTO students(student_id,passport_first_name,passport_last_name,current_status,created_date,updated_at,folder_path,form_data_json) VALUES(?,?,?,?,?,?,?,?)',
                       (student_id, 'TEST ONLY', 'DEMO STUDENT', 'Active', stamp, stamp, str(folder), '{}'))
        kind = 'NewInternationalStudentContract'
        commercial = editor.authority_v2_commercial(kind, editor._default_commercial())
        commercial.update({'commercial_schema': 'authority_v2', 'gst_treatment': 'EXCLUSIVE',
                           'company_name': 'AAIE / AOYA — TEST ONLY', 'company_abn': '00 000 000 000',
                           'company_address': 'TEST ONLY, Melbourne',
                           'company_contact': 'contracts@example.test'})
        signer = {'source': 'mother', 'name': '测试家长', 'relationship': '母亲',
                  'phone': '0400000000', 'email': 'parent@example.test'}
        instance = {'contract_uid': student_id, 'contract_date': stamp[:10],
                    'student_full_name': 'TEST ONLY — 模拟学生', 'student_date_of_birth': '2011-01-01',
                    'student_id_or_passport_number': student_id, 'student_current_year_level': 'Year 8',
                    'student_applying_year_level': 'Year 9', 'company_authorised_person': 'Sonny Zhang',
                    'parent_address': 'TEST ONLY', 'payment_method': 'TEST ONLY', 'discount_amount': '0.00',
                    'special_terms': '仅用于手机微信签署流程测试。虚拟学生、模拟家长身份；本合同不具法律效力，不用于真实申请、收费或服务。'}
        rendered = editor.render_published_contract_artifacts(
            editor.build_domain_v1_snapshot(kind, commercial), student_name=instance['student_full_name'],
            signer=signer, selected_services=list(contract_domain.default_service_ids(kind)), contract_instance=instance)
        source = folder / 'Forms' / 'TEST_ONLY_contract.pdf'
        source.write_bytes(rendered['pdf_bytes'])
        cid = app.db.execute('INSERT INTO contracts(student_id,contract_uid,contract_category,contract_type,contract_title,contract_status,service_scope_json,contract_file_path,parent_signer_name,standalone_data_json,generated_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                             (student_id, student_id, '留学合同', 'StudyAbroadContract', '手机微信测试合同（无效力）', 'Draft', '[]', str(source), signer['name'], json.dumps({'contract_snapshot': {'signer': signer}}, ensure_ascii=False), stamp, stamp))
        handler = object.__new__(app.CRMHandler)
        link = handler.contract_signing_create_link(cid, days=2)
        output['contracts'][purpose] = {'contract_id': cid, 'token': link['token'],
                                       'path': '/contract-sign/' + link['token'],
                                       'expires_at': link['expires_at']}
    manifest.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    manifest.chmod(0o600)
    return output


def make_handler(app, tokens):
    class Handler(app.CRMHandler):
        server_version = 'AAIEContractDemo/1.0'

        def log_message(self, *_args):
            pass  # Do not put bearer links or drawn signatures into access logs.

        def setup(self):
            super().setup()
            self.connection.settimeout(90)

        def send_html(self, content, status=200, headers=None, **kwargs):
            headers = list(headers or []) + [('X-Robots-Tag', 'noindex, nofollow'), ('Cache-Control', 'no-store')]
            return super().send_html(label_demo(content), status, headers, **kwargs)

        def contract_signing_login_required_page(self, token, link):
            body = f'''<h1>手机合同签署测试</h1>
            <p>本页使用模拟家长身份，不获取真实微信身份。点击下方按钮后，阅读测试合同、手写签名并提交。</p>
            <p>提交后，系统会自动加上公司签名、公章和日期，并显示完整合同及下载按钮。</p>
            <form method="post" action="/contract-sign/{app.esc(token)}/login">
              <input type="hidden" name="wechat_openid" value="demo-{app.esc(token[:20])}">
              <input type="hidden" name="wechat_nickname" value="测试家长">
              <button type="submit">以模拟家长身份查看合同</button>
            </form>'''
            return self.school_plan_share_public_page('手机合同签署测试', body)

        def demo_dispatch(self, method):
            self.user = None
            self.query = {}
            found = route(self.path, tokens)
            if not found:
                return self.send_error(404)
            token, suffix = found
            permitted = {'GET': {'', '/login', '/final/view', '/final/download'},
                         'POST': {'/login', '/submit'}}
            if suffix not in permitted[method]:
                return self.send_error(405)
            if method == 'POST':
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    return self.send_error(400)
                if not 0 < size <= MAX_BODY or self.headers.get('Transfer-Encoding'):
                    return self.send_error(413)
            # Preserve the real handler's identity, signature, archive and final-file gates.
            if suffix == '/login':
                return self.contract_signing_login(method, token)
            if suffix == '/submit':
                return self.contract_signing_submit(method, token)
            if suffix.startswith('/final/'):
                return self.contract_signing_final_pdf(method, token, suffix.rsplit('/', 1)[1])
            return self.contract_signing_portal(method, token)

        def do_GET(self):
            return self.demo_dispatch('GET')

        def do_POST(self):
            return self.demo_dispatch('POST')

        def do_HEAD(self):
            self.send_error(405)

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-root', type=Path, required=True)
    args = parser.parse_args()
    app, c = configure(args.run_root)
    manifest = seed(app, args.run_root)
    tokens = {item['token'] for item in manifest['contracts'].values()}
    server = HTTPServer(('127.0.0.1', PORT), make_handler(app, tokens))
    server.timeout = 1
    print(json.dumps({'ready': True, 'port': PORT, 'run_root': str(args.run_root)}), flush=True)
    # Serial requests avoid duplicate concurrent signing in this small acceptance server.
    deadline = time.monotonic() + 24 * 60 * 60
    try:
        while time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
