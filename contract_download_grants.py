"""Short-lived authority to retrieve one already-finalized contract PDF.

Only the authenticated contract handler issues grants. Redemption still requires
the existing live link, contract-status and archive checks. The bearer grant is
reusable briefly because mobile browsers may retry a download in another process.
"""
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import unquote_plus

from cryptography.fernet import InvalidToken

GRANT_TTL_SECONDS = 300
PURPOSE = 'contract-final-download-v1'


def _scope(link, path, payload=None):
    path = Path(path).resolve()
    if payload is None:
        payload = path.read_bytes()
    return dict(
        purpose=PURPOSE,
        link_id=int(link['id']),
        contract_id=int(link['contract_id']),
        signature_id=int(link['signed_signature_id']),
        token_hash=str(link['token_hash']),
        identity=hashlib.sha256(str(link['bound_wechat_openid']).encode()).hexdigest(),
        artifact=hashlib.sha256(str(path).encode() + b'\0' + payload).hexdigest(),
    )


def issue_contract_download_grant(cipher, link, path):
    if str(link['status']).lower() != 'signed' or not link['bound_wechat_openid'] or not link['signed_signature_id']:
        raise ValueError('A completed, identity-bound signing link is required')
    return cipher.encrypt(json.dumps(_scope(link, path), separators=(',', ':')).encode()).decode('ascii')


def verify_contract_download_grant(cipher, grant, link, path, *, payload=None):
    if not isinstance(grant, str) or not 1 <= len(grant) <= 2048:
        return False
    try:
        decoded = cipher.decrypt_at_time(grant.encode('ascii'), GRANT_TTL_SECONDS, int(time.time()))
        return json.loads(decoded) == _scope(link, path, payload)
    except (InvalidToken, ValueError, TypeError, KeyError, OSError, UnicodeError):
        return False


def redact_contract_download_grants(message):
    def redact(match):
        if unquote_plus(match.group(2)) == 'download_grant':
            return match.group(1) + match.group(2) + '=[redacted]'
        return match.group(0)
    return re.sub(r'([?&])([^=&\s"\']+)=([^&\s"\']*)', redact, message)
