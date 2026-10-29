import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nas_access_capability import (
    NasAccessError,
    inspect_nas_access,
    require_readable_file,
    require_writable_directory,
)


class NasAccessCapabilityTests(unittest.TestCase):
    def test_missing_mount_is_not_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            missing_mount = Path(folder) / "missing"
            result = inspect_nas_access(missing_mount, missing_mount / "CRM_TEST")

        self.assertFalse(result.ready)
        self.assertFalse(result.mounted)
        self.assertEqual(result.error_code, "mount_unavailable")

    def test_missing_active_root_is_not_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            mount = Path(folder)
            result = inspect_nas_access(mount, mount / "missing")

        self.assertFalse(result.ready)
        self.assertTrue(result.mounted)
        self.assertFalse(result.root_exists)
        self.assertEqual(result.error_code, "root_unavailable")

    def test_lightweight_read_denial_is_not_ready(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch(
            "nas_access_capability.os.scandir",
            side_effect=PermissionError(1, "denied"),
        ):
            root = Path(folder)
            result = inspect_nas_access(root, root)

        self.assertFalse(result.ready)
        self.assertFalse(result.readable)
        self.assertEqual(result.error_code, "read_denied")

    def test_lightweight_write_denial_is_not_ready(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch(
            "nas_access_capability.os.access",
            return_value=False,
        ):
            root = Path(folder)
            result = inspect_nas_access(root, root)

        self.assertFalse(result.ready)
        self.assertTrue(result.readable)
        self.assertFalse(result.writable)
        self.assertEqual(result.error_code, "write_denied")

    def test_write_probe_is_exclusive_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            result = require_writable_directory(root)

            self.assertEqual(result, root.resolve())
            self.assertEqual(list(root.iterdir()), [])

    def test_write_probe_does_not_overwrite_an_existing_name(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            existing = root / ".fixed_probe"
            existing.write_text("must remain", encoding="utf-8")
            with mock.patch(
                "nas_access_capability._probe_path",
                return_value=existing,
            ):
                with self.assertRaises(NasAccessError) as caught:
                    require_writable_directory(root)

            self.assertEqual(caught.exception.code, "write_denied")
            self.assertEqual(existing.read_text(encoding="utf-8"), "must remain")

    def test_write_probe_create_failure_uses_stable_code(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch(
            "nas_access_capability.os.open",
            side_effect=PermissionError(1, "denied"),
        ):
            with self.assertRaises(NasAccessError) as caught:
                require_writable_directory(Path(folder))

        self.assertEqual(caught.exception.code, "write_denied")

    def test_write_probe_cleanup_failure_uses_stable_code(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch(
            "nas_access_capability.Path.unlink",
            side_effect=PermissionError(1, "denied"),
        ):
            with self.assertRaises(NasAccessError) as caught:
                require_writable_directory(Path(folder))

        self.assertEqual(caught.exception.code, "probe_cleanup_failed")

    def test_source_read_denial_has_stable_code(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.pdf"
            source.write_bytes(b"pdf")
            with mock.patch(
                "nas_access_capability.Path.open",
                side_effect=PermissionError(1, "denied"),
            ):
                with self.assertRaises(NasAccessError) as caught:
                    require_readable_file(source)

        self.assertEqual(caught.exception.code, "read_denied")

    def test_readable_source_returns_resolved_path(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.pdf"
            source.write_bytes(b"pdf")

            result = require_readable_file(source)

        self.assertEqual(result, source.resolve())

    def test_status_as_dict_has_stable_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = inspect_nas_access(root, root).as_dict()

        self.assertEqual(
            set(result),
            {
                "root",
                "mounted",
                "root_exists",
                "readable",
                "writable",
                "ready",
                "error_code",
                "message",
            },
        )
        self.assertEqual(result["root"], str(root.resolve()))


if __name__ == "__main__":
    unittest.main()
