import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def password_hash(password):
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + base64.urlsafe_b64encode(salt + digest).decode("ascii")


def password_verify(password, encoded):
    try:
        kind, payload = encoded.split("$", 1)
        if kind != "scrypt":
            return False
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        salt, expected = raw[:16], raw[16:]
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def new_token(size=32):
    return secrets.token_urlsafe(size)


def expiry_iso(days=14):
    return (datetime.now(timezone.utc).astimezone() + timedelta(days=days)).isoformat(timespec="seconds")


def is_expired(value):
    if not value:
        return True
    return datetime.fromisoformat(value) < datetime.now(timezone.utc).astimezone()


def safe_compare(left, right):
    return hmac.compare_digest(str(left or ""), str(right or ""))

