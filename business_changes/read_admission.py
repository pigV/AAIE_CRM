"""Bound CPU-heavy snapshot work; overflow must never wait in a read queue."""
from functools import wraps
from threading import BoundedSemaphore

_snapshot_slots=BoundedSemaphore(1)


def limit_snapshot_reads(function):
    @wraps(function)
    def wrapped(handler,method):
        if method!='GET' or not getattr(handler,'user',None):return function(handler,method)
        if not _snapshot_slots.acquire(blocking=False):
            return handler.json_response({'ok':False,'code':'snapshot_busy','retry_after_seconds':2},503)
        try:return function(handler,method)
        finally:_snapshot_slots.release()
    return wrapped
