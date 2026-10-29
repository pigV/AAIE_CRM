"""Strict auth-only restore projection. No application import or input mutation.

Caller must hold the release writer lease, drain requests, seal/verify source
files and independently authorize schema/registry changes. This module never
installs a destination or changes a live path. Reports contain aggregates only.
"""
from contextlib import closing
import hashlib,json,os,sqlite3
from pathlib import Path

VISA_037_038_EXTRA_TABLES=frozenset(('visa_document_keyword_rules','visa_document_metadata','visa_processing_date_extractions','visa_processing_date_revisions'))
VISA_039_EXTRA_TABLES=frozenset(('visa_result_status_events','visa_result_status_current'))
EXTRA_TABLES=VISA_037_038_EXTRA_TABLES|VISA_039_EXTRA_TABLES
AUTH_TABLES=frozenset(('users','sessions','audit_logs'))
EXCLUDED=frozenset(('schema_migrations','sqlite_sequence'))
LOGIN_COLUMNS=frozenset(('last_login_at','last_login_ip'))

def _quote(s):return '"'+s.replace('"','""')+'"'
def _path(value):
 p=Path(value).absolute()
 if any(x.is_symlink() for x in (p,*p.parents)):raise ValueError('symlink_path')
 return p

def _read(path,*,live=False):
 path=_path(path)
 wal=Path(str(path)+'-wal')
 if wal.is_symlink():raise ValueError('symlink_wal')
 if not live and wal.exists() and wal.stat().st_size:raise ValueError('checkpoint_required_before_auth_snapshot')
 c=sqlite3.connect(path.as_uri()+('?mode=ro' if live else '?mode=ro&immutable=1'),uri=True);c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');c.execute('BEGIN');return c

def _tables(c):return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
def _cols(c,t):return [r[1] for r in c.execute('PRAGMA table_info('+_quote(t)+')')]
def _rows(c,t):return [dict(r) for r in c.execute('SELECT * FROM '+_quote(t))]
def _encode(row):
 return json.dumps(row,sort_keys=True,separators=(',',':'),ensure_ascii=False,default=lambda x:{'blob':x.hex()} if isinstance(x,bytes) else (_ for _ in ()).throw(TypeError())).encode()
def _digest(rows):
 hashes=sorted(hashlib.sha256(_encode(row)).digest() for row in rows);h=hashlib.sha256()
 for v in hashes:h.update(v)
 return {'count':len(hashes),'sha256':h.hexdigest()}
def table_projection(connection,table):
 """Aggregate hash of complete rows, suitable for a sealed extra-table receipt."""
 cursor=connection.execute('SELECT * FROM '+_quote(table));columns=[d[0] for d in cursor.description]
 return _digest(dict(zip(columns,row)) for row in cursor)
def _integrity(c):
 if [r[0] for r in c.execute('PRAGMA integrity_check')]!=['ok'] or c.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('database_integrity_failed')
def _schema(c):return [tuple(r) for r in c.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name')]

def _validate(b,c,extra):
 _integrity(b);_integrity(c);bt=_tables(b);ct=_tables(c)
 if not AUTH_TABLES<=bt or not bt<=ct:raise ValueError('required_table_missing')
 extras=ct-bt
 if set(extra)!=extras or not extras<=EXTRA_TABLES:raise ValueError('unapproved_extra_tables')
 baseline_schema={(r[0],r[1]):r for r in _schema(b)};current_schema={(r[0],r[1]):r for r in _schema(c)}
 if any(current_schema.get(k)!=v for k,v in baseline_schema.items()):raise ValueError('existing_schema_object_changed')
 if any(v[2] not in extras for k,v in current_schema.items() if k not in baseline_schema):raise ValueError('unexpected_schema_object')
 for t in extras:
  if table_projection(c,t)!=extra[t]:raise ValueError('extra_table_changed')
 for t in bt-EXCLUDED:
  if list(b.execute('PRAGMA table_info('+_quote(t)+')'))!=list(c.execute('PRAGMA table_info('+_quote(t)+')')):raise ValueError('existing_table_schema_changed')
 protected={}
 for t in sorted(bt-AUTH_TABLES-EXCLUDED):
  protected[t]=table_projection(b,t)
  if protected[t]!=table_projection(c,t):raise ValueError('business_delta')
 users_b={r['id']:r for r in _rows(b,'users')};users_c={r['id']:r for r in _rows(c,'users')}
 if set(users_b)!=set(users_c) or not LOGIN_COLUMNS<=set(_cols(b,'users')):raise ValueError('user_identity_delta')
 for uid,row in users_b.items():
  if {k:v for k,v in row.items() if k not in LOGIN_COLUMNS}!={k:v for k,v in users_c[uid].items() if k not in LOGIN_COLUMNS}:raise ValueError('protected_user_delta')
 sessions=_rows(c,'sessions')
 if any(r['user_id'] not in users_b for r in sessions):raise ValueError('session_unknown_user')
 audits_b={r['id']:r for r in _rows(b,'audit_logs')};audits_c={r['id']:r for r in _rows(c,'audit_logs')}
 if any(audits_c.get(k)!=v for k,v in audits_b.items()):raise ValueError('existing_audit_changed')
 new=[r for k,r in audits_c.items() if k not in audits_b];maximum=max(audits_b,default=0)
 known=frozenset(('id','user_id','username','action_type','module_name','module_key','record_id','file_path','old_value','new_value','student_id','contract_id','ip_address','user_agent','device_info','status','error_message','created_at'))
 for r in new:
  if set(r)-known or type(r['id']) is not int or r['id']<=maximum:raise ValueError('audit_shape_or_id_invalid')
  if r['action_type'] not in ('login_success','login_failed','logout') or r['module_name']!='auth' or r.get('module_key')!='auth':raise ValueError('non_auth_audit')
  if any(r.get(k) not in (None,'') for k in ('file_path','old_value','new_value','student_id','contract_id')):raise ValueError('audit_business_payload')
  if r.get('user_id') is not None and r['user_id'] not in users_b:raise ValueError('audit_unknown_user')
  uid=r.get('user_id')
  if uid is not None and r.get('username')!=users_b[uid]['username']:raise ValueError('audit_username_mismatch')
  if r['action_type']=='login_failed':
   if r.get('status')!='failed' or uid is None and r.get('username')!='public':raise ValueError('invalid_failed_login_audit')
  elif uid is None or r.get('status')!='success':raise ValueError('invalid_success_auth_audit')
  if r['action_type']!='login_failed' and r.get('record_id') not in (None,''):raise ValueError('audit_record_invalid')
 sequences={}
 if 'sqlite_sequence' in bt:
  sb={r['name']:r['seq'] for r in b.execute('SELECT name,seq FROM sqlite_sequence')};sc={r['name']:r['seq'] for r in c.execute('SELECT name,seq FROM sqlite_sequence')}
  if {k:v for k,v in sb.items() if k!='audit_logs'}!={k:v for k,v in sc.items() if k!='audit_logs'}:raise ValueError('non_auth_sequence_changed')
  expected=dict(sb)
  if new and 'AUTOINCREMENT' in next(r[3] for r in _schema(b) if r[0]=='table' and r[1]=='audit_logs').upper():expected['audit_logs']=max(maximum,max(r['id'] for r in new))
  if sc!=expected:raise ValueError('auth_sequence_invalid')
  sequences=expected
 auth={t:table_projection(c,t) for t in AUTH_TABLES}
 return {'status':'PASS','auth':auth,'protected_business':protected,'auth_audit_appended':len(new),'approved_extra_tables':extra,'expected_sequences':sequences},sessions,users_c,new

def validate_auth_delta(backup,current,*,expected_extra_tables=None):
 # Live read-only transaction includes committed WAL. SQLite may access SHM
 # operational metadata; no business SQL is executed and no checkpoint is run.
 with closing(_read(backup)) as b,closing(_read(current,live=True)) as c:return _validate(b,c,expected_extra_tables or {})[0]

def prepare_auth_preserving_restore(backup,current,destination,*,expected_extra_tables=None):
 destination=_path(destination);root=Path('/Volumes/Zhang/AAIE CodeX/CRM')
 if not destination.is_relative_to(root) or destination in (_path(backup),_path(current)):raise ValueError('destination_outside_fresh_workspace')
 if destination.exists():raise FileExistsError(destination)
 with closing(_read(backup)) as b,closing(_read(current)) as c:
  receipt,sessions,users,audits=_validate(b,c,expected_extra_tables or {})
  # Reserve without replacement; failed output is retained for diagnosis.
  fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
  with closing(sqlite3.connect(destination)) as d:
   d.row_factory=sqlite3.Row;b.backup(d);d.execute('PRAGMA foreign_keys=ON')
   with d:
    d.execute('DELETE FROM sessions')
    for row in sessions:
     cols=list(row);d.execute('INSERT INTO sessions('+','.join(map(_quote,cols))+') VALUES('+','.join('?' for _ in cols)+')',tuple(row.values()))
    for uid,row in users.items():d.execute('UPDATE users SET last_login_at=?,last_login_ip=? WHERE id=?',(row['last_login_at'],row['last_login_ip'],uid))
    for row in audits:
     cols=list(row);d.execute('INSERT INTO audit_logs('+','.join(map(_quote,cols))+') VALUES('+','.join('?' for _ in cols)+')',tuple(row.values()))
   _integrity(d)
   if 'sqlite_sequence' in _tables(d) and {r['name']:r['seq'] for r in d.execute('SELECT name,seq FROM sqlite_sequence')}!=receipt['expected_sequences']:raise ValueError('restore_sequence_mismatch')
   if _schema(d)!=_schema(b):raise ValueError('restore_schema_changed')
   if any(table_projection(d,t)!=v for t,v in receipt['protected_business'].items()):raise ValueError('restore_business_changed')
   if any(table_projection(d,t)!=v for t,v in receipt['auth'].items()):raise ValueError('restore_auth_mismatch')
   if 'schema_migrations' in _tables(b) and table_projection(d,'schema_migrations')!=table_projection(b,'schema_migrations'):raise ValueError('restore_registry_changed')
  return {**receipt,'restore_schema_exact':True,'restore_registry_exact':True,'destination_sha256':hashlib.sha256(destination.read_bytes()).hexdigest()}
