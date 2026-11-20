"""Native parent router regression; run only through the isolated full-schema runner.

No external OAuth calls: runtime mode/configuration and OAuth transport are patched.
Native routing, state validation and identity persistence use the isolated schema.
"""
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import app
from tests import test_business_change_school_chain as school_chain


class PreproductionParentIdentityGateTests(unittest.TestCase):
    def setUp(self):
        self.f = school_chain.SchoolMaterialChainTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.h = self.f.h
        self.db = self.f.db
        self.f.output = {}
        self.f.cid = self.db.one('SELECT id FROM contracts ORDER BY id LIMIT 1')[0]
        self.f.token = 'isolated-contract-identity-only'
        self.db.execute("UPDATE contracts SET contract_status='Sent',final_signed_file_path='' WHERE id=?", (self.f.cid,))
        self.f.lid = self.db.execute("INSERT INTO contract_signing_links(contract_id,student_id,token_hash,token_last_4,signing_url,status,expires_at,created_at) VALUES(?,?,?,'only','/synthetic','active','2099-01-01T00:00:00+11:00','2026-11-20')",(self.f.cid,self.f.sid,app.contract_signing_token_hash(self.f.token)))
        self.parent_token = 'isolated-parent-identity-only'
        self.db.execute("INSERT INTO parent_form_tokens(token,student_hint,expires_at,created_at,created_by,intake_type) VALUES(?,?,?,?,?,?)", (self.parent_token,'Synthetic only','2099-01-01T00:00:00+11:00','2026-11-20T00:00:00+11:00',self.f.actor['id'],'ordinary_new_application'))
        from cryptography.fernet import Fernet
        cipher = Fernet(Fernet.generate_key())
        cipher_patch = patch.object(app, 'fernet', return_value=cipher)
        cipher_patch.start()
        self.addCleanup(cipher_patch.stop)
        self.h.send_html = lambda body,*args,**kwargs: self.f.output.update(html=body,status=kwargs.get('status',args[0] if args else 200))
        secret = self.f.root / 'synthetic-cookie-secret'
        secret.write_bytes(b'isolated-test-secret-only')
        self.secret_patch = patch.object(app, 'SECRET_PATH', secret)
        self.secret_patch.start()
        self.addCleanup(self.secret_patch.stop)
        self.h.server = SimpleNamespace(service_mode='parent', server_port=8891)
        self.h.current_user = lambda: (None, '')
        self.h.redirect = lambda location, cookie=None: self.f.output.update(status=302,location=location,cookie=cookie)
        self.h.parent_form_wechat_oauth_start = lambda *args: self.f.output.update(status=302,location='official-oauth-start')
        self.h.contract_signing_oauth_start = lambda *args: self.f.output.update(status=302,location='official-oauth-start')

    def post_identity(self, path, *, mode='production', oauth=True):
        body = urlencode({'wechat_nickname':'Synthetic developer identity','wechat_openid':'dev_untrusted_identity'}).encode()
        self.h.path = path
        self.h.rfile = io.BytesIO(body)
        self.h.headers = {'Content-Type':'application/x-www-form-urlencoded','Content-Length':str(len(body)),'User-Agent':'identity-gate-isolated-regression'}
        with patch.object(app,'STAGING',SimpleNamespace(mode=mode)), patch.object(self.h,'school_plan_wechat_oauth_enabled',return_value=oauth):
            self.h.dispatch('POST')

    def test_production_contract_login_rejects_submitted_openid_even_when_oauth_configured(self):
        self.post_identity('/contract-sign/' + self.f.token + '/login')
        row = self.db.one('SELECT bound_wechat_openid FROM contract_signing_links WHERE id=?',(self.f.lid,))
        self.assertFalse(row[0], 'Production parent router accepted an unverified developer OpenID')
        self.assertFalse(self.f.output.get('cookie'), 'Rejected developer identity must not receive an auth cookie')

    def test_production_parent_form_login_rejects_submitted_openid_even_when_oauth_configured(self):
        self.post_identity('/parent-form/' + self.parent_token + '/wechat-login')
        row = self.db.one('SELECT bound_wechat_openid FROM parent_form_access_bindings WHERE token=?',(self.parent_token,))
        self.assertIsNone(row, 'Production parent router bound an unverified developer OpenID')
        self.assertFalse(self.f.output.get('cookie'), 'Rejected developer identity must not receive an auth cookie')

    def test_isolated_staging_contract_keeps_identifiable_manual_acceptance(self):
        self.post_identity('/contract-sign/' + self.f.token + '/login',mode='staging',oauth=False)
        self.assertEqual('dev_untrusted_identity',self.db.one('SELECT bound_wechat_openid FROM contract_signing_links WHERE id=?',(self.f.lid,))[0])
        self.assertTrue(self.f.output.get('cookie'))

    def test_isolated_staging_parent_form_keeps_identifiable_manual_acceptance(self):
        self.post_identity('/parent-form/' + self.parent_token + '/wechat-login',mode='staging',oauth=False)
        self.assertEqual('dev_untrusted_identity',self.db.one('SELECT bound_wechat_openid FROM parent_form_access_bindings WHERE token=?',(self.parent_token,))[0])
        self.assertTrue(self.f.output.get('cookie'))

    def assert_manual_rejected(self, path, *, mode, oauth):
        self.post_identity(path, mode=mode, oauth=oauth)
        self.assertEqual(403, self.f.output.get('status'))
        self.assertEqual(0, self.h.rfile.tell(), 'Rejected manual identity must not parse the submitted body')
        self.assertFalse(self.f.output.get('cookie'))
        self.assertFalse(self.db.one('SELECT bound_wechat_openid FROM contract_signing_links WHERE id=?',(self.f.lid,))[0])
        self.assertIsNone(self.db.one('SELECT * FROM parent_form_access_bindings WHERE token=?',(self.parent_token,)))

    def test_staging_oauth_contract_rejects_manual_identity_before_parsing(self):
        self.assert_manual_rejected('/contract-sign/'+self.f.token+'/login', mode='staging', oauth=True)

    def test_staging_oauth_parent_form_rejects_manual_identity_before_parsing(self):
        self.assert_manual_rejected('/parent-form/'+self.parent_token+'/wechat-login', mode='staging', oauth=True)

    def test_production_contract_without_oauth_rejects_manual_identity(self):
        self.assert_manual_rejected('/contract-sign/'+self.f.token+'/login', mode='production', oauth=False)

    def test_production_parent_form_without_oauth_rejects_manual_identity(self):
        self.assert_manual_rejected('/parent-form/'+self.parent_token+'/wechat-login', mode='production', oauth=False)

    def get_login(self, path, oauth):
        self.h.path = path
        self.h.headers = {}
        with patch.object(app,'STAGING',SimpleNamespace(mode='production')), patch.object(self.h,'school_plan_wechat_oauth_enabled',return_value=oauth):
            self.h.dispatch('GET')

    def test_production_contract_without_oauth_has_clear_unavailable_page(self):
        self.get_login('/contract-sign/'+self.f.token+'/login',False)
        self.assertEqual(503,self.f.output.get('status'))
        self.assertIn('微信验证暂不可用',self.f.output.get('html',''))
        self.assertNotIn('name="wechat_openid"',self.f.output.get('html',''))

    def test_production_parent_form_without_oauth_has_clear_unavailable_page(self):
        self.get_login('/parent-form/'+self.parent_token+'/wechat-login',False)
        self.assertEqual(503,self.f.output.get('status'))
        self.assertIn('微信验证暂不可用',self.f.output.get('html',''))
        self.assertNotIn('name="wechat_openid"',self.f.output.get('html',''))

    def test_production_contract_get_still_starts_official_oauth(self):
        self.get_login('/contract-sign/'+self.f.token+'/login',True)
        self.assertEqual('official-oauth-start',self.f.output.get('location'))

    def test_production_parent_form_get_still_starts_official_oauth(self):
        self.get_login('/parent-form/'+self.parent_token+'/wechat-login',True)
        self.assertEqual('official-oauth-start',self.f.output.get('location'))

    def callback(self,purpose,token):
        state='isolated-callback-state'
        payload={'state':state,'purpose':purpose,'token':token,'link_id':self.f.lid,'created_at':app.now_iso(),'expires_at':'2099-01-01T00:00:00+11:00'}
        self.db.set_setting(app.wechat_oauth_state_setting_key(state),json.dumps(payload),None)
        self.h.path='/api/wechat/oauth/callback?code=synthetic-transport-code&state='+state
        self.h.headers={}
        with patch.object(app,'STAGING',SimpleNamespace(mode='production')), patch.object(self.h,'school_plan_wechat_oauth_enabled',return_value=True), patch.object(app,'wechat_exchange_oauth_code',return_value={'openid':'synthetic-callback-identity'}) as exchange, patch.object(app,'wechat_oauth_scope',return_value='snsapi_base'):
            self.h.dispatch('GET')
            exchange.assert_called_once_with('synthetic-transport-code')
        self.assertTrue(self.f.output.get('cookie'))
        self.assertEqual(302,self.f.output.get('status'))

    def test_contract_callback_keeps_native_state_and_identity_binding(self):
        self.callback('contract_signing',self.f.token)
        self.assertEqual('synthetic-callback-identity',self.db.one('SELECT bound_wechat_openid FROM contract_signing_links WHERE id=?',(self.f.lid,))[0])

    def test_parent_form_callback_keeps_native_state_and_identity_binding(self):
        self.callback('parent_form',self.parent_token)
        self.assertEqual('synthetic-callback-identity',self.db.one('SELECT bound_wechat_openid FROM parent_form_access_bindings WHERE token=?',(self.parent_token,))[0])
