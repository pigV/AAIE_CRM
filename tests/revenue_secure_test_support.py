"""Synthetic artefacts only; database temporary directories are never relocated."""
import os
from pathlib import Path
import shutil
import stat
import tempfile
from unittest.mock import patch

APPROVED_ROOT = Path('/Users/faqiang/CRM-Revenue-Secure-Test')

def secure_directory(testcase):
    raw = os.environ.get('REVENUE_POSIX_TEST_ROOT')
    if raw != str(APPROVED_ROOT):
        raise RuntimeError('Explicit approved REVENUE_POSIX_TEST_ROOT is required')
    if APPROVED_ROOT.is_symlink() or APPROVED_ROOT.resolve() != APPROVED_ROOT:
        raise RuntimeError('Synthetic storage root must not contain symlinks')
    directory = Path(tempfile.mkdtemp(prefix='revenue-fixture-', dir=APPROVED_ROOT))
    testcase.addCleanup(shutil.rmtree, directory)
    os.chmod(directory, 0o700)
    if stat.S_IMODE(directory.stat().st_mode) != 0o700:
        raise RuntimeError('Synthetic directory does not support exact 0700')
    return directory

def configure_secure_directory(testcase):
    directory = secure_directory(testcase)
    configuration = patch.dict(os.environ, {'REVENUE_SECURE_STORAGE_ROOT': str(directory)})
    configuration.start()
    testcase.addCleanup(configuration.stop)
    return directory

def write_test_document(path, payload):
    path = Path(path)
    if not path.is_relative_to(APPROVED_ROOT):
        raise RuntimeError('Synthetic sensitive document must stay in approved storage')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            raise RuntimeError('Synthetic document does not support exact 0600')
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(payload)
            stream.flush()
    finally:
        os.close(fd)
    return path
