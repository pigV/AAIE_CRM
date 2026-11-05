"""Successful preview caches; legacy and failed placeholder images are never hits."""
from collections import OrderedDict
from functools import wraps
import hashlib
import json
from pathlib import Path
import threading
from uuid import uuid4

PDF_PREVIEW_SLOTS = threading.BoundedSemaphore(2)
_count_lock = threading.Lock()
_page_counts = OrderedDict()

# Fixed stripes keep the lock table bounded. Reentrant locks also cover the
# renderer called by a locked handler, including manual regeneration.
_document_locks = [threading.RLock() for _ in range(64)]


def synchronize_preview(document_argument):
    def decorate(function):
        @wraps(function)
        def synchronized(*args, **kwargs):
            document_id = args[document_argument] if len(args) > document_argument else kwargs['document_id']
            lock = _document_locks[hash(str(document_id)) % len(_document_locks)]
            with lock:
                return function(*args, **kwargs)
        return synchronized
    return decorate


def _signature(path):
    path = Path(path)
    stat = path.stat()
    return [str(path.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def preview_source_signature(path):
    return _signature(path)


def load_preview_cache(source, target, page):
    target = Path(target)
    try:
        record = json.loads(target.with_suffix(target.suffix + '.json').read_text())
        if (record['version'] == 1 and record['source'] == _signature(source)
                and record['image'] == _signature(target)
                and record['page'] == page and record['total_pages'] >= page >= 1):
            return record['total_pages']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def save_preview_cache(source, target, page, total_pages, source_signature):
    """Publish only after a successful render of an unchanged source."""
    target = Path(target)
    marker = target.with_suffix(target.suffix + '.json')
    temporary = marker.with_name(marker.name + '.' + uuid4().hex + '.tmp')
    try:
        if source_signature != _signature(source):
            return
        record = dict(version=1, source=source_signature, image=_signature(target),
                      page=page, total_pages=total_pages)
        temporary.write_text(json.dumps(record))
        temporary.replace(marker)
    except OSError:
        # A cache write failure must not prevent serving a rendered page.
        pass
    finally:
        temporary.unlink(missing_ok=True)


def cached_pdf_page_count(source, data, document, filename, reader):
    # Content identity also protects against a source replaced during a request.
    key = hashlib.sha256(data).digest()
    with _count_lock:
        if key in _page_counts:
            _page_counts.move_to_end(key)
            return _page_counts[key]
        count = reader(data, document, filename)
        _page_counts[key] = count
        if len(_page_counts) > 128:
            _page_counts.popitem(last=False)
        return count


def save_preview_image(image, target):
    target = Path(target)
    temporary = target.with_name(target.name + '.' + uuid4().hex + '.tmp')
    try:
        image.save(temporary, format='PNG')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
