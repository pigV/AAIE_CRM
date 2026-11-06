"""Prepare the exact local Vision executable before starting managed services."""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def checked(path):
    """Reject linked build inputs/destinations before any unsandboxed writes."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('OCR paths must be absolute')
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Linked OCR build path rejected')
    if path.is_file() and path.stat().st_nlink != 1:
        raise ValueError('Hardlinked OCR build file rejected')
    return path


def build(*, data_root=None, source_root=None):
    data = checked(Path(data_root) if data_root is not None else ROOT / 'STAGING_DATA')
    source = checked((Path(source_root) if source_root is not None else ROOT) / 'tools/macos_vision_ocr.swift')
    target = checked(data / 'tools/document-vision-ocr')
    receipt = checked(target.with_suffix('.json'))
    log = checked(data / 'logs/document-ocr-build.log')
    cache = checked(data / 'cache/swift_module_cache')
    tmp = checked(data / 'tmp')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if target.is_file() and os.access(target, os.X_OK) and receipt.is_file():
        previous = json.loads(receipt.read_text())
        if (previous.get('source_sha256') == digest and
                previous.get('binary_sha256') == hashlib.sha256(target.read_bytes()).hexdigest()):
            return target
    for folder in (target.parent, tmp, log.parent, cache):
        checked(folder).mkdir(parents=True, exist_ok=True)
    sdk = subprocess.check_output(['/usr/bin/xcrun', '--show-sdk-path'], text=True).strip()
    compiler = subprocess.check_output(['/usr/bin/xcrun', '--find', 'swiftc'], text=True).strip()
    env = os.environ.copy()
    env['TMPDIR'] = str(tmp)
    # The compiler writes only inside a fresh private directory, never through
    # a pre-existing .pending link. Publish artifacts by replacing directory entries.
    with tempfile.TemporaryDirectory(prefix='.ocr-build-', dir=checked(target.parent)) as folder:
        pending = Path(folder) / target.name
        result = subprocess.run([compiler, '-sdk', sdk, '-target', platform.machine()+'-apple-macosx15.0',
                                 '-module-cache-path', str(cache), str(source), '-o', str(pending)],
                                env=env, capture_output=True, text=True, timeout=180)
        pending_log = Path(folder) / 'build.log'
        pending_log.write_text(result.stderr)
        os.replace(pending_log, checked(log))
        if result.returncode:
            raise RuntimeError(f'OCR build failed; see {log}')
        checked(pending).chmod(0o700)
        pending_receipt = Path(folder) / 'receipt.json'
        pending_receipt.write_text(json.dumps({'source_sha256': digest,
            'binary_sha256': hashlib.sha256(pending.read_bytes()).hexdigest()}, indent=2)+'\n')
        os.replace(pending, checked(target))
        os.replace(pending_receipt, checked(receipt))
    return target


if __name__ == '__main__':
    print(build())
