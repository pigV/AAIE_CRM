"""Small shared helpers for correction audit modules."""

import hashlib


def carrier_fingerprint(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
