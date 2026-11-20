import os
import tempfile
import unittest
from pathlib import Path


class StagingStorageBoundaryTests(unittest.TestCase):
    def test_secure_directory_walk_works_without_reading_production_or_other_checkout(self):
        from staging_environment import ACTIVE
        from revenue.secure_storage import _open_directory
        self.assertIsNotNone(ACTIVE)
        with tempfile.TemporaryDirectory() as directory:
            with _open_directory(Path(directory), writable_mount=Path('/Volumes/Data')) as (path, descriptor):
                self.assertEqual(Path(directory),path)
                self.assertTrue(os.fstat(descriptor).st_ino)
        for path in (
            '/Volumes/Data/AAIE_CRM/runtime/production/config/runtime.json',
            '/Volumes/Data/AAIE_CRM/main/app.py',
            '/Volumes/Data/AAIE_CRM/worktrees/home-stage-colors-20261120/app.py',
        ):
            with self.subTest(path=path):
                with self.assertRaises(PermissionError):
                    descriptor = os.open(path, os.O_RDONLY)
                    os.close(descriptor)
