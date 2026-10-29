import importlib
from pathlib import Path
import tempfile
import unittest
import fcntl
import os

ROOT=Path(__file__).resolve().parents[1]


class WriterLeaseTests(unittest.TestCase):
    def setUp(self):
        base=ROOT/'SAFEBOX/controller-safety-20260909/writer-lease-tests';base.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base);self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'writer.lock';self.path.write_bytes(b'synthetic-existing-metadata')

    def lease(self):
        api=importlib.import_module('tools.production_writer_lease')
        return api.ExistingWriterLease(self.path)

    def test_exclusive_lease_preserves_file_bytes(self):
        before=self.path.read_bytes()
        with self.lease() as lease:
            lease.assert_held()
            with self.path.open('r') as second:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(second.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.assertEqual(self.path.read_bytes(),before)

    def test_active_writer_blocks_without_truncation(self):
        with self.path.open('r') as first:
            fcntl.flock(first.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                with self.lease():pass
        self.assertEqual(self.path.read_bytes(),b'synthetic-existing-metadata')

    def test_missing_lock_is_not_created(self):
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            with self.lease():pass
        self.assertFalse(self.path.exists())

    def test_replaced_inode_invalidates_lease(self):
        with self.lease() as lease:
            other=self.path.with_name('replacement');other.write_bytes(b'replacement')
            os.replace(other,self.path)
            with self.assertRaisesRegex(RuntimeError,'lock_identity_changed'):lease.assert_held()


if __name__=='__main__':unittest.main()
