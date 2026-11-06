"""Synthetic fixture boundary must not silently migrate past its caller's cutoff."""
from pathlib import Path
import hashlib,json,sqlite3,tempfile,unittest
from tests import test_student_case_phase2_transfer as fixture
class TransferFixtureBoundaryTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.db=self.root/'fixture.db'
  helper=fixture.TransferPhase2MigrationTests();helper.connect=fixture.TransferPhase2MigrationTests.connect;helper._create_legacy_fixture(self.db)
 def test_visa_prefix_stops_at_requested_version_and_keeps_verified_backups(self):
  fixture.apply_all_test_migrations_with_rebuild_backup(self.db,self.root,label='bounded-fixture',through='20260909_038')
  with sqlite3.connect(self.db) as c:
   versions=[r[0] for r in c.execute('SELECT version FROM schema_migrations ORDER BY version')]
   self.assertEqual(max(versions),'20260909_038');self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok');self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])
  for stage in ['035-transfer','035-visa']:
   backup=self.root/f'bounded-fixture-before-{stage}.db';receipt=json.loads(backup.with_suffix('.manifest.json').read_text());self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(),receipt['backup_sha256'])
 def test_unsupported_early_or_unknown_boundary_rejected_before_writes(self):
  before=hashlib.sha256(self.db.read_bytes()).hexdigest()
  for through in ['20260729_002','29990101_999']:
   with self.subTest(through=through):
    with self.assertRaises(ValueError):fixture.apply_all_test_migrations_with_rebuild_backup(self.db,self.root,label='invalid',through=through)
  self.assertEqual(before,hashlib.sha256(self.db.read_bytes()).hexdigest());self.assertEqual([p.name for p in self.root.iterdir()],['fixture.db'])

 def test_legacy_transfer_boundary_stops_before_v2_and_keeps_current_base_columns(self):
  fixture.apply_all_test_migrations_with_rebuild_backup(self.db,self.root,label='legacy-prefix',through='20260824_030')
  with sqlite3.connect(self.db) as c:
   self.assertEqual(c.execute('SELECT max(version) FROM schema_migrations').fetchone()[0],'20260824_030')
   self.assertNotIn('v2_version',{r[1] for r in c.execute('PRAGMA table_info(transfer_cases)')})
   self.assertIn('passport_last_name',{r[1] for r in c.execute('PRAGMA table_info(students)')})
   self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])
