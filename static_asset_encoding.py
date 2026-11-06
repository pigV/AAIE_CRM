"""Bounded gzip representations for public static text assets only."""
import gzip
import threading
from collections import OrderedDict

TEXT_SUFFIXES = {'.css', '.js', '.mjs', '.json', '.svg', '.txt', '.xml', '.html'}
MIN_SIZE = 1024
MAX_FILE_SIZE = 4 * 1024 * 1024
MAX_CACHE_BYTES = 8 * 1024 * 1024
MAX_CACHE_ENTRIES = 128
_cache = OrderedDict()
_cache_bytes = 0
_lock = threading.Lock()


def accepts_gzip(value):
    """An explicit gzip refusal takes precedence over a wildcard."""
    qualities = {}
    for item in str(value or '').split(','):
        parts = [part.strip().lower() for part in item.split(';')]
        quality = 1.0
        for parameter in parts[1:]:
            if parameter.startswith('q='):
                try:
                    quality = float(parameter[2:])
                except ValueError:
                    quality = 0.0
        qualities[parts[0]] = quality if 0 <= quality <= 1 else 0.0
    return qualities.get('gzip', qualities.get('*', 0)) > 0


def asset_representation(target, accept_encoding, if_none_match=None):
    """Return bytes, representation ETag and optional content encoding.

    The file signature includes ctime and inode so replacing a same-size file
    or restoring its mtime cannot reuse an earlier compressed representation.
    The lock avoids concurrent requests compressing the same cold asset.
    """
    global _cache_bytes
    stat = target.stat()
    signature = (str(target), stat.st_ino, stat.st_mtime_ns,
                 stat.st_ctime_ns, stat.st_size)
    etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}-{stat.st_ctime_ns:x}-{stat.st_ino:x}"'
    if (target.suffix.lower() not in TEXT_SUFFIXES
            or not MIN_SIZE <= stat.st_size <= MAX_FILE_SIZE
            or not accepts_gzip(accept_encoding)):
        if if_none_match == etag:
            return None, etag, None
        return target.read_bytes(), etag, None
    with _lock:
        if signature in _cache:
            payload, encoding = _cache.pop(signature)
            _cache[signature] = (payload, encoding)
        else:
            raw = target.read_bytes()
            compressed = gzip.compress(raw, compresslevel=6, mtime=0)
            payload, encoding = ((compressed, 'gzip') if len(compressed) < len(raw)
                                 else (raw, None))
            # A concurrent writer can change a file between stat and read.
            current = target.stat()
            unchanged = signature[1:] == (current.st_ino, current.st_mtime_ns,
                                          current.st_ctime_ns, current.st_size)
            if unchanged:
                while _cache and (len(_cache) >= MAX_CACHE_ENTRIES
                                  or _cache_bytes + len(payload) > MAX_CACHE_BYTES):
                    _, (old_payload, _) = _cache.popitem(last=False)
                    _cache_bytes -= len(old_payload)
                if len(payload) <= MAX_CACHE_BYTES:
                    _cache[signature] = (payload, encoding)
                    _cache_bytes += len(payload)
    return payload, etag[:-1] + '-gzip"' if encoding else etag, encoding
