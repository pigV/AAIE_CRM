"""Only the opt-in validated staging document root may use the Data mount policy."""
import os,unittest,secrets
from pathlib import Path
from revenue.secure_storage import write_immutable,read_secure,StorageUnavailable
class HistoricalStagingStorageTests(unittest.TestCase):
 def test_actual_staging_root_roundtrips_without_production_exception(self):
  from staging_environment import ACTIVE
  self.assertIsNotNone(ACTIVE);root=ACTIVE.root/'revenue_documents';payload=b'isolated staging storage test';name=secrets.token_hex(8)+'.pdf'
  try:p,created=write_immutable(root,name,payload)
  except StorageUnavailable:self.fail('validated task document root is blocked by mount traversal')
  import hashlib
  self.assertTrue(created);self.assertEqual(payload,read_secure(root,p,hashlib.sha256(payload).hexdigest(),len(payload)))
 def test_unrelated_task_tmp_root_remains_denied(self):
  from staging_environment import ACTIVE
  with self.assertRaises(StorageUnavailable):write_immutable(ACTIVE.root/'tmp/untrusted-documents','sample.pdf',b'data')

 def test_no_staging_environment_does_not_enable_exception(self):
  from staging_environment import ACTIVE
  from unittest.mock import patch
  with patch.dict(os.environ,{'AAIE_CRM_ENV':'production'}):
   with self.assertRaises(StorageUnavailable):write_immutable(ACTIVE.root/'revenue_documents','denied.pdf',b'data')
 def test_wrong_configured_root_does_not_enable_exception(self):
  from staging_environment import ACTIVE
  from unittest.mock import patch
  for value in ['',str(ACTIVE.root/'revenue_documents')+'/',str(ACTIVE.root/'other')]:
   with self.subTest(value=value),patch.dict(os.environ,{'REVENUE_SECURE_STORAGE_ROOT':value}):
    with self.assertRaises(StorageUnavailable):write_immutable(ACTIVE.root/'revenue_documents','denied.pdf',b'data')
 def test_similarly_named_sibling_is_denied(self):
  from staging_environment import ACTIVE
  with self.assertRaises(StorageUnavailable):write_immutable(ACTIVE.root/'revenue_documents-other','denied.pdf',b'data')
 def test_failed_staging_validation_has_storage_error_contract(self):
  from staging_environment import Staging,IsolationError,ACTIVE
  from unittest.mock import patch
  with patch.object(Staging,'check',side_effect=IsolationError('test rejection')):
   with self.assertRaises(StorageUnavailable):write_immutable(ACTIVE.root/'revenue_documents','denied.pdf',b'data')
