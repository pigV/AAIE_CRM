"""Prepare the exact local Vision executable before starting Staging services."""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def build():
    data = ROOT / 'STAGING_DATA'
    source = ROOT / 'tools/macos_vision_ocr.swift'
    target = data / 'tools/document-vision-ocr'
    receipt = target.with_suffix('.json')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if target.is_file() and receipt.is_file():
        previous = json.loads(receipt.read_text())
        if (previous.get('source_sha256') == digest and
                previous.get('binary_sha256') == hashlib.sha256(target.read_bytes()).hexdigest()):
            return target
    target.parent.mkdir(parents=True, exist_ok=True)
    cache = data / 'cache/swift_module_cache'
    cache.mkdir(parents=True, exist_ok=True)
    sdk = subprocess.check_output(['/usr/bin/xcrun', '--show-sdk-path'], text=True).strip()
    compiler = subprocess.check_output(['/usr/bin/xcrun', '--find', 'swiftc'], text=True).strip()
    env = os.environ.copy()
    env['TMPDIR'] = str(data/'tmp')
    pending = target.with_suffix('.pending')
    result = subprocess.run([compiler, '-sdk', sdk, '-target', platform.machine()+'-apple-macosx15.0',
                             '-module-cache-path', str(cache), str(source), '-o', str(pending)],
                            env=env, capture_output=True, text=True, timeout=180)
    (data/'logs/document-ocr-build.log').write_text(result.stderr)
    if result.returncode:
        raise RuntimeError('OCR build failed; see STAGING_DATA/logs/document-ocr-build.log')
    os.replace(pending, target)
    receipt.write_text(json.dumps({'source_sha256': digest,
                                  'binary_sha256': hashlib.sha256(target.read_bytes()).hexdigest()}, indent=2)+'\n')
    return target


if __name__ == '__main__':
    print(build())
