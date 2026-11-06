"""Local implementation and artifact identity checks for reproducible experiments."""
from pathlib import Path
import hashlib
import json


def implementation_signature(root):
    root=Path(root);digest=hashlib.sha256()
    for name in ('local_document_lab/infer_v2.py','local_document_lab/evidence.py',
                 'local_document_lab/project.py','STAGING_DATA/environment.lock'):
        digest.update(name.encode());digest.update((root/name).read_bytes())
    return digest.hexdigest()


def verify_model_snapshot(directory):
    directory=Path(directory).resolve();receipt=directory/'download-receipt.json';raw=receipt.read_bytes()
    manifest=json.loads(raw)
    if not manifest.get('files'):raise ValueError('Model inventory is empty')
    for item in manifest['files']:
        path=(directory/item['name']).resolve()
        if not path.is_relative_to(directory) or not path.is_file():raise ValueError('Invalid model artifact path')
        if path.stat().st_size!=item['bytes']:raise ValueError('Model artifact size mismatch')
        with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        if digest!=item['sha256']:raise ValueError('Model artifact checksum mismatch')
    return hashlib.sha256(raw).hexdigest()


def packet_identity_matches(packet,case):
    return packet.get('id')==case.get('id') and packet.get('kind')==case.get('kind')
