from pathlib import Path
import tempfile
import unittest
from business_changes.files import persist_verified_file


class VerifiedFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'material.pdf'

    def test_source_is_complete_and_readable_before_database_save(self):
        def save(path,digest):
            self.assertEqual(b'synthetic',path.read_bytes());self.assertEqual(64,len(digest));return 42
        self.assertEqual(42,persist_verified_file(self.path,b'synthetic',save))
        self.assertEqual(b'synthetic',self.path.read_bytes())

    def test_database_failure_removes_only_new_owned_file(self):
        def broken(*args):raise RuntimeError('synthetic database failure')
        with self.assertRaises(RuntimeError):persist_verified_file(self.path,b'synthetic',broken)
        self.assertFalse(self.path.exists())

    def test_existing_file_is_never_overwritten_or_removed(self):
        self.path.write_bytes(b'existing')
        with self.assertRaises(FileExistsError):persist_verified_file(self.path,b'new',lambda *a:None)
        self.assertEqual(b'existing',self.path.read_bytes())

    def test_failed_cleanup_reports_actual_orphan_path(self):
        from unittest.mock import patch
        def broken(*args):raise RuntimeError('synthetic database failure')
        with patch.object(Path,'unlink',side_effect=OSError('synthetic locked file')):
            with self.assertRaisesRegex(RuntimeError,'file_cleanup_failed') as error:persist_verified_file(self.path,b'synthetic',broken)
        self.assertIn(str(self.path),str(error.exception));self.assertTrue(self.path.exists())
