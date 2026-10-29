"""Borrow the existing Drive writer lock without changing policy or file bytes.

This excludes confirm writers only. A release must additionally drain in-flight
scheduled scans and prove the DB family has no open handles before restore.
"""
import fcntl
import os
from pathlib import Path
import stat


class ExistingWriterLease:
    def __init__(self,path):
        self.path=Path(path).absolute();self.fd=None;self.used=False

    def __enter__(self):
        if self.used:raise RuntimeError('writer_lease_attempt_consumed')
        self.used=True
        if any(p.is_symlink() for p in (self.path,*self.path.parents)):
            raise RuntimeError('writer_lock_symlink')
        fd=os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW)
        try:
            st=os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):raise RuntimeError('writer_lock_not_regular')
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.fd=fd;self.identity=(st.st_dev,st.st_ino)
            self.assert_held()
        except BaseException:
            os.close(fd);self.fd=None
            raise
        return self

    def assert_held(self):
        if self.fd is None:raise RuntimeError('writer_lease_not_held')
        st=self.path.stat(follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev,st.st_ino)!=self.identity:
            raise RuntimeError('writer_lock_identity_changed')
        actual=os.fstat(self.fd)
        if (actual.st_dev,actual.st_ino)!=self.identity:
            raise RuntimeError('writer_lock_descriptor_changed')

    def __exit__(self,*args):
        if self.fd is not None:
            try:fcntl.flock(self.fd,fcntl.LOCK_UN)
            finally:os.close(self.fd);self.fd=None
