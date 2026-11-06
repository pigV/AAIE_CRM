"""Per-interpreter synthetic service fixtures; migration tests never use this cache."""
from contextlib import closing
from pathlib import Path
import sqlite3,tempfile,threading

class FixtureTemplate:
    def __init__(self,build):
        self.build=build;self._folder=None;self._seed=None;self._lock=threading.RLock()
    def __enter__(self):return self
    def close(self):
        with self._lock:
            if self._folder:self._folder.cleanup()
            self._folder=None;self._seed=None
    def __exit__(self,*exc):self.close()
    def copy_to(self,path):
        path=Path(path)
        with self._lock:
            # Exclusive creation prevents clobbering even if callers race.
            with path.open('xb'):pass
            try:
                if self._seed is None:
                    folder=tempfile.TemporaryDirectory(prefix='synthetic-lifecycle-template-')
                    seed=Path(folder.name)/'seed.db'
                    try:
                        self.build(seed)
                        with closing(sqlite3.connect(f'{seed.as_uri()}?mode=ro',uri=True)) as c:
                            if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or c.execute('PRAGMA foreign_key_check').fetchall():
                                raise ValueError('Invalid synthetic fixture seed')
                    except BaseException:folder.cleanup();raise
                    self._folder,self._seed=folder,seed
                with closing(sqlite3.connect(f'{self._seed.as_uri()}?mode=ro',uri=True)) as source:
                    with closing(sqlite3.connect(path)) as target:source.backup(target)
            except BaseException:
                # Only the empty/partial file exclusively created by this method.
                path.unlink(missing_ok=True)
                raise
