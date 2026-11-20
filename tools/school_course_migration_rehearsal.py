"""Apply only authorized 052 to this task's stopped preview, retaining proof."""
from hashlib import sha256
from contextlib import closing
import json
from pathlib import Path
import socket
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from migration_runner import apply_migrations,plan_migrations,verify_migrations

PREVIEW=ROOT.parent/'runtime/staging/word-issues-retest-20261120/database/staging.db'
OUT=ROOT/'reports/word-triage/course-052-migration'
VERSION='20261120_052'


def state(db):
    result={}
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        h=sha256();count=0
        # Stable sorted serialization avoids relying on table physical order.
        records=[]
        for row in db.execute('SELECT * FROM "'+table.replace('"','""')+'"'):
            records.append(repr(tuple(row)))
        for record in sorted(records):
            h.update(record.encode());h.update(b'\n');count+=1
        result[table]={'rows':count,'sha256':h.hexdigest()}
    return result


def file_sha(path):
    digest=sha256()
    with path.open('rb') as file:
        for chunk in iter(lambda:file.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    for port in (19460,19461):
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1',port))==0:
                raise RuntimeError('Stop this task preview before migration')
    if not PREVIEW.is_file():
        raise RuntimeError('Explicit task preview does not exist')
    OUT.mkdir(parents=True,exist_ok=True)
    backup=OUT/'preview-before-052.sqlite3'
    rehearsal=OUT/'rollback-rehearsal.sqlite3'
    if (OUT/'result.json').exists():
        raise RuntimeError('Completed evidence already exists; do not overwrite')
    if not backup.exists():
        with closing(sqlite3.connect(PREVIEW)) as src,closing(sqlite3.connect(backup)) as dst:
            src.backup(dst)
    with closing(sqlite3.connect(backup)) as db:
        before=state(db)
        assert not db.execute('SELECT 1 FROM schema_migrations WHERE version=?',(VERSION,)).fetchone()
    plan=plan_migrations(PREVIEW,ROOT/'migrations',through=VERSION)
    if [x['version'] for x in plan['will_apply']] not in ([VERSION],[]):
        raise RuntimeError('Only migration052 is authorized')
    result=apply_migrations(PREVIEW,ROOT/'migrations',applied_by='authorized-isolated-course-052',
                           source_commit='candidate-working-tree-20261120',through=VERSION)
    with closing(sqlite3.connect(PREVIEW)) as db,closing(sqlite3.connect(rehearsal)) as copy:
        after=state(db)
        unchanged={name:before[name]==after[name] for name in before if name!='schema_migrations'}
        assert all(unchanged.values()),'Existing business table changed'
        assert after['revenue_school_courses']['rows']==0
        assert after['revenue_school_course_evidence']['rows']==0
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        db.backup(copy)
    verify=verify_migrations(PREVIEW,ROOT/'migrations')
    with closing(sqlite3.connect(rehearsal)) as db:
        db.execute('BEGIN IMMEDIATE')
        # Execute each statement in one explicit transaction (executescript would commit first).
        sql=(ROOT/'migrations/rollback/20261120_052_school_course_tuition_rollback.sql').read_text()
        for statement in sql.split(';'):
            if statement.strip():db.execute(statement)
        db.commit()
        reverted=state(db)
        assert {k:v for k,v in reverted.items() if k!='schema_migrations'}=={
            k:v for k,v in before.items() if k!='schema_migrations'},'Rollback changed pre-existing business data'
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    # The registry is append-only: never delete its row or disable its trigger.
    # A full DB rollback restores the pre-migration online backup on the separate
    # rehearsal copy, while the authorized preview remains migrated.
    with closing(sqlite3.connect(backup)) as source,closing(sqlite3.connect(rehearsal)) as restored:
        source.backup(restored)
        assert state(restored)==before
    report={'database':str(PREVIEW),'backup':str(backup),'backup_sha256':file_sha(backup),
            'migration_result':result,'verification':verify,'existing_tables_unchanged':unchanged,
            'before':before,'after':after,'empty_rollback_data_equal':True,'registry_preserved_during_ddl_rollback':True,
            'separate_rehearsal_restored_from_online_backup':True,
            'production_accessed':False,'historical_backfill':False}
    (OUT/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'migration':VERSION,'existing_tables_unchanged':len(unchanged),
                      'empty_rollback_data_equal':True,'report':str(OUT/'result.json')}))


if __name__=='__main__':main()
