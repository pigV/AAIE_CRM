import hashlib,io,os,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import app
from tests.revenue_secure_test_support import secure_directory,write_test_document
from revenue.secure_storage import StorageUnavailable
from revenue.ui_actions import RevenueUIActions
class ReaderHTTPTests(unittest.TestCase):
 def setUp(self):
  self.root=secure_directory(self);self.legacy=self.root/'legacy';self.secure=self.root/'secure';self.legacy.mkdir(mode=0o700);self.secure.mkdir(mode=0o700)
  self.data=b'%PDF-1.4\nSynthetic';self.sha=hashlib.sha256(self.data).hexdigest()
 def handler(self,role,module_permissions_json='["revenue_commission"]'):
  class Handler:
   user={'role_name':role,'module_permissions_json':module_permissions_json} if role else None
   def __init__(self):self.status=None;self.headers={};self.wfile=io.BytesIO()
   def error_page(self,message,status):self.status=int(status)
   def send_response(self,status):self.status=int(status)
   def send_header(self,k,v):self.headers[k]=v
   def end_headers(self):pass
  return Handler()
 def run_read(self,p,role='Admin',secure=True):
  row={'storage_path':str(p),'sha256':self.sha,'size_bytes':len(self.data),'mime_type':'application/pdf','document_kind':'test'}
  def schema_rows(sql, parameters=()):
   if 'sqlite_master' in sql:return [{'name':'schema_migrations','type':'table'}]
   if parameters==('20260901_034',):return [{'version':'20260901_034'}]
   return []
  db=SimpleNamespace(path=self.legacy.parent/'test.db',one=lambda *a:row,all=schema_rows)
  # Derive exactly the documented DB-local legacy root.
  self.legacy.rename(self.legacy.parent/'revenue_documents') if self.legacy.name=='legacy' else None
  old=self.legacy;self.legacy=old.parent/'revenue_documents'
  if p.is_relative_to(old):row['storage_path']=str(self.legacy/p.relative_to(old))
  h=self.handler(role)
  with patch.object(app,'db',db),patch.dict(os.environ,{'REVENUE_SECURE_STORAGE_ROOT':str(self.secure) if secure else ''}):app.CRMHandler.revenue_document(h,'GET','test','view')
  return h
 def test_admin_legacy_without_config(self):
  p=write_test_document(self.legacy/'a.pdf',self.data);h=self.run_read(p,secure=False);self.assertEqual(200,h.status);self.assertEqual(self.data,h.wfile.getvalue())
 def test_admin_secure(self):
  p=write_test_document(self.secure/'a.pdf',self.data);h=self.run_read(p);self.assertEqual(200,h.status);self.assertEqual('private, no-store',h.headers['Cache-Control'])
 def test_revenue_off_and_anonymous_direct_endpoint_denied_before_db(self):
  for role in ['Staff','Finance','Manager',None]:
   h=self.handler(role,'[]')
   with patch.object(app,'db',SimpleNamespace(one=lambda *a:(_ for _ in ()).throw(AssertionError('DB touched')))):
    app.CRMHandler.revenue_document(h,'GET','known-id','view')
   self.assertEqual(403,h.status);self.assertEqual(b'',h.wfile.getvalue())
 def test_revenue_on_staff_can_use_direct_endpoint(self):
  p=write_test_document(self.secure/'staff.pdf',self.data)
  h=self.run_read(p,role='Staff')
  self.assertEqual(200,h.status)
  self.assertEqual(self.data,h.wfile.getvalue())
 def test_missing_secure_config_is_safe_404(self):
  p=write_test_document(self.secure/'a.pdf',self.data);self.assertEqual(404,self.run_read(p,secure=False).status)
 def test_write_authority_has_no_legacy_default(self):
  action=object.__new__(RevenueUIActions);action._document_root=None
  with patch.dict(os.environ,{'REVENUE_SECURE_STORAGE_ROOT':''}):
   with self.assertRaises(StorageUnavailable):_ = action.document_root
  with patch.dict(os.environ,{'REVENUE_SECURE_STORAGE_ROOT':str(self.secure)}):self.assertEqual(self.secure,action.document_root)
 def test_write_configuration_missing_503(self):
  class H:
   user={'role_name':'Admin'};headers={}
   def request_view_mode(self):return 'desktop'
   def _execute_revenue_action(self,action):raise StorageUnavailable()
   def error_page(self,msg,status):self.status=int(status)
  h=H();app.CRMHandler.revenue_action(h,'POST','contracts/upload');self.assertEqual(503,h.status)
