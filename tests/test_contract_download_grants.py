"""Real handler/cipher/file tests; no production app import, DB or credentials."""
import ast
import hashlib
import hmac
import html
import io
import http.client
import json
import sys
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlparse
from unittest.mock import patch

from cryptography.fernet import Fernet
from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TREE = ast.parse((ROOT / 'app.py').read_text())
HANDLER = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == 'CRMLegacyHandler')


class ContractDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'final.pdf'
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=400)
        writer.write(self.path)
        self.cipher = Fernet(Fernet.generate_key())
        self.link = dict(id=11, contract_id=22, signed_signature_id=33,
                         token_hash='synthetic-link-hash', bound_wechat_openid='parent-a',
                         status='signed', contract_status='FullyExecuted',
                         invalidated_at=None, invalidated_reason=None,
                         deleted_at=None, voided_at=None)
        self.ns = dict(hashlib=hashlib, safe_compare=hmac.compare_digest,
                       SimpleCookie=SimpleCookie, HTTPStatus=HTTPStatus, esc=html.escape,
                       quote=quote, Path=Path, PdfReader=PdfReader,
                       normalize_contract_status=lambda s:s, fernet=lambda:self.cipher,
                       parse_local_datetime=lambda s:datetime.fromisoformat(s) if s else None,
                       datetime=datetime, is_expired=lambda value:datetime.fromisoformat(value)<datetime.now().astimezone(), now_iso=lambda:datetime.now().astimezone().isoformat())
        if (ROOT / 'contract_download_grants.py').exists():
            import contract_download_grants as grants
            self.ns.update(issue_contract_download_grant=grants.issue_contract_download_grant,
                           verify_contract_download_grant=grants.verify_contract_download_grant,
                           redact_contract_download_grants=grants.redact_contract_download_grants)
        names = {'contract_signing_cookie_name', 'contract_signing_cookie_value',
                 'contract_signing_cookie_valid', 'contract_signing_status_problem',
                 'contract_signing_final_pdf', 'contract_signing_final_pdf_path', 'contract_signing_final_actions',
                 'contract_signing_submit', 'contract_signing_portal', 'log_message'}
        methods = [n for n in HANDLER.body if isinstance(n, ast.FunctionDef) and n.name in names]
        module = ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(
            name='TestHandler', bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[]))
        exec(compile(module, str(ROOT/'app.py'), 'exec'), self.ns)

    def handler(self, authenticated=False, grant=None):
        h = self.ns['TestHandler']()
        h.headers = {}
        if authenticated:
            h.headers['Cookie'] = 'aaie_csl_11=' + h.contract_signing_cookie_value(self.link, 'parent-a')
        h.query = {} if grant is None else {'download_grant':[grant]}
        h.wfile = io.BytesIO()
        h.response = {'status':None, 'headers':{}}
        h.contract_signing_row_by_token = lambda token:self.link
        h.contract_signing_record_log = lambda *a,**k:None
        conn = sqlite3.connect(':memory:', check_same_thread=False)
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.executescript('''CREATE TABLE contracts(id INTEGER, contract_status TEXT, final_signed_file_path TEXT);
            CREATE TABLE contract_signatures(id INTEGER, link_id INTEGER, status TEXT, archive_status TEXT);''')
        conn.execute('INSERT INTO contracts VALUES(22,?,?)', ('FullyExecuted',str(self.path)))
        conn.execute("INSERT INTO contract_signatures VALUES(33,11,'signed','archived')")
        self.ns['db'] = SimpleNamespace(one=lambda sql,args:conn.execute(sql,args).fetchone())
        h.conn = conn
        h.contract_center_file_path = lambda contract,kind:Path(contract['final_signed_file_path'])
        h.contract_signing_login_required_page = lambda *a:('login-required', 200)
        h.school_plan_share_public_page = lambda title,body,status=200:('html', int(status), body)
        h.send_response = lambda status:h.response.update(status=int(status))
        h.send_header = lambda key,value:h.response['headers'].update({key:value})
        h.end_headers = lambda:None
        return h

    def grant(self, *, age=0, changes=None):
        # Independent fixture for the grant wire contract, not production issuance.
        payload = dict(purpose='contract-final-download-v1', link_id=11, contract_id=22,
                       signature_id=33, token_hash='synthetic-link-hash',
                       identity=hashlib.sha256(b'parent-a').hexdigest(),
                       artifact=hashlib.sha256(str(self.path.resolve()).encode()+b'\0'+self.path.read_bytes()).hexdigest())
        payload.update(changes or {})
        return self.cipher.encrypt_at_time(json.dumps(payload).encode(), int(time.time())-age).decode()

    def test_external_browser_without_cookie_downloads_only_authorized_final_pdf(self):
        h = self.handler(grant=self.grant())
        h.contract_signing_final_pdf('GET', 'test-token', 'download')
        self.assertEqual(h.response['headers'].get('Content-Type'), 'application/pdf')
        self.assertEqual(h.wfile.getvalue(), self.path.read_bytes())

    def test_original_authenticated_view_and_download_still_work(self):
        for action,disposition in [('view','inline'),('download','attachment')]:
            with self.subTest(action=action):
                h = self.handler(authenticated=True)
                h.contract_signing_final_pdf('GET','test-token',action)
                self.assertEqual(h.wfile.getvalue(),self.path.read_bytes())
                self.assertTrue(h.response['headers']['Content-Disposition'].startswith(disposition))

    def test_no_cookie_and_no_grant_still_require_original_login(self):
        for action in ['view','download']:
            self.assertEqual(self.handler().contract_signing_final_pdf('GET','test-token',action),('login-required',200))

    def assert_blocked(self, h, action='download'):
        result = h.contract_signing_final_pdf('GET','test-token',action)
        self.assertNotEqual(h.response['headers'].get('Content-Type'),'application/pdf')
        self.assertEqual(h.wfile.getvalue(),b'')
        return result

    def test_expired_grant_has_refresh_guidance_without_oauth_loop(self):
        result = self.assert_blocked(self.handler(grant=self.grant(age=301)))
        self.assertEqual(result[1],403)
        self.assertIn('微信',result[2])
        self.assertIn('刷新',result[2])

    def test_wrong_scope_tampering_future_and_untrusted_cipher_are_rejected(self):
        variants = [self.grant(changes={k:v}) for k,v in [
            ('purpose','parent-form'),('link_id',12),('contract_id',23),
            ('signature_id',34),('token_hash','other'),('identity','other'),('artifact','other')]]
        variants += ['invalid', self.grant()[:-4]+'abcd', self.grant(age=-3600),
                     Fernet(Fernet.generate_key()).encrypt(b'{}').decode()]
        for value in variants:
            with self.subTest(value=value[:15]):
                self.assertEqual(self.assert_blocked(self.handler(grant=value))[1],403)

    def test_download_grant_does_not_authorize_view_or_signing(self):
        self.assertEqual(self.assert_blocked(self.handler(grant=self.grant()),'view'),('login-required',200))

    def test_other_identity_cookie_cannot_be_overridden_by_grant(self):
        h=self.handler(grant=self.grant())
        h.headers['Cookie']='aaie_csl_11='+h.contract_signing_cookie_value(self.link,'parent-b')
        self.assertEqual(self.assert_blocked(h)[1],403)

    def test_revoked_link_and_inactive_contract_reject_even_valid_grant(self):
        grant=self.grant()
        for key,value in [('status','revoked'),('invalidated_at','2026-11-01'),
                          ('contract_status','Voided'),('contract_status','Deleted'),
                          ('contract_status','Superseded')]:
            with self.subTest(key=key,value=value):
                original=self.link[key];self.link[key]=value
                self.assertEqual(self.assert_blocked(self.handler(grant=grant))[1],410)
                self.link[key]=original

    def test_link_must_still_be_signed(self):
        grant=self.grant();self.link['status']='active';self.link['expires_at']='2099-01-01T00:00:00+00:00'
        self.assertEqual(self.assert_blocked(self.handler(grant=grant))[1],403)

    def test_replaced_file_or_new_signature_invalidates_old_grant(self):
        grant=self.grant();original=self.path.read_bytes()
        self.path.write_bytes(original+b'\n%replaced')
        self.assertEqual(self.assert_blocked(self.handler(grant=grant))[1],403)
        self.path.write_bytes(original);self.link['signed_signature_id']=34
        self.assertEqual(self.assert_blocked(self.handler(grant=grant))[1],403)

    def test_response_uses_same_file_snapshot_that_grant_verified(self):
        original=self.path.read_bytes();h=self.handler(grant=self.grant())
        verify=self.ns['verify_contract_download_grant']
        def replace_after_verifying(*args,**kwargs):
            valid=verify(*args,**kwargs)
            self.path.write_bytes(original+b'\n%new confidential version')
            return valid
        self.ns['verify_contract_download_grant']=replace_after_verifying
        h.contract_signing_final_pdf('GET','test-token','download')
        self.assertEqual(h.wfile.getvalue(),original)

    def test_final_file_failure_is_not_bypassed_by_valid_grant(self):
        h=self.handler(grant=self.grant())
        def unavailable(link): raise ValueError('not archived')
        h.contract_signing_final_pdf_path=unavailable
        self.assertEqual(self.assert_blocked(h)[1],404)

    def test_actual_archive_and_pdf_guards_remain_enforced(self):
        for sql in ["UPDATE contract_signatures SET archive_status='failed'",
                    "UPDATE contracts SET contract_status='ParentSigned'",
                    "DELETE FROM contract_signatures"]:
            with self.subTest(sql=sql):
                h=self.handler(grant=self.grant());h.conn.execute(sql)
                self.assertEqual(self.assert_blocked(h)[1],404)
        h=self.handler(grant=self.grant());self.path.write_bytes(b'not a PDF')
        with self.assertLogs('pypdf',level='WARNING'):
            self.assertEqual(self.assert_blocked(h)[1],404)

    def test_authenticated_legacy_download_ignores_stale_grant(self):
        h=self.handler(authenticated=True,grant=self.grant(age=301))
        h.contract_signing_final_pdf('GET','test-token','download')
        self.assertEqual(h.wfile.getvalue(),self.path.read_bytes())

    def test_non_get_cannot_use_grant(self):
        h=self.handler(grant=self.grant())
        self.assertEqual(h.contract_signing_final_pdf('POST','test-token','download')[1],405)

    def test_actions_keep_view_in_page_and_issue_working_scoped_download(self):
        from html.parser import HTMLParser
        class Links(HTMLParser):
            def __init__(self):super().__init__();self.links=[]
            def handle_starttag(self,tag,attrs):
                if tag=='a':self.links.append(dict(attrs))
        h=self.handler(authenticated=True)
        markup=h.contract_signing_final_actions('test-token',self.link)
        parser=Links();parser.feed(markup)
        self.assertEqual(parser.links[0]['href'],'#final-contract-preview')
        self.assertNotIn('target',parser.links[0])
        url=urlparse(parser.links[1]['href'])
        self.assertEqual(url.path,'/contract-sign/test-token/final/download')
        grant=parse_qs(url.query)['download_grant'][0]
        external=self.handler(grant=grant)
        external.contract_signing_final_pdf('GET','test-token','download')
        self.assertEqual(external.wfile.getvalue(),self.path.read_bytes())

    def test_actions_cannot_mint_download_authority_without_original_login(self):
        with self.assertRaises(ValueError):self.handler().contract_signing_final_actions('test-token',self.link)

    def failing_issuer(self,*args):
        raise OSError('synthetic key read failure')

    def configure_page_dependencies(self,h):
        self.ns.update(display_datetime=str, ensure_contract_uid=lambda c:'TEST-22',
                       emit_contract_business_event=lambda *a,**k:None,
                       append_service_log=lambda *a,**k:None)
        self.ns['db'].execute=lambda sql,args=():h.conn.execute(sql,args).lastrowid
        h.conn.executescript('CREATE TABLE contract_signing_links(id,first_viewed_at,last_viewed_at,viewed_count,updated_at); INSERT INTO contract_signing_links(id) VALUES(11);')
        h.conn.execute("ALTER TABLE contracts ADD COLUMN contract_title TEXT DEFAULT 'Example contract'")
        h.conn.execute("ALTER TABLE contract_signatures ADD COLUMN signed_at TEXT DEFAULT '2026-11-01'")
        self.link.update(bound_wechat_nickname='Example parent',bound_wechat_unionid='',
                         student_id='test',contract_student_id='test',expires_at='2099-01-01T00:00:00+00:00')
        h.contract_signing_row_by_id=lambda _:self.link
        h.contract_signing_pdf_preview_pages_data_uris=lambda *a:['data:image/png;base64,AA==']
        h.school_plan_share_masked_openid=lambda _: 'masked'
        h.send_html=lambda markup,**kwargs:markup
        self.ns['issue_contract_download_grant']=self.failing_issuer

    def test_signed_portal_survives_download_grant_issuance_failure(self):
        h=self.handler(authenticated=True);self.configure_page_dependencies(h)
        try:
            markup=h.contract_signing_portal('GET','test-token')
        except OSError as exc:
            self.fail('Download controls must not break the signed portal: '+str(exc))
        self.assertIn('id="final-contract-preview"',markup)
        self.assertIn('刷新',markup)
        self.assertNotIn('download_grant=',markup)
        self.assertEqual(h.conn.execute('SELECT archive_status FROM contract_signatures').fetchone()[0],'archived')

    def test_successful_signing_archive_is_not_failed_by_download_grant_issuance(self):
        h=self.handler(authenticated=True);self.configure_page_dependencies(h)
        h.conn.execute('DELETE FROM contract_signatures')
        for column in ['signed_at','updated_at','deleted_at','voided_at']:
            h.conn.execute('ALTER TABLE contracts ADD COLUMN '+column+' TEXT')
        for column in ['contract_id','student_id','signed_ip','user_agent','wechat_openid','wechat_unionid',
                       'wechat_nickname','signature_image_path','confirmation_statement','created_at','updated_at','archive_error']:
            h.conn.execute('ALTER TABLE contract_signatures ADD COLUMN '+column+' TEXT')
        # SQLite allocates signature IDs just as the real insert does.
        h.conn.execute('DROP TABLE contract_signatures')
        h.conn.execute('''CREATE TABLE contract_signatures(id INTEGER PRIMARY KEY, contract_id,student_id,link_id,signed_at,
            signed_ip,user_agent,wechat_openid,wechat_unionid,wechat_nickname,signature_image_path,
            confirmation_statement,status,archive_status,created_at,updated_at,archive_error)''')
        self.link.update(status='active',signed_signature_id=None)
        h.client_address=('127.0.0.1',10000)
        self.ns['parse_request_body']=lambda _:({'declaration':'yes','signature_data':'synthetic'},[])
        h.contract_signing_save_signature_image=lambda *a:self.path
        def archive(signature_id,user=None):
            h.conn.execute("UPDATE contract_signatures SET archive_status='archived'")
            h.conn.execute("UPDATE contracts SET contract_status='FullyExecuted'")
            return dict(final_document_path=str(self.path),final_document_id=55,signed_document_id=56)
        h.contract_signing_archive_documents=archive
        h.contract_signing_mark_link_signed=lambda link_id,stamp,signature_id:self.link.update(status='signed',signed_signature_id=signature_id)
        result=h.contract_signing_submit('POST','test-token')
        self.assertEqual(result[1],200)
        self.assertEqual(h.conn.execute('SELECT archive_status FROM contract_signatures').fetchone()[0],'archived')
        self.assertIn('id="final-contract-preview"',result[2])
        self.assertNotIn('download_grant=',result[2])

    def test_pdf_responses_disable_cache_and_referrer(self):
        h=self.handler(grant=self.grant());h.contract_signing_final_pdf('GET','test-token','download')
        self.assertEqual(h.response['headers']['Referrer-Policy'],'no-referrer')
        self.assertEqual(h.response['headers']['Cache-Control'],'no-store')

    def test_access_log_does_not_store_download_grant(self):
        self.ns['LOG_DIR']=Path(self.tmp.name)
        h=self.handler();h.address_string=lambda:'127.0.0.1'
        h.log_message('%s','GET /contract-sign/test-token/final/download?download_grant=SECRET&x=1 HTTP/1.1')
        value=(Path(self.tmp.name)/'access.log').read_text()
        self.assertNotIn('SECRET',value)
        self.assertIn('download_grant=[redacted]&x=1',value)

    def test_access_log_redacts_url_encoded_grant_parameter_names(self):
        self.ns['LOG_DIR']=Path(self.tmp.name)
        h=self.handler();h.address_string=lambda:'127.0.0.1'
        for key in ['%64ownload_grant','download%5fgrant','download_grant']:
            h.log_message('%s',f'GET /final/download?x=1&{key}=SECRET&download_grant=SECRET2 HTTP/1.1')
        self.assertNotIn('SECRET',(Path(self.tmp.name)/'access.log').read_text())

    def test_two_independent_http_clients_handoff_without_sharing_cookies(self):
        probe=self.handler()
        class HTTPFixture(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                probe.headers=self.headers
                probe.query=parse_qs(urlparse(self.path).query)
                probe.wfile=self.wfile
                probe.send_response=self.send_response
                probe.send_header=self.send_header
                probe.end_headers=self.end_headers
                if self.path=='/actions':
                    payload=probe.contract_signing_final_actions('test-token',probe.contract_signing_row_by_token('test-token')).encode()
                    self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(payload)
                else:
                    result=probe.contract_signing_final_pdf('GET','test-token','download')
                    if result:
                        self.send_response(result[1]);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(b'login-required')
        server=HTTPServer(('127.0.0.1',0),HTTPFixture)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            inside=http.client.HTTPConnection(*server.server_address,timeout=5)
            cookie='aaie_csl_11='+probe.contract_signing_cookie_value(self.link,'parent-a')
            inside.request('GET','/actions',headers={'Cookie':cookie})
            response=inside.getresponse();markup=response.read().decode();self.assertEqual(response.status,200);inside.close()
            from html.parser import HTMLParser
            class DownloadLink(HTMLParser):
                href=None
                def handle_starttag(self,tag,attrs):
                    attrs=dict(attrs)
                    if tag=='a' and '/final/download?' in attrs.get('href',''):self.href=attrs['href']
            parser=DownloadLink();parser.feed(markup);self.assertIsNotNone(parser.href)
            outside=http.client.HTTPConnection(*server.server_address,timeout=5)
            outside.request('GET',parser.href)
            response=outside.getresponse();self.assertEqual(response.status,200)
            self.assertEqual(response.getheader('Content-Type'),'application/pdf')
            self.assertEqual(response.read(),self.path.read_bytes());outside.close()
            unauth=http.client.HTTPConnection(*server.server_address,timeout=5)
            unauth.request('GET','/contract-sign/test-token/final/download')
            response=unauth.getresponse();self.assertEqual(response.getheader('Content-Type'),'text/html');response.read();unauth.close()
        finally:
            server.shutdown();worker.join(timeout=5);server.server_close()


if __name__ == '__main__':
    unittest.main()
