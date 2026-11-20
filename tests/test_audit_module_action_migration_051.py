"""Real runner regression for the isolated audit lookup index contract."""
from pathlib import Path
from contextlib import closing
import sqlite3,tempfile,unittest
import migration_runner as runner
ROOT=Path(__file__).resolve().parents[1]
VERSION='20261120_051'
STEM=VERSION+'_audit_module_action_index'

class AuditLookupMigrationTests(unittest.TestCase):
 def setUp(self):
  base=ROOT/'reports/preproduction-readiness-20261120/snapshot-performance/unit-fixtures';base.mkdir(parents=True,exist_ok=True)
  self.temp=tempfile.TemporaryDirectory(dir=base);self.addCleanup(self.temp.cleanup);self.db=Path(self.temp.name)/'fixture.db'
 def package(self):
  path=ROOT/'migrations'/(STEM+'.sql');self.assertTrue(path.is_file(),'051 audit lookup migration is missing');return path
 def fixture(self):
  self.package()
  with closing(sqlite3.connect(self.db)) as c:
   c.executescript("CREATE TABLE audit_logs(record_id TEXT,module_name TEXT,action_type TEXT,new_value TEXT,created_at TEXT); INSERT INTO audit_logs VALUES('11','visa_management','visa_letter_extraction_retired','{\"visa_case_id\":1}','2026-11-20'); CREATE INDEX idx_audit_created ON audit_logs(created_at DESC);")
   c.executescript((ROOT/'migrations/20261119_050_business_changes.sql').read_text());c.executescript(runner.REGISTRY_DDL)
   for m in runner.discover_migrations(ROOT/'migrations'):
    if m.version<VERSION:c.execute('INSERT INTO schema_migrations VALUES(?,?,?,?,?,?,?)',(m.version,m.name,m.checksum_sha256,'2026-11-20',0,'test','fixture'))
   c.commit()
 def apply(self):return runner.apply_migrations(self.db,ROOT/'migrations',applied_by='test-051',source_commit='candidate')
 def test_version_prefix_applies_only_051_once_without_changing_rows(self):
  self.fixture()
  with closing(sqlite3.connect(self.db)) as c:before=c.execute('SELECT * FROM audit_logs').fetchall()
  self.assertEqual([VERSION],[m['version'] for m in runner.plan_migrations(self.db,ROOT/'migrations')['will_apply']])
  self.assertEqual([VERSION],self.apply()['applied']);self.assertEqual([],self.apply()['applied'])
  with closing(sqlite3.connect(self.db)) as c:
   self.assertEqual(before,c.execute('SELECT * FROM audit_logs').fetchall())
   plan=c.execute("EXPLAIN QUERY PLAN SELECT record_id FROM audit_logs WHERE module_name='visa_management' AND action_type='visa_letter_extraction_retired' AND json_extract(new_value,'$.visa_case_id')=1").fetchall()
   self.assertTrue(any('SEARCH audit_logs USING INDEX idx_audit_module_action' in r[3] for r in plan),plan)
  self.assertFalse(runner.verify_migrations(self.db,ROOT/'migrations')['pending'])
 def test_cumulative_check_rejects_wrong_owner_unique_order_and_partial(self):
  self.fixture();self.apply()
  check=(ROOT/'migrations/checks'/(STEM+'_forward_check.sql')).read_text()
  variants=[
   'CREATE INDEX idx_audit_module_action ON audit_logs(action_type,module_name)',
   'CREATE UNIQUE INDEX idx_audit_module_action ON audit_logs(module_name,action_type)',
   "CREATE INDEX idx_audit_module_action ON audit_logs(module_name,action_type) WHERE module_name='visa_management'",
   'CREATE INDEX idx_audit_module_action ON other_audit(module_name,action_type)',
  ]
  with closing(sqlite3.connect(self.db)) as c:
   c.execute('CREATE TABLE other_audit(module_name TEXT,action_type TEXT)')
   for variant in variants:
    with self.subTest(ddl=variant):
     c.execute('DROP INDEX idx_audit_module_action');c.execute(variant);self.assertFalse(c.execute(check).fetchone()[0])
   c.execute('DROP INDEX idx_audit_module_action');c.execute('CREATE INDEX idx_audit_module_action ON audit_logs(module_name,action_type)')
   c.execute('DROP INDEX business_change_events_command_idx');self.assertFalse(c.execute(check).fetchone()[0])
 def test_forward_check_failure_rolls_back_index_and_registration(self):
  self.fixture()
  with closing(sqlite3.connect(self.db)) as c:c.execute('DROP INDEX business_change_events_command_idx');c.commit()
  with self.assertRaises(runner.MigrationForwardCheckError):self.apply()
  with closing(sqlite3.connect(self.db)) as c:
   self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='idx_audit_module_action'").fetchone());self.assertIsNone(c.execute('SELECT version FROM schema_migrations WHERE version=?',(VERSION,)).fetchone())

if __name__=='__main__':unittest.main()
