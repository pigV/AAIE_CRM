from contextlib import closing,redirect_stdout
import io
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from business_write_gate import Gate,MaintenanceError
from tools import system_snapshot_manifest as snapshot
ROOT=Path(__file__).resolve().parents[1]
try:
 from tools import release_snapshot_admission as admission
except ImportError:admission=None

class SnapshotAdmissionTests(unittest.TestCase):
 def setUp(self):
  self.assertIsNotNone(admission,'snapshot admission missing')
  base=ROOT/'SAFEBOX/current-gate-20260909/snapshot-admission/tmp';base.mkdir(parents=True,exist_ok=True)
  self.temp=tempfile.TemporaryDirectory(dir=base);self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.db=self.root/'db.sqlite';self.target=self.root/'output/copy.sqlite'
  with closing(sqlite3.connect(self.db)) as c:c.execute('CREATE TABLE synthetic(value)');c.execute('INSERT INTO synthetic VALUES(1)');c.commit()
  self.gate=Gate(self.db,self.root/'Library/Application Support/AAIE CRM/production-services/business-write-gate')
  self.gate.directory.parent.mkdir(parents=True)
  self.a=patch.object(admission,'FORMAL_DATABASE',self.db);self.a.start();self.addCleanup(self.a.stop)
  self.b=patch.object(admission.Path,'home',return_value=self.root);self.b.start();self.addCleanup(self.b.stop)
 def create(self):snapshot.sqlite_online_backup(self.db,self.target);return {'snapshot_path':str(self.target)}
 def test_open_backup_is_real_and_closed_before_scope_exit(self):
  self.gate.initialize_open();conns=[];native=sqlite3.connect
  def connect(*a,**kw):c=native(*a,**kw);conns.append(c);return c
  with patch.object(snapshot.sqlite3,'connect',side_effect=connect):result=admission.run_snapshot(self.db,self.create)
  self.assertTrue(self.target.exists());self.assertIn('snapshot_path',result)
  for c in conns:
   with self.assertRaises(sqlite3.ProgrammingError):c.execute('SELECT 1')
 def test_closed_and_unknown_skip_before_any_output(self):
  for state in ('unknown','closed'):
   if state=='closed':self.gate.initialize_open();self.gate.enable()
   result=admission.run_snapshot(self.db,self.create)
   self.assertTrue(result['was_skipped']);self.assertEqual(result['skip_reason'],'release_read_only');self.assertFalse(self.target.parent.exists())
 def test_late_call_after_close_is_not_queued(self):
  self.gate.initialize_open();self.gate.enable()
  self.assertTrue(admission.run_snapshot(self.db,self.create)['was_skipped'])
  s=self.gate.status();self.gate.disable(expected_generation=s['generation'],release_verified=True)
  self.assertFalse(self.target.exists())
 def test_other_database_unaffected(self):
  result=admission.run_snapshot(self.root/'other.sqlite',lambda:{'ordinary':True});self.assertEqual(result,{'ordinary':True})
 def test_both_main_entrypoints_skip_before_create(self):
  from tools import daily_system_snapshot as daily,hourly_system_snapshot as hourly
  for mod,name in ((daily,'create_daily_snapshot'),(hourly,'create_hourly_snapshot')):
   with patch.object(mod,name,side_effect=AssertionError('must not create')),redirect_stdout(io.StringIO()) as out:
    self.assertEqual(mod.main(['--root',str(self.root),'--db-path',str(self.db)]),0)
   self.assertIn('SKIPPED=release_read_only',out.getvalue())
 def test_direct_script_imports(self):
  for name in ('daily_system_snapshot.py','hourly_system_snapshot.py'):
   p=subprocess.run([sys.executable,'-B',str(ROOT/'tools'/name),'--help'],capture_output=True,text=True,cwd=self.root)
   self.assertEqual(p.returncode,0,p.stderr)
 def test_snapshot_scope_blocks_enable_until_all_backup_steps_finish(self):
  import threading
  self.gate.initialize_open();observed=[]
  def create():
   def enable():
    try:self.gate.enable(timeout=.04)
    except MaintenanceError:observed.append('drained_wait')
   t=threading.Thread(target=enable);t.start();t.join()
   self.create();return {'snapshot_path':str(self.target)}
  admission.run_snapshot(self.db,create)
  self.assertEqual(observed,['drained_wait']);self.assertTrue(self.target.exists())
  self.assertEqual(self.gate.enable()['state'],'CLOSED')
 def test_target_finalize_and_check_connections_close(self):
  self.create();native=sqlite3.connect;conns=[]
  def connect(*a,**kw):c=native(*a,**kw);conns.append(c);return c
  with patch.object(snapshot.sqlite3,'connect',side_effect=connect):
   snapshot.finalize_sqlite_backup(self.target);self.assertEqual(snapshot.sqlite_quick_check(self.target),'ok')
  for c in conns:
   with self.assertRaises(sqlite3.ProgrammingError):c.execute('SELECT 1')
