"""Real isolated SQLite regression for an explicit, truthful migration prefix."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import migration_runner as m
from tools import run_migrations as cli
ROOT=Path(__file__).resolve().parents[1]

class MigrationThroughTests(unittest.TestCase):
 def setUp(self):
  base=ROOT/'SAFEBOX/visa-production-20260909/migration-boundary/unit-fixtures';base.mkdir(parents=True,exist_ok=True)
  self.temp=tempfile.TemporaryDirectory(dir=base);self.root=Path(self.temp.name);self.db=self.root/'synthetic.sqlite3';self.pack=self.root/'packages';(self.pack/'checks').mkdir(parents=True)
  with sqlite3.connect(self.db) as c:c.execute('CREATE TABLE synthetic_business(id INTEGER PRIMARY KEY,value TEXT)');c.execute("INSERT INTO synthetic_business VALUES(1,'preserve fixture')")
  self.package('20260907_035','baseline','baseline_marker')
  m.apply_migrations(self.db,self.pack,applied_by='test',source_commit='synthetic')
  self.package('20260907_037','visa_metadata','visa_metadata_marker');self.package('20260907_038','visa_dates','visa_dates_marker');self.package('20260908_036','revenue','revenue_marker')
 def tearDown(self):self.temp.cleanup()
 def package(self,version,name,table):
  (self.pack/(version+'_'+name+'.sql')).write_text('CREATE TABLE '+table+'(id INTEGER PRIMARY KEY);')
  (self.pack/'checks'/(version+'_'+name+'_forward_check.sql')).write_text("SELECT count(*)=1 FROM sqlite_master WHERE type='table' AND name='"+table+"';")
 def apply(self,**kwargs):return m.apply_migrations(self.db,self.pack,applied_by='test',source_commit='synthetic',**kwargs)
 def tables(self):
  with sqlite3.connect(self.db) as c:return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
 def test_explicit_visa_prefix_leaves_revenue_pending_and_rows_unchanged(self):
  result=self.apply(through='20260907_038');self.assertEqual(['20260907_037','20260907_038'],result['applied']);self.assertEqual(['20260908_036'],result['pending']);self.assertNotIn('revenue_marker',self.tables())
  status=m.migration_status(self.db,self.pack);self.assertEqual(['20260907_035','20260907_037','20260907_038'],[r['version'] for r in status['applied']])
  with sqlite3.connect(self.db) as c:self.assertEqual([(1,'preserve fixture')],c.execute('SELECT * FROM synthetic_business').fetchall())
 def test_same_endpoint_reapply_is_idempotent_and_future_can_later_apply(self):
  self.apply(through='20260907_038');self.assertEqual([],self.apply(through='20260907_038')['applied']);self.assertEqual(['20260908_036'],self.apply()['applied']);self.assertIn('revenue_marker',self.tables())
 def test_default_remains_all_pending(self):
  self.assertEqual(['20260907_037','20260907_038','20260908_036'],self.apply()['applied'])
 def test_plan_bounded_and_readonly(self):
  before=hashlib.sha256(self.db.read_bytes()).hexdigest();plan=m.plan_migrations(self.db,self.pack,through='20260907_038')
  self.assertEqual(['20260907_037','20260907_038'],[r['version'] for r in plan['will_apply']]);self.assertEqual(['20260908_036'],[r['version'] for r in plan['pending']]);self.assertEqual(before,hashlib.sha256(self.db.read_bytes()).hexdigest())
 def test_unknown_malformed_or_backward_boundary_rejected_without_write(self):
  self.apply(through='20260907_038')
  for endpoint in ('20260907_035','20260907_039','037','20260907_038_extra','',38,True):
   with self.subTest(endpoint=endpoint):
    before=hashlib.sha256(self.db.read_bytes()).hexdigest()
    with self.assertRaises(m.MigrationOrderError):self.apply(through=endpoint)
    self.assertEqual(before,hashlib.sha256(self.db.read_bytes()).hexdigest())
 def test_endpoint_equal_current_applied_does_not_apply_pending(self):
  result=self.apply(through='20260907_035');self.assertEqual([],result['applied']);self.assertNotIn('visa_metadata_marker',self.tables());self.assertEqual(['20260907_037','20260907_038','20260908_036'],result['pending'])
 def test_checksum_of_full_history_is_verified_before_boundary(self):
  self.apply();(self.pack/'20260908_036_revenue.sql').write_text('-- changed\nCREATE TABLE revenue_marker(id INTEGER PRIMARY KEY);')
  with self.assertRaises(m.MigrationChecksumError):self.apply(through='20260907_038')
 def test_future_package_missing_check_is_not_hidden_by_boundary(self):
  (self.pack/'checks/20260908_036_revenue_forward_check.sql').unlink()
  with self.assertRaises(m.MigrationError):self.apply(through='20260907_038')
  self.assertNotIn('visa_metadata_marker',self.tables())
 def test_required_pending_failure_prevents_endpoint_and_revenue(self):
  (self.pack/'checks/20260907_037_visa_metadata_forward_check.sql').write_text('SELECT 0;')
  with self.assertRaises(m.MigrationError):self.apply(through='20260907_038')
  self.assertNotIn('visa_metadata_marker',self.tables());self.assertNotIn('visa_dates_marker',self.tables());self.assertNotIn('revenue_marker',self.tables())
 def test_inserted_historical_gap_rejected_even_with_endpoint(self):
  self.apply();self.package('20260907_036','unauthorized_gap','gap_marker')
  with self.assertRaises(m.MigrationOrderError):self.apply(through='20260908_036')
 def test_duplicate_identity_after_endpoint_rejected(self):
  self.package('20260908_036','duplicate','duplicate_marker')
  with self.assertRaises(m.MigrationError):self.apply(through='20260907_038')
 def test_cli_plan_and_apply_support_bounded_prefix(self):
  common=['--database',str(self.db),'--migrations-dir',str(self.pack),'--through','20260907_038','--json']
  output=io.StringIO()
  with contextlib.redirect_stdout(output):code=cli.main(['plan',*common])
  self.assertEqual(0,code);self.assertEqual(['20260907_037','20260907_038'],[r['version'] for r in json.loads(output.getvalue())['will_apply']])
  output=io.StringIO()
  with contextlib.redirect_stdout(output):code=cli.main(['apply',*common,'--applied-by','test','--source-commit','synthetic'])
  self.assertEqual(0,code);self.assertEqual(['20260908_036'],json.loads(output.getvalue())['pending']);self.assertNotIn('revenue_marker',self.tables())
 def test_formal_safety_gates_are_still_required(self):
  with self.assertRaises(m.MigrationError):m.apply_migrations(self.db,self.pack,through='20260907_038',applied_by='test',source_commit='synthetic',formal_database_path=self.db)
  self.assertNotIn('visa_metadata_marker',self.tables())

class CurrentVisaSequenceTests(unittest.TestCase):
 def test_existing036_registry_and_rows_preserved_real_visa_packages_apply_once(self):
  import shutil
  base=ROOT/'SAFEBOX/visa-production-20260909/current-sequence';base.mkdir(parents=True,exist_ok=True)
  with tempfile.TemporaryDirectory(dir=base) as directory:
   directory=Path(directory);database=directory/'synthetic.sqlite3';packages=directory/'packages';(packages/'checks').mkdir(parents=True)
   sqlite3.connect(database).close()
   stem='20260908_036_synthetic_existing_baseline'
   (packages/(stem+'.sql')).write_text("CREATE TABLE users(id INTEGER PRIMARY KEY); CREATE TABLE visa_cases(id INTEGER PRIMARY KEY); CREATE TABLE visa_case_documents(id INTEGER PRIMARY KEY); CREATE TABLE existing_business(id INTEGER PRIMARY KEY,value TEXT); INSERT INTO existing_business VALUES(1,'preserve');")
   (packages/'checks'/(stem+'_forward_check.sql')).write_text("SELECT count(*)=1 FROM existing_business;")
   m.apply_migrations(database,packages,applied_by='synthetic-test',source_commit='synthetic-baseline')
   with sqlite3.connect(database) as c:before=c.execute('SELECT * FROM schema_migrations').fetchall()
   for stem in ('20260909_037_visa_document_metadata','20260909_038_visa_processing_dates'):
    shutil.copyfile(ROOT/'migrations'/(stem+'.sql'),packages/(stem+'.sql'));shutil.copyfile(ROOT/'migrations/checks'/(stem+'_forward_check.sql'),packages/'checks'/(stem+'_forward_check.sql'))
   first=m.apply_migrations(database,packages,through='20260909_038',applied_by='synthetic-test',source_commit='synthetic-candidate')
   self.assertEqual(['20260909_037','20260909_038'],first['applied']);self.assertEqual([],first['pending'])
   self.assertEqual([],m.apply_migrations(database,packages,through='20260909_038',applied_by='synthetic-test',source_commit='synthetic-candidate')['applied'])
   with sqlite3.connect(database) as c:
    self.assertEqual(before,c.execute("SELECT * FROM schema_migrations WHERE version='20260908_036'").fetchall());self.assertEqual([(1,'preserve')],c.execute('SELECT * FROM existing_business').fetchall());self.assertEqual(5,c.execute('SELECT count(*) FROM visa_document_keyword_rules').fetchone()[0]);self.assertEqual([],c.execute('PRAGMA foreign_key_check').fetchall());self.assertEqual([('ok',)],c.execute('PRAGMA integrity_check').fetchall())

if __name__=='__main__':unittest.main()
