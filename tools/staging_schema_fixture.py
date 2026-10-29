#!/usr/bin/env python3
"""Create an EMPTY fixture from a consistent in-memory Production schema snapshot.

Only schema and migration registry leave memory. No Production rows, users,
sessions, secrets, file references or settings are copied to Staging.
"""
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.staging_control import load,require_stopped
from staging_environment import PRODUCTION_DB


def create():
    c=load();require_stopped(c);c.check(c.db)
    with closing(sqlite3.connect(PRODUCTION_DB.as_uri()+'?mode=ro',uri=True)) as source, closing(sqlite3.connect(':memory:')) as snapshot:
        source.execute('PRAGMA query_only=ON')
        source.backup(snapshot)
        if snapshot.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise RuntimeError('Snapshot integrity failed')
        schema=snapshot.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'view' THEN 2 ELSE 3 END,name").fetchall()
        columns=[r[1] for r in snapshot.execute('PRAGMA table_info(schema_migrations)')]
        registry=snapshot.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall()
    stamp=datetime.now().strftime('%Y%m%d%H%M%S%f')
    candidate=c.check(c.root/f'database/schema-fixture-{stamp}.db')
    with closing(sqlite3.connect(candidate)) as target:
        for _,_,sql in schema:target.execute(sql)
        target.executemany('INSERT INTO schema_migrations VALUES('+','.join('?' for _ in columns)+')',registry)
        target.commit()
        if target.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise RuntimeError('Empty fixture integrity failed')
        if target.execute('PRAGMA foreign_key_check').fetchall():raise RuntimeError('Empty fixture foreign keys failed')
    if c.db.exists():
        archive=c.check(c.root/f'backups/pre-schema-fixture-{stamp}.db');c.db.rename(archive)
        for suffix in ['-wal','-shm']:
            side=c.check(Path(str(c.db)+suffix))
            if side.exists():side.rename(c.check(Path(str(archive)+suffix)))
    candidate.rename(c.db)
    c.check(c.root/'schema-fixture-receipt.json').write_text(json.dumps({'method':'sqlite_online_backup_to_memory_then_schema_only','source':str(PRODUCTION_DB),'target':str(c.db),'business_rows_copied':0,'settings_copied':0,'users_copied':0,'migration_registry_rows':len(registry),'schema_sha256':hashlib.sha256(repr(schema).encode()).hexdigest(),'created_at':datetime.now(timezone.utc).isoformat(),'integrity':'ok'},indent=2)+'\n')
    print('Empty schema fixture created; no Production business rows copied')

if __name__=='__main__':create()
