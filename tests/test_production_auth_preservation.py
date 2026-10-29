import sqlite3,tempfile,unittest,hashlib
from pathlib import Path
from tools.production_auth_preservation import validate_auth_delta,prepare_auth_preserving_restore
ROOT=Path(__file__).resolve().parents[1]
class AuthPreservationTests(unittest.TestCase):
 def setUp(self):
  base=ROOT/'SAFEBOX/current-gate-20260909/write-gate/qa/auth';base.mkdir(parents=True,exist_ok=True)
  self.dir=Path(tempfile.mkdtemp(dir=base));self.b=self.dir/'backup.sqlite3';self.c=self.dir/'current.sqlite3';self.dest=self.dir/'restore.sqlite3'
  with sqlite3.connect(self.b) as c:
   c.executescript("""CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,password_hash TEXT,role_id INTEGER,last_login_at TEXT,last_login_ip TEXT); CREATE TABLE sessions(token TEXT PRIMARY KEY,user_id INTEGER REFERENCES users(id),csrf_token TEXT,expires_at TEXT,created_at TEXT,last_seen_at TEXT); CREATE TABLE audit_logs(id INTEGER PRIMARY KEY,user_id INTEGER,username TEXT,action_type TEXT,module_name TEXT,module_key TEXT,record_id TEXT,file_path TEXT,old_value TEXT,new_value TEXT,student_id TEXT,contract_id TEXT,status TEXT,error_message TEXT,created_at TEXT); CREATE TABLE business(id INTEGER PRIMARY KEY,value TEXT); CREATE TABLE schema_migrations(version TEXT PRIMARY KEY); INSERT INTO users VALUES(1,'synthetic','hash',1,NULL,NULL); INSERT INTO sessions VALUES('revoked',1,'csrf','future','past','past'); INSERT INTO audit_logs(id,action_type,module_name,module_key,created_at) VALUES(1,'update','business','business','past'); INSERT INTO business VALUES(1,'unchanged');""")
  self.c.write_bytes(self.b.read_bytes())
 def mutate(self,sql,args=()):
  with sqlite3.connect(self.c) as c:c.execute(sql,args)
 def audit(self,action='login_success',**kw):
  row=dict(id=2,user_id=1,username='synthetic',action_type=action,module_name='auth',module_key='auth',status='success',created_at='now');row.update(kw)
  self.mutate('INSERT INTO audit_logs('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
 def test_preserves_login_logout_touch_and_failed_login(self):
  self.mutate("DELETE FROM sessions");self.mutate("INSERT INTO sessions VALUES('new',1,'csrf','future','now','later')");self.mutate("UPDATE users SET last_login_at='now',last_login_ip='127.0.0.1'");self.audit();self.audit('logout',id=3);self.audit('login_failed',id=4,user_id=None,username='public',status='failed',record_id='unknown',error_message='unknown_username')
  hashes=[hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.b,self.c)];r=prepare_auth_preserving_restore(self.b,self.c,self.dest);self.assertEqual(r['status'],'PASS');self.assertEqual(hashes,[hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.b,self.c)])
  with sqlite3.connect(self.dest) as c:self.assertEqual(c.execute('SELECT token FROM sessions').fetchall(),[('new',)]);self.assertEqual(c.execute('SELECT COUNT(*) FROM audit_logs').fetchone()[0],4)
 def test_role_password_user_deletion_fail(self):
  for sql in ('UPDATE users SET role_id=2',"UPDATE users SET password_hash='tamper'",'DELETE FROM users'):
   with self.subTest(sql=sql):
    self.c.write_bytes(self.b.read_bytes());self.mutate(sql)
    with self.assertRaises(ValueError):validate_auth_delta(self.b,self.c)
 def test_old_audit_update_delete_fail(self):
  for sql in ("UPDATE audit_logs SET action_type='login_success'",'DELETE FROM audit_logs'):
   self.c.write_bytes(self.b.read_bytes());self.mutate(sql)
   with self.assertRaises(ValueError):validate_auth_delta(self.b,self.c)
 def test_non_auth_and_business_payload_fail(self):
  for kw in ({'action_type':'update'},{'module_key':'visa'},{'student_id':'1'},{'old_value':'payload'},{'file_path':'file'},{'user_id':99}):
   self.c.write_bytes(self.b.read_bytes());self.audit(**kw)
   with self.assertRaises(ValueError):validate_auth_delta(self.b,self.c)
 def test_business_delta_and_unknown_table_fail(self):
  for sql in ("UPDATE business SET value='tamper'",'CREATE TABLE surprise(id INTEGER)'):
   self.c.write_bytes(self.b.read_bytes());self.mutate(sql)
   with self.assertRaises(ValueError):validate_auth_delta(self.b,self.c)
 def test_existing_destination_never_overwritten(self):
  self.dest.write_bytes(b'sentinel')
  with self.assertRaises(FileExistsError):prepare_auth_preserving_restore(self.b,self.c,self.dest)
  self.assertEqual(self.dest.read_bytes(),b'sentinel')
 def test_foreign_session_user_rejected(self):
  self.mutate('UPDATE sessions SET user_id=99')
  with self.assertRaises(ValueError):validate_auth_delta(self.b,self.c)
 def test_extra_table_needs_exact_sealed_projection(self):
  from tools.production_auth_preservation import table_projection
  self.mutate('CREATE TABLE visa_document_metadata(id INTEGER PRIMARY KEY)')
  with self.assertRaises(ValueError):validate_auth_delta(self.b,self.c)
  with sqlite3.connect(self.c) as c:
   c.row_factory=sqlite3.Row;expected={'visa_document_metadata':table_projection(c,'visa_document_metadata')}
  self.assertEqual(prepare_auth_preserving_restore(self.b,self.c,self.dest,expected_extra_tables=expected)['status'],'PASS')
  with sqlite3.connect(self.dest) as c:self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE name='visa_document_metadata'").fetchone())
  self.mutate('INSERT INTO visa_document_metadata VALUES(1)')
  with self.assertRaises(ValueError):validate_auth_delta(self.b,self.c,expected_extra_tables=expected)
 def test_039_result_tables_are_allowed_only_with_exact_sealed_projection(self):
  from tools.production_auth_preservation import table_projection
  for table in ('visa_result_status_events','visa_result_status_current'):
   self.mutate('CREATE TABLE '+table+'(id INTEGER PRIMARY KEY)')
  with sqlite3.connect(self.c) as c:
   expected={table:table_projection(c,table) for table in ('visa_result_status_events','visa_result_status_current')}
  self.assertEqual(validate_auth_delta(self.b,self.c,expected_extra_tables=expected)['status'],'PASS')
  self.mutate('INSERT INTO visa_result_status_events VALUES(1)')
  with self.assertRaises(ValueError):validate_auth_delta(self.b,self.c,expected_extra_tables=expected)
 def test_partial_destination_is_retained_on_validation_failure(self):
  from unittest.mock import patch
  from tools import production_auth_preservation as m
  real=m._integrity;calls=[]
  def fail_final(c):
   calls.append(1)
   if len(calls)==3:raise ValueError('synthetic final verification failure')
   return real(c)
  before=self.b.read_bytes(),self.c.read_bytes()
  with patch.object(m,'_integrity',side_effect=fail_final):
   with self.assertRaises(ValueError):prepare_auth_preserving_restore(self.b,self.c,self.dest)
  self.assertTrue(self.dest.exists());self.assertEqual(before,(self.b.read_bytes(),self.c.read_bytes()))
 def test_nonempty_wal_fails_without_touching_inputs(self):
  wal=Path(str(self.c)+'-wal');wal.write_bytes(b'uncheckpointed')
  with self.assertRaises(ValueError):prepare_auth_preserving_restore(self.b,self.c,self.dest)
  self.assertEqual(wal.read_bytes(),b'uncheckpointed')
 def test_auth_success_requires_matching_existing_identity(self):
  for kw in ({'user_id':None},{'username':'different'},{'status':'failed'}):
   self.c.write_bytes(self.b.read_bytes());self.audit(**kw)
   with self.assertRaises(ValueError):validate_auth_delta(self.b,self.c)
 def test_projection_accepts_standard_sqlite_connection(self):
  from tools.production_auth_preservation import table_projection
  with sqlite3.connect(self.c) as c:self.assertEqual(table_projection(c,'users')['count'],1)
 def test_schema_trigger_changes_fail_closed(self):
  self.mutate("CREATE TRIGGER injected AFTER INSERT ON sessions BEGIN UPDATE business SET value='changed'; END")
  with self.assertRaises(ValueError):validate_auth_delta(self.b,self.c)
 def test_new_audit_id_must_exceed_backup_maximum(self):
  self.audit(id=0)
  with self.assertRaises(ValueError):validate_auth_delta(self.b,self.c)
 def test_exact_source_auth_table_shapes_work(self):
  import re
  source=(ROOT/'database.py').read_text()
  with sqlite3.connect(self.b) as c:
   for t in ('sessions','audit_logs','users'):c.execute('DROP TABLE '+t)
   c.execute('CREATE TABLE roles(id INTEGER PRIMARY KEY)');c.execute('INSERT INTO roles VALUES(1)')
   for t in ('users','sessions','audit_logs'):
    sql=re.search(r'CREATE TABLE IF NOT EXISTS '+t+r' \(.*?\n\);',source,re.S).group();c.execute(sql)
   c.execute("INSERT INTO users(id,username,display_name,password_hash,role_id,created_at) VALUES(1,'synthetic','Synthetic','hash',1,'past')")
  self.c.write_bytes(self.b.read_bytes());self.audit();self.assertEqual(prepare_auth_preserving_restore(self.b,self.c,self.dest)['status'],'PASS')
 def test_live_validation_reads_committed_wal_auth_and_rejects_business(self):
  c=sqlite3.connect(self.c)
  try:
   c.execute('PRAGMA journal_mode=WAL');c.execute("UPDATE users SET last_login_at='wal-now'");c.commit()
   self.assertGreater(Path(str(self.c)+'-wal').stat().st_size,0)
   receipt=validate_auth_delta(self.b,self.c);self.assertEqual(receipt['status'],'PASS')
   with sqlite3.connect(self.c) as read:
    from tools.production_auth_preservation import table_projection
    self.assertEqual(receipt['auth']['users'],table_projection(read,'users'))
   c.execute("UPDATE business SET value='wal-business'");c.commit()
   with self.assertRaises(ValueError):validate_auth_delta(self.b,self.c)
  finally:c.close()
 def test_destination_authority_is_crm_workspace_not_module_checkout(self):
  from unittest.mock import patch
  from tools import production_auth_preservation as m
  with patch.object(m,'__file__',str(self.dir/'different-checkout/tools/module.py')):
   self.assertEqual(prepare_auth_preserving_restore(self.b,self.c,self.dest)['status'],'PASS')
  with self.assertRaises(ValueError):prepare_auth_preserving_restore(self.b,self.c,Path('/outside-approved-crm/restore.sqlite3'))
 def test_non_auth_sequence_change_rejected(self):
  with sqlite3.connect(self.b) as c:c.executescript('CREATE TABLE numbered(id INTEGER PRIMARY KEY AUTOINCREMENT); INSERT INTO numbered DEFAULT VALUES;')
  self.c.write_bytes(self.b.read_bytes());self.mutate("UPDATE sqlite_sequence SET seq=90 WHERE name='numbered'")
  with self.assertRaises(ValueError):validate_auth_delta(self.b,self.c)
 def test_audit_sequence_only_advances_to_current_maximum(self):
  with sqlite3.connect(self.b) as c:
   sql=c.execute("SELECT sql FROM sqlite_master WHERE name='audit_logs'").fetchone()[0].replace('INTEGER PRIMARY KEY','INTEGER PRIMARY KEY AUTOINCREMENT')
   c.execute('DROP TABLE audit_logs');c.execute(sql)
   c.execute("INSERT INTO audit_logs(id,action_type,module_name,module_key,created_at) VALUES(1,'update','business','business','past')")
  self.c.write_bytes(self.b.read_bytes());self.audit()
  self.assertEqual(prepare_auth_preserving_restore(self.b,self.c,self.dest)['status'],'PASS')
  self.mutate("UPDATE sqlite_sequence SET seq=99 WHERE name='audit_logs'")
  with self.assertRaises(ValueError):validate_auth_delta(self.b,self.c)
