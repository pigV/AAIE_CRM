"""Actual filesystem security checks; never mock chmod or accept0700 files."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

from revenue.secure_storage import StorageUnavailable, configured_root, read_reference, read_secure, write_immutable
from revenue.uploads import stage_revenue_upload

class SecureStorageConfigurationTests(unittest.TestCase):
    def test_missing_configuration_has_no_legacy_fallback(self):
        with self.assertRaises(StorageUnavailable): configured_root({})
    def test_relative_and_parent_paths_rejected(self):
        for path in ('relative', '/Users/../tmp/security'):
            with self.subTest(path=path),self.assertRaises(StorageUnavailable): configured_root({'REVENUE_SECURE_STORAGE_ROOT':path})
    def test_current_exfat_refuses_before_sensitive_payload(self):
        root=Path(__file__).resolve().parents[1]/'SAFEBOX/revenue-posix-storage-20260907/exfat-negative'
        root.mkdir(parents=True,exist_ok=True)
        with self.assertRaises(StorageUnavailable):write_immutable(root,'negative.pdf',b'%PDF-SYNTHETIC-NEVER-WRITE')
        self.assertFalse((root/'negative.pdf').exists())
        self.assertFalse(any(x.is_file() and x.stat().st_size for x in root.iterdir()))

@unittest.skipUnless(os.environ.get('REVENUE_POSIX_TEST_ROOT'),'actual approved POSIX test root required; no mocked filesystem')
class ActualPosixStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='secure-test-',dir=os.environ['REVENUE_POSIX_TEST_ROOT'])
        self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.payload=b'%PDF-SYNTHETIC'
    def test_configured_private_root_under_group_writable_data_mount(self):
        mount = Path('/Volumes/Data')
        if not self.root.is_relative_to(mount) or not mount.stat().st_mode & 0o022:
            self.skipTest('requires a fixture on the group-writable Data mount')
        root = self.root / 'revenue_documents'
        root.mkdir(mode=0o700)
        other = self.root / 'unapproved'
        other.mkdir(mode=0o700)
        configuration = {'REVENUE_SECURE_STORAGE_ROOT': str(root)}
        payload = b'\xff\xd8\xff\xe0synthetic proof\xff\xd9'
        upload = SimpleNamespace(filename='proof.jpg', content_type='image/jpeg', data=payload)
        with patch('revenue.secure_storage._PRODUCTION_REVENUE_ROOT', root):
            self.assertEqual(root, configured_root(configuration))
            staged = stage_revenue_upload(root / 'client-payments', upload, {'.jpg'})
            self.assertEqual(0o600, staged.path.stat().st_mode & 0o777)
            self.assertEqual(payload, read_reference(
                self.root / 'legacy', staged.path, staged.sha256, staged.size_bytes,
                secure_root=root,
            ))
            with self.assertRaises(StorageUnavailable):
                configured_root({'REVENUE_SECURE_STORAGE_ROOT': str(other)})
            root.chmod(0o750)
            with self.assertRaises(StorageUnavailable):
                write_immutable(root / 'client-payments', 'blocked.jpg', payload)
            with self.assertRaises(StorageUnavailable):
                read_reference(self.root / 'legacy', staged.path, staged.sha256,
                               staged.size_bytes, secure_root=root)
        missing = self.root / 'missing'
        with patch('revenue.secure_storage._PRODUCTION_REVENUE_ROOT', missing):
            with self.assertRaises(StorageUnavailable):
                write_immutable(missing / 'client-payments', 'blocked.jpg', payload)
        self.assertFalse(missing.exists())
    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin extended ACL integration')
    def test_actual_allow_acl_rejected_and_deny_only_accepted(self):
        path,_=write_immutable(self.root,'acl.pdf',self.payload)
        subprocess.run(['/bin/chmod','+a','everyone allow read',str(path)],check=True)
        self.assertEqual(0o600,path.stat().st_mode&0o777)
        with self.assertRaises(StorageUnavailable):
            read_secure(self.root,path,hashlib.sha256(self.payload).hexdigest(),len(self.payload))
        subprocess.run(['/bin/chmod','-N',str(path)],check=True)
        subprocess.run(['/bin/chmod','+a','everyone deny delete',str(path)],check=True)
        self.assertEqual(self.payload,read_secure(self.root,path,hashlib.sha256(self.payload).hexdigest(),len(self.payload)))
        subprocess.run(['/bin/chmod','-N',str(path)],check=True)
    def test_intermediate_actual_permissions_before_every_payload_write(self):
        actual_write=os.write
        observed=[]
        def observe(fd,data):
            s=os.fstat(fd)
            self.assertEqual(0o600,s.st_mode&0o777)
            self.assertEqual((os.geteuid(),os.getegid()),(s.st_uid,s.st_gid))
            self.assertEqual(1,s.st_nlink)
            observed.append(s.st_size)
            return actual_write(fd,data)
        # Instrument real writes; no permission operation or result is mocked.
        with patch('revenue.secure_storage.os.write',side_effect=observe):
            write_immutable(self.root,'intermediate.pdf',self.payload)
        self.assertTrue(observed)
        self.assertEqual(0,observed[0])
        self.assertFalse(list(self.root.glob('.pending-*')))
    def test_exact_mode_hash_and_reuse(self):
        path,created=write_immutable(self.root,'evidence.pdf',self.payload)
        self.assertTrue(created);self.assertEqual(0o600,path.stat().st_mode&0o777)
        self.assertEqual((os.geteuid(),os.getegid()),(path.stat().st_uid,path.stat().st_gid))
        self.assertEqual((path,False),write_immutable(self.root,'evidence.pdf',self.payload))
        self.assertEqual(self.payload,read_secure(self.root,path,hashlib.sha256(self.payload).hexdigest(),len(self.payload)))
        self.assertEqual(['evidence.pdf'],[p.name for p in self.root.iterdir()])
    def test_no_overwrite_or_permission_repair(self):
        path,_=write_immutable(self.root,'evidence.pdf',self.payload)
        with self.assertRaises(StorageUnavailable):write_immutable(self.root,path.name,b'changed')
        path.chmod(0o640)
        with self.assertRaises(StorageUnavailable):write_immutable(self.root,path.name,self.payload)
        self.assertEqual(0o640,path.stat().st_mode&0o777);path.chmod(0o600)
    def test_symlink_root_ancestor_and_leaf_denied(self):
        real=self.root/'real';real.mkdir(mode=0o700);alias=self.root/'alias';alias.symlink_to(real,target_is_directory=True)
        for root in (alias,alias/'child'):
            with self.assertRaises(StorageUnavailable):write_immutable(root,'evidence.pdf',self.payload)
        target,_=write_immutable(real,'actual.pdf',self.payload);(real/'link.pdf').symlink_to(target)
        with self.assertRaises(StorageUnavailable):write_immutable(real,'link.pdf',self.payload)
    def test_traversal_and_outside_read_denied(self):
        for name in ('../outside.pdf','/outside.pdf','a/b.pdf'):
            with self.assertRaises(StorageUnavailable):write_immutable(self.root,name,self.payload)
        with self.assertRaises(StorageUnavailable):read_secure(self.root,self.root.parent/'outside.pdf','a'*64,1)
    def test_hardlinked_object_denied(self):
        path,_=write_immutable(self.root,'evidence.pdf',self.payload);os.link(path,self.root/'hardlink.pdf')
        with self.assertRaises(StorageUnavailable):read_secure(self.root,path,hashlib.sha256(self.payload).hexdigest(),len(self.payload))
    def test_unsafe_directory_denied_without_chmod(self):
        self.root.chmod(0o750)
        with self.assertRaises(StorageUnavailable):write_immutable(self.root,'evidence.pdf',self.payload)
        self.assertEqual(0o750,self.root.stat().st_mode&0o777);self.root.chmod(0o700)
