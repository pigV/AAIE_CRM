"""Immutable student tuition proof under the managed Revenue directory."""

import os
from pathlib import Path
import stat

from revenue.secure_storage import StorageUnavailable, _open_directory, _path, read_secure
from revenue.uploads import stage_revenue_upload

DATA = Path("/Volumes/Data")


class TuitionEvidenceStorage:
    def __init__(self, legacy_root, configured_root=None):
        configured = configured_root or os.environ.get("REVENUE_SECURE_STORAGE_ROOT")
        if not configured:
            raise StorageUnavailable()
        root = _path(configured)
        if legacy_root is not None:
            legacy = _path(legacy_root)
            if root.is_relative_to(legacy) or legacy.is_relative_to(root):
                raise StorageUnavailable()
        self.mount = DATA if root.is_relative_to(DATA) else None
        device = None
        if self.mount:
            try:
                mounted = DATA.stat(follow_symlinks=False)
            except OSError as exc:
                raise StorageUnavailable() from exc
            if not DATA.is_mount() or not stat.S_ISDIR(mounted.st_mode) or mounted.st_mode & 0o002:
                raise StorageUnavailable()
            device = mounted.st_dev
        with _open_directory(root, writable_mount=self.mount) as (_, fd):
            if device is not None and os.fstat(fd).st_dev != device:
                raise StorageUnavailable()
        self.root = root / "tuition-evidence"

    def stage(self, upload, extensions):
        return stage_revenue_upload(self.root, upload, extensions, writable_mount=self.mount)

    def read(self, path, digest, size):
        target = _path(path)
        if target.parent != self.root or target.stem != digest:
            raise StorageUnavailable()
        return read_secure(self.root, target, digest, size, writable_mount=self.mount)
