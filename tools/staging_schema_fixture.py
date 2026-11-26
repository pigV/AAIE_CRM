#!/usr/bin/env python3
"""Create an EMPTY staging fixture from an explicitly selected verified schema.

No implicit production source. Only schema and migration registry are copied.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.staging_control import load, require_stopped
from staging_environment import canonical
from migration_runner import verify_migrations


MIGRATIONS = Path(__file__).resolve().parents[1] / 'migrations'


def create(schema_source, *, runtime=None):
    c = runtime or load()
    require_stopped(c)
    target_path = c.check(c.db)
    if target_path.exists():
        raise FileExistsError('Schema initialization requires a new empty target path; existing database preserved')
    source_path = canonical(schema_source)
    if not source_path.is_file() or source_path == target_path:
        raise ValueError('Explicit existing schema source required')
    result = verify_migrations(source_path, MIGRATIONS)
    if result['pending'] or result['foreign_key_violations']:
        raise RuntimeError('Source schema/migration verification failed')
    with closing(sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)) as source:
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        if source.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise RuntimeError('Source integrity failed')
        schema = source.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'view' THEN 2 ELSE 3 END,name").fetchall()
        columns = [r[1] for r in source.execute('PRAGMA table_info(schema_migrations)')]
        registry = source.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall()
    stamp = datetime.now().strftime('%Y%m%d%H%M%S%f')
    candidate = c.check(c.root / f'database/schema-fixture-{stamp}.db')
    candidate.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(candidate)) as target:
        for _, _, sql in schema:
            target.execute(sql)
        target.executemany('INSERT INTO schema_migrations (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')', registry)
        target.commit()
        if target.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or target.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Empty fixture integrity failed')
    result = verify_migrations(candidate, MIGRATIONS)
    if result['pending'] or result['foreign_key_violations']:
        raise RuntimeError('Empty fixture migration verification failed')
    # Atomic no-overwrite install, even if another initializer raced this one.
    import os
    os.link(candidate, target_path)
    candidate.unlink()
    receipt = {'method':'explicit_verified_schema_only', 'source':str(source_path), 'target':str(target_path),
               'business_rows_copied':0, 'settings_copied':0, 'users_copied':0,
               'migration_registry_rows':len(registry), 'schema_sha256':hashlib.sha256(repr(schema).encode()).hexdigest(),
               'created_at':datetime.now(timezone.utc).isoformat(), 'integrity':'ok'}
    c.check(c.root / 'schema-fixture-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--schema-source', required=True)
    print(json.dumps(create(parser.parse_args().schema_source), indent=2))
