"""Explicit fresh-fixture migration, executed only inside the Staging sandbox."""
from pathlib import Path
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime,timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from staging_environment import ACTIVE as c, IsolationError, SOURCE
if c is None:raise IsolationError('Staging fixture requires protected bootstrap')
from migration_runner import apply_migrations,discover_migrations


def migrate():
    package=c.check(c.root/'migration-package');package.mkdir(exist_ok=True)
    (package/'checks').mkdir(exist_ok=True)
    migrations=discover_migrations(SOURCE/'migrations')
    with sqlite3.connect(c.check(c.db)) as conn:
        exists=conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone()
        applied={r[0] for r in conn.execute('SELECT version FROM schema_migrations')} if exists else set()
    unknown=applied-{m.version for m in migrations}
    if unknown:raise IsolationError('Fixture registry exceeds this source authority; migration HOLD')
    for migration in migrations:
        # Build a complete immutable prefix, never skip registry history.
        for p in (SOURCE/'migrations').glob(migration.version+'*.sql'):shutil.copyfile(p,c.check(package/p.name))
        for p in (SOURCE/'migrations/checks').glob(migration.version+'*.sql'):shutil.copyfile(p,c.check(package/'checks'/p.name))
        if migration.version in applied:continue
        backup=c.check(c.root/'backups'/f'migration-{migration.version}-{datetime.now().strftime("%H%M%S%f")}.db')
        with sqlite3.connect(c.db) as src:
            src.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            with sqlite3.connect(backup) as dest:
                src.backup(dest)
                quick=dest.execute('PRAGMA quick_check').fetchone()[0]
                integrity=dest.execute('PRAGMA integrity_check').fetchone()[0]
        manifest=backup.with_suffix('.json')
        data={'database_path':str(c.db),'backup_path':str(backup),'backup_method':'sqlite_online_backup','quick_check':quick,'integrity_check':integrity,'created_at':datetime.now(timezone.utc).isoformat(),'source_database_sha256':hashlib.sha256(c.db.read_bytes()).hexdigest(),'backup_sha256':hashlib.sha256(backup.read_bytes()).hexdigest()}
        c.check(manifest).write_text(json.dumps(data))
        result=apply_migrations(c.db,package,applied_by='staging-fixture',source_commit=sys.argv[1],required_backup_metadata={'backup_path':str(backup),'manifest_path':str(manifest)})
        print(result['applied'],flush=True)

if __name__=='__main__':migrate()
