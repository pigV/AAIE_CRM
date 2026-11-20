"""Persist a new file before a caller-owned database transaction.

Exclusive creation prevents overwrite. The save callback must finish its DB
commit before returning. Synchronous failures compensate the new file; a hard
process crash can still leave an unreferenced file, requiring the orphan audit.
"""
import hashlib
import os
from pathlib import Path


def persist_verified_file(path,data,save):
    path=Path(path);created=False
    try:
        with path.open('xb') as handle:
            created=True
            handle.write(data);handle.flush();os.fsync(handle.fileno())
        digest=hashlib.sha256(data).hexdigest()
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise OSError('persisted_file_verification_failed')
        return save(path,digest)
    except BaseException as error:
        if created:
            try:path.unlink()
            except OSError as cleanup:
                raise RuntimeError('file_cleanup_failed: '+str(path)) from error
        raise
