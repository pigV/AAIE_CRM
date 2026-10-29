#!/usr/bin/env python3
"""Explicit storage authority CLI. Not imported by the application; no startup hooks.

Normal SQL cannot register its connection-local capability. This does not defend
against arbitrary Python/DDL executed by the SQLite file owner.
"""
from pathlib import Path
import argparse
import datetime
import hashlib
import json
import os
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from revenue.secure_storage import read_reference, write_immutable, configured_write_root, _path
from revenue.storage_locations import document_fingerprint, location_history, read_document

EVENT_COLUMNS = ('operation_id','document_id','document_uid','sequence','previous_event_id',
    'old_path','new_path','sha256','size_bytes','document_fingerprint','occurred_at','authority',
    'reason','kind','reverses_event_id','verification_result','manifest_sha256','source_sha256',
    'destination_sha256','source_verification')
MIGRATION = ROOT/'migrations/20260908_036_revenue_storage_locations.sql'
FORMAL = Path('/Volumes/Zhang/AAIE CodeX/CRM/database/aaie_crm.db')


def require(value, message):
    if not value: raise ValueError(message)


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',', ':'),ensure_ascii=False)


def schema_digest(connection):
    rows = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name").fetchall()
    return hashlib.sha256(canonical([tuple(r) for r in rows]).encode()).hexdigest()


def _normalize(sql):
    return re.sub(r'\s+', ' ', sql.strip().rstrip(';'))


def verify_schema(connection, expected):
    require(schema_digest(connection)==expected,'schema authority changed')
    model=sqlite3.connect(':memory:')
    try:
        model.executescript(MIGRATION.read_text())
        reference=model.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name LIKE 'revenue_document_locations%'").fetchall()
        for name,sql in reference:
            actual=connection.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()
            require(actual and _normalize(actual[0])==_normalize(sql),'location guard schema mismatch')
    finally: model.close()
    source=(ROOT/'migrations/20260901_034_revenue_client_payment_receipt.sql').read_text()
    for name in ('revenue_documents_financial_no_update','revenue_documents_financial_no_delete'):
        sql=re.search(r'CREATE TRIGGER '+name+r'\b.*?END;',source,re.S).group()
        actual=connection.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()
        require(actual and _normalize(actual[0])==_normalize(sql),'financial immutability authority changed')
    require(connection.execute('PRAGMA trusted_schema').fetchone()[0]==1,'trusted-schema context incompatible; do not weaken settings')


def _query(connection):
    return lambda sql,parameters=():connection.execute(sql,parameters).fetchall()


def _document(connection, operation):
    expected=operation['document']
    row=connection.execute('SELECT * FROM revenue_documents WHERE id=?',(expected['id'],)).fetchone()
    require(row is not None and dict(row)==expected,'document identity/facts changed')
    history=location_history(expected,_query(connection))
    prior=history[-1] if history else None
    require(operation['expected_sequence']==len(history),'location sequence changed')
    require(operation['expected_path']==(prior['new_path'] if prior else expected['storage_path']),'old path changed')
    if operation['kind']=='restore':
        require(prior and prior['kind']=='relocate' and operation['new_path']==prior['old_path'],'restore must reverse current relocation')
    else: require(operation['kind']=='relocate','unsupported relocation operation')
    return expected,prior


def _read(path, doc, legacy, secure):
    return read_reference(legacy,path,doc['sha256'],doc['size_bytes'],secure_root=secure)


def _verify_files(operation, legacy, secure, *, copy):
    doc=operation['document']; target=_path(operation['new_path']);source=_path(operation['expected_path'])
    require(target!=source,'identical source/target')
    source_status='MATCH';source_sha=doc['sha256']
    if operation['kind']=='relocate':
        require(target.is_relative_to(secure),'new relocation must use secure root')
        data=_read(source,doc,legacy,secure)
        if copy:
            write_immutable(target.parent,target.name,data)
    else:
        # Recovery must work when the active copy is unavailable; never invent its checksum.
        try: _read(source,doc,legacy,secure)
        except (ValueError,OSError): source_status,source_sha='UNAVAILABLE',None
    data=_read(target,doc,legacy,secure)
    return source_sha,hashlib.sha256(data).hexdigest(),source_status


def _authorizer(action, arg1, arg2, database, context):
    if database not in (None,'main'): return sqlite3.SQLITE_DENY
    if action==sqlite3.SQLITE_INSERT:
        return sqlite3.SQLITE_OK if arg1=='revenue_document_locations' and context is None else sqlite3.SQLITE_DENY
    if action==sqlite3.SQLITE_FUNCTION:
        if arg2=='revenue_relocation_authorize':
            return sqlite3.SQLITE_OK if context=='revenue_document_locations_validate' else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if arg2 in ('length','trim','substr','max','coalesce') else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_TRANSACTION) else sqlite3.SQLITE_DENY


def _append(connection, values):
    connection.execute('INSERT INTO revenue_document_locations('+','.join(EVENT_COLUMNS)+') VALUES('+','.join('?' for _ in EVENT_COLUMNS)+')',values)


class VerificationAuthorityChanged(ValueError):
    pass


def _application_verify(dbpath, operations, legacy, secure):
    c=sqlite3.connect(Path(dbpath).as_uri()+'?mode=ro',uri=True)
    try:
        c.row_factory=sqlite3.Row
        c.execute('BEGIN')
        for op in operations:
            row=c.execute('SELECT * FROM revenue_documents WHERE id=?',(op['document']['id'],)).fetchone()
            if row is None or dict(row)!=op['document']:
                raise VerificationAuthorityChanged('application verification document authority changed')
            history=location_history(row,_query(c))
            if (not history or history[-1]['operation_id']!=op['operation_id']
                or history[-1]['sequence']!=op['expected_sequence']+1
                or history[-1]['new_path']!=op['new_path']):
                raise VerificationAuthorityChanged('application verification location authority advanced; HOLD')
            read_document(row,_query(c),legacy,secure)
    finally: c.close()


def execute_plan(dbpath, manifest, *, allow_formal=False):
    """Validate complete manifest, copy/verify, then append a single audited batch.

    Caller must separately approve real deployment. API has no shared connection,
    arbitrary SQL parameter, persistent grant, normal Admin endpoint or bypass flag.
    """
    dbpath=Path(dbpath).absolute()
    require(dbpath.exists() and not dbpath.is_symlink(),'database must exist and be a regular canonical path')
    require(allow_formal or not os.path.samefile(dbpath,FORMAL),'Production database requires explicit formal invocation')
    require(manifest.get('version')==1,'unknown manifest version')
    require(set(manifest)=={'version','authority','reason','legacy_root','secure_root','schema_sha256','operations'},'unknown manifest fields')
    require(all(isinstance(manifest[k],str) and manifest[k].strip() for k in ('authority','reason','schema_sha256')),'explicit authority/reason/schema required')
    legacy=_path(manifest['legacy_root']);secure=configured_write_root(legacy,{'REVENUE_SECURE_STORAGE_ROOT':manifest['secure_root']})
    operations=manifest['operations'];require(isinstance(operations,list) and 0<len(operations)<=100,'empty/oversized explicit batch')
    for op in operations:
        require(set(op)=={'operation_id','document','expected_path','expected_sequence','new_path','kind'},'unknown operation fields')
        require(isinstance(op['operation_id'],str) and op['operation_id'].strip(),'operation id required')
        require(type(op['expected_sequence']) is int and op['expected_sequence']>=0,'sequence required')
    require(len({o['kind'] for o in operations})==1,'mixed relocation/restore batch is forbidden')
    require(len({o['document']['id'] for o in operations})==len(operations),'duplicate document in batch')
    require(len({o['operation_id'] for o in operations})==len(operations),'duplicate operation id')
    manifest_sha=hashlib.sha256(canonical(manifest).encode()).hexdigest()
    authority=canonical({'approval':manifest['authority'],'uid':os.geteuid(),'gid':os.getegid(),
        'executable':str(Path(sys.executable).resolve()),
        'tool_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    c=sqlite3.connect(dbpath.as_uri()+'?mode=rw',uri=True,isolation_level=None,cached_statements=0)
    c.row_factory=sqlite3.Row
    c.enable_load_extension(False)
    c.execute('PRAGMA foreign_keys=ON')
    events=[]
    try:
        verify_schema(c,manifest['schema_sha256'])
        replays=[c.execute('SELECT * FROM revenue_document_locations WHERE operation_id=?',(o['operation_id'],)).fetchone() for o in operations]
        if any(replays):
            require(all(replays) and all(e['manifest_sha256']==manifest_sha for e in replays),'partial/conflicting replay')
            for op,event in zip(operations,replays):
                doc=c.execute('SELECT * FROM revenue_documents WHERE id=?',(op['document']['id'],)).fetchone()
                require(dict(doc)==op['document'],'replay document changed')
                history=location_history(doc,_query(c))
                require(history[-1]['id']==event['id'],'replay no longer current')
            _application_verify(dbpath,operations,legacy,secure)
            return {'status':'REPLAY','events':[dict(e) for e in replays]}
        for op in operations:
            _document(c,op)
            _verify_files(op,legacy,secure,copy=True)
        c.execute('BEGIN IMMEDIATE')
        verify_schema(c,manifest['schema_sha256'])
        approved=[]
        for op in operations:
            doc,prior=_document(c,op)
            source_sha,dest_sha,source_status=_verify_files(op,legacy,secure,copy=False)
            event=dict(zip(EVENT_COLUMNS,(op['operation_id'],doc['id'],doc['uid'],op['expected_sequence']+1,
                prior['id'] if prior else None,op['expected_path'],op['new_path'],doc['sha256'],doc['size_bytes'],
                document_fingerprint(doc),datetime.datetime.now(datetime.timezone.utc).isoformat(),authority,
                manifest['reason'],op['kind'],prior['id'] if op['kind']=='restore' else None,'PASS',manifest_sha,source_sha,dest_sha,source_status)))
            approved.append(tuple(event[k] for k in EVENT_COLUMNS));events.append(event)
        pending=list(approved)
        def authorize(*values):
            if not pending or tuple(values)!=pending[0]: return 0
            pending.pop(0)
            return 1
        c.create_function('revenue_relocation_authorize',len(EVENT_COLUMNS),authorize,deterministic=False)
        c.set_authorizer(_authorizer)
        for values in approved: _append(c,values)
        require(not pending,'incomplete authorization consumption')
        for op in operations:
            row=c.execute('SELECT * FROM revenue_documents WHERE id=?',(op['document']['id'],)).fetchone()
            require(dict(row)==op['document'],'financial facts changed')
            require(location_history(row,_query(c))[-1]['new_path']==op['new_path'],'published location mismatch')
        require(c.total_changes==len(operations),'unexpected database side effect')
        c.execute('COMMIT')
    except BaseException:
        if c.in_transaction: c.rollback()
        raise
    finally:
        c.close()
    try: _application_verify(dbpath,operations,legacy,secure)
    except VerificationAuthorityChanged:
        raise
    except Exception as error:
        if all(o['kind']=='relocate' for o in operations):
            reverse=dict(manifest)
            reverse['reason']='Restore after application verification failure: '+manifest['reason']
            reverse['operations']=[dict(o,operation_id=o['operation_id']+':restore',expected_path=o['new_path'],
                new_path=o['expected_path'],expected_sequence=o['expected_sequence']+1,kind='restore') for o in operations]
            execute_plan(dbpath,reverse,allow_formal=allow_formal)
            raise ValueError('application verification failed; audited restore completed; copies retained') from error
        raise ValueError('restore reference committed but application verification failed; HOLD; files retained') from error
    return {'status':'VERIFIED','events':events,'financial_facts_unchanged':True,'source_retained':True}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database',required=True);p.add_argument('--manifest',required=True)
    p.add_argument('--allow-formal-database',action='store_true');p.add_argument('--confirm')
    args=p.parse_args()
    if args.allow_formal_database and args.confirm!='APPROVED-REVENUE-STORAGE-RELOCATION':
        p.error('formal database requires exact separate cutover confirmation')
    result=execute_plan(Path(args.database),json.loads(Path(args.manifest).read_text()),allow_formal=args.allow_formal_database)
    print(json.dumps(result,indent=2))

if __name__=='__main__': main()
