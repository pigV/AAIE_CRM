import hashlib,os,stat,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from revenue.secure_storage import read_reference,write_immutable,StorageUnavailable
class ReaderCompatibilityTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(dir='/Users/faqiang/CRM-Revenue-Secure-Test',prefix='reader-');self.addCleanup(self.temp.cleanup)
  self.base=Path(self.temp.name);self.legacy=self.base/'legacy';self.secure=self.base/'secure'
  self.legacy.mkdir(mode=0o700);self.secure.mkdir(mode=0o700);self.data=b'synthetic reader evidence';self.sha=hashlib.sha256(self.data).hexdigest()
 def put(self,root,name='a.pdf'):
  return write_immutable(root,name,self.data)[0]
 def read(self,p,secure=True):
  return read_reference(self.legacy,p,self.sha,len(self.data),secure_root=self.secure if secure else None)
 def test_legacy_reference_without_secure_configuration(self):
  p=self.put(self.legacy);os.chmod(p,0o700);self.assertEqual(self.data,self.read(p,False))
 def test_secure_reference_is_read_with_exact_600(self):
  p=self.put(self.secure);self.assertEqual(self.data,self.read(p));self.assertEqual(0o600,stat.S_IMODE(p.stat().st_mode))
 def test_secure_without_configuration_fails(self):
  p=self.put(self.secure)
  with self.assertRaises(StorageUnavailable):self.read(p,False)
 def test_no_fallback_same_basename(self):
  self.put(self.legacy)
  with self.assertRaises(StorageUnavailable):self.read(self.secure/'a.pdf')
  self.put(self.secure,'b.pdf')
  with self.assertRaises(StorageUnavailable):self.read(self.legacy/'b.pdf')
 def test_corrupt_secure_does_not_try_legacy(self):
  self.put(self.legacy);p=self.put(self.secure);p.write_bytes(b'wrong')
  with self.assertRaises(StorageUnavailable):self.read(p)
 def test_secure_700_rejected_not_reclassified_legacy(self):
  p=self.put(self.secure);os.chmod(p,0o700)
  with self.assertRaises(StorageUnavailable):self.read(p)
 def test_overlap_roots_rejected(self):
  p=self.put(self.legacy)
  for secure in [self.legacy,self.legacy/'nested',self.base]:
   with self.subTest(secure=secure),self.assertRaises(StorageUnavailable):read_reference(self.legacy,p,self.sha,len(self.data),secure_root=secure)
 def test_traversal_relative_and_outside_rejected(self):
  p=self.put(self.secure)
  for q in [str(self.legacy)+'/../secure/a.pdf','a.pdf',str(self.base/'a.pdf')]:
   with self.subTest(path=q),self.assertRaises(StorageUnavailable):self.read(q)
 def test_symlink_file_and_directory_rejected(self):
  p=self.put(self.secure);(self.legacy/'link.pdf').symlink_to(p);(self.legacy/'linkdir').symlink_to(self.secure)
  for q in [self.legacy/'link.pdf',self.legacy/'linkdir/a.pdf']:
   with self.subTest(path=q),self.assertRaises(StorageUnavailable):self.read(q)
 def test_hash_and_size_fail_closed(self):
  p=self.put(self.legacy)
  with self.assertRaises(StorageUnavailable):read_reference(self.legacy,p,'0'*64,len(self.data),secure_root=self.secure)
  with self.assertRaises(StorageUnavailable):read_reference(self.legacy,p,self.sha,1,secure_root=self.secure)

 def test_write_configuration_cannot_select_legacy_or_overlap(self):
  from revenue.secure_storage import configured_write_root
  for root in [self.legacy,self.legacy/'nested',self.base]:
   with self.subTest(root=root),self.assertRaises(StorageUnavailable):configured_write_root(self.legacy,{'REVENUE_SECURE_STORAGE_ROOT':str(root)})
  self.assertEqual(self.secure,configured_write_root(self.legacy,{'REVENUE_SECURE_STORAGE_ROOT':str(self.secure)}))
