import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime_configuration import RuntimeConfigurationError, canonical


class SidecarPathTests(unittest.TestCase):
    def test_file_can_disappear_during_validation(self):
        path = Path(__file__).resolve().parent / "isolated.db-shm"
        stat = Path.stat

        def disappearing_stat(candidate, *args, **kwargs):
            if candidate == path:
                raise FileNotFoundError(path)
            return stat(candidate, *args, **kwargs)

        with patch.object(Path, "stat", disappearing_stat), patch.object(Path, "exists", return_value=True), patch.object(Path, "is_file", return_value=True):
            self.assertEqual(canonical(path), path)

    def test_symlinks_and_hardlinks_are_still_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "source"
            source.touch()
            symlink = root / "symlink"
            symlink.symlink_to(source)
            hardlink = root / "hardlink"
            os.link(source, hardlink)
            with self.assertRaisesRegex(RuntimeConfigurationError, "symlink_path_rejected"):
                canonical(symlink)
            with self.assertRaisesRegex(RuntimeConfigurationError, "hardlink_path_rejected"):
                canonical(hardlink)
