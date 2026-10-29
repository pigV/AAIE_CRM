"""Dedicated additive-schema integrity tests; isolated in-memory and workspace fixture databases."""
import sqlite3
import unittest
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MIG=ROOT/'migrations/20260908_036_revenue_storage_locations.sql'
CHECK=ROOT/'migrations/checks/20260908_036_revenue_storage_locations_forward_check.sql'
ROLLBACK=ROOT/'migrations/rollback/20260908_036_revenue_storage_locations_rollback.sql'
COLUMNS='operation_id document_id document_uid sequence previous_event_id old_path new_path sha256 size_bytes document_fingerprint occurred_at authority reason kind reverses_event_id verification_result manifest_sha256 source_sha256 destination_sha256 source_verification'.split()

class StorageLocationsSchemaTests(unittest.TestCase):
 def setUp(self):
  self.assertTrue(MIG.exists(),'approved additive migration is not implemented')
  self.c=sqlite3.connect(':memory:');self.addCleanup(self.c.close);self.c.row_factory=sqlite3.Row
  self.c.execute('PRAGMA foreign_keys=ON')
  self.c.executescript("CREATE TABLE revenue_documents(id INTEGER PRIMARY KEY,uid TEXT UNIQUE,storage_path TEXT,sha256 TEXT,size_bytes INTEGER); CREATE TABLE revenue_payments(id INTEGER PRIMARY KEY,evidence_document_id INTEGER); CREATE TABLE revenue_receipts(id INTEGER PRIMARY KEY,document_id INTEGER);")
  ddl=(ROOT/'migrations/20260901_034_revenue_client_payment_receipt.sql').read_text()
  for name in ('revenue_documents_financial_no_update','revenue_documents_financial_no_delete'):
   start=ddl.index('CREATE TRIGGER '+name);end=ddl.index('\nEND;',start)+len('\nEND;');self.c.executescript(ddl[start:end])
  self.c.execute("INSERT INTO revenue_documents VALUES(1,'doc1','/legacy/one',?,100)",('a'*64,));self.c.execute("INSERT INTO revenue_documents VALUES(2,'doc2','/legacy/two',?,200)",('b'*64,));self.c.execute('INSERT INTO revenue_payments VALUES(1,1)');self.c.commit()
  self.original=self.trigger_sql();self.c.executescript(MIG.read_text());self.calls=[]
 def trigger_sql(self):return dict(self.c.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'revenue_documents_financial_%'"))
 def authorize(self,value=1):self.c.create_function('revenue_relocation_authorize',20,lambda *a:self.calls.append(a) or value)
 def event(self,**kw):
  d=dict(zip(COLUMNS,['op1',1,'doc1',1,None,'/legacy/one','/secure/one','a'*64,100,'c'*64,'2026-09-08T00:00:00Z','approved-relocation','approved two-file relocation','relocate',None,'PASS','d'*64,'a'*64,'a'*64,'MATCH']));d.update(kw);return d
 def insert(self,d,verb='INSERT',extra=None):
  d={**(extra or {}),**d};return self.c.execute(verb+' INTO revenue_document_locations('+','.join(d)+') VALUES('+','.join('?' for _ in d)+')',list(d.values()))
 def test_missing_udf_and_non_one_authorization_fail_closed(self):
  with self.assertRaises(sqlite3.OperationalError):self.insert(self.event())
  for result in (0,None,-1,'1'):
   self.authorize(result)
   with self.subTest(result=result),self.assertRaises(sqlite3.DatabaseError):self.insert(self.event())
  self.assertEqual(0,self.c.execute('SELECT COUNT(*) FROM revenue_document_locations').fetchone()[0])
 def test_exact_udf_order_chain_restore_and_original_document_immutable(self):
  self.authorize();first=self.insert(self.event()).lastrowid
  self.assertEqual(tuple(self.event().values()),self.calls[0])
  self.insert(self.event(operation_id='op2',sequence=2,previous_event_id=first,old_path='/secure/one',new_path='/legacy/one',kind='restore',reverses_event_id=first,source_verification='UNAVAILABLE',source_sha256=None))
  self.assertEqual('/legacy/one',self.c.execute('SELECT storage_path FROM revenue_documents WHERE id=1').fetchone()[0]);self.assertEqual(self.original,self.trigger_sql())
  with self.assertRaisesRegex(sqlite3.IntegrityError,'immutable'):self.c.execute("UPDATE revenue_documents SET storage_path='/other' WHERE id=1")
 def test_chaining_identity_fingerprint_and_hash_constraints(self):
  self.authorize()
  cases=[{'sequence':0},{'sequence':2},{'old_path':'/other'},{'document_uid':'wrong'},{'size_bytes':101},{'sha256':'b'*64},{'document_fingerprint':'short'},{'manifest_sha256':'short'},{'new_path':'/legacy/one'},{'destination_sha256':'b'*64},{'source_sha256':None},{'source_verification':'UNAVAILABLE'},{'verification_result':'FAIL'},{'kind':'unknown'},{'size_bytes':1.5},{'sequence':1.5}]
  for patch in cases:
   with self.subTest(patch=patch),self.assertRaises(sqlite3.DatabaseError):self.insert(self.event(**patch))
  first=self.insert(self.event()).lastrowid
  for patch in ({'previous_event_id':None},{'previous_event_id':999},{'old_path':'/wrong'},{'sequence':3}):
   with self.subTest(patch=patch),self.assertRaises(sqlite3.DatabaseError):self.insert({**self.event(operation_id='op2',sequence=2,previous_event_id=first,old_path='/secure/one',new_path='/secure/next'),**patch})
 def test_update_delete_replace_and_upsert_cannot_rewrite_history(self):
  self.authorize();first=self.insert(self.event()).lastrowid
  for sql in ("UPDATE revenue_document_locations SET reason='changed'",'DELETE FROM revenue_document_locations'):
   with self.assertRaises(sqlite3.IntegrityError):self.c.execute(sql)
  for patch,extra in (({},None),({'operation_id':'other'},None),({'operation_id':'other','document_id':2,'document_uid':'doc2','old_path':'/legacy/two','new_path':'/secure/two','sha256':'b'*64,'size_bytes':200,'source_sha256':'b'*64,'destination_sha256':'b'*64},{'id':first})):
   with self.assertRaises(sqlite3.IntegrityError):self.insert(self.event(**patch),'INSERT OR REPLACE',extra)
  with self.assertRaises(sqlite3.IntegrityError):
   self.c.execute('INSERT INTO revenue_document_locations('+','.join(COLUMNS)+') VALUES('+','.join('?' for _ in COLUMNS)+") ON CONFLICT(operation_id) DO UPDATE SET reason='changed'",list(self.event().values()))
  self.assertEqual(1,self.c.execute('SELECT COUNT(*) FROM revenue_document_locations').fetchone()[0])
 def test_cross_document_predecessor_and_restore_target_rejected(self):
  self.authorize();first=self.insert(self.event()).lastrowid
  for patch in ({'document_id':2,'document_uid':'doc2','sha256':'b'*64,'size_bytes':200,'source_sha256':'b'*64,'destination_sha256':'b'*64},{'kind':'restore','reverses_event_id':None},{'kind':'restore','reverses_event_id':999}):
   with self.subTest(patch=patch),self.assertRaises(sqlite3.DatabaseError):self.insert({**self.event(operation_id='op2',sequence=2,previous_event_id=first,old_path='/secure/one',new_path='/legacy/one'),**patch})
 def test_forward_check_and_empty_rollback_preserve_original_triggers(self):
  for statement in CHECK.read_text().split(';'):
   if statement.strip():self.assertEqual(1,self.c.execute(statement).fetchone()[0])
  self.c.executescript(ROLLBACK.read_text());self.assertIsNone(self.c.execute("SELECT 1 FROM sqlite_master WHERE name='revenue_document_locations'").fetchone());self.assertEqual(self.original,self.trigger_sql())
 def test_empty_rollback_holds_write_lock_between_check_and_drop(self):
  fixture_root=ROOT/'SAFEBOX/storage-relocation-20260908/schema';fixture_root.mkdir(parents=True,exist_ok=True)
  with tempfile.TemporaryDirectory(prefix='rollback-lock-',dir=fixture_root) as folder:
   db=Path(folder)/'fixture.sqlite3'
   with sqlite3.connect(db) as first,sqlite3.connect(db,timeout=0) as writer:
    self.c.backup(first);observed=[]
    def race(statement):
     if statement.strip().upper()=='DROP TABLE REVENUE_DOCUMENT_LOCATIONS;':
      try:writer.execute('BEGIN IMMEDIATE')
      except sqlite3.OperationalError as exc:observed.append(exc.sqlite_errorcode)
      else:writer.rollback();observed.append('UNLOCKED')
    first.set_trace_callback(race);first.executescript(ROLLBACK.read_text())
    self.assertEqual([sqlite3.SQLITE_BUSY],observed)
    self.assertFalse(first.in_transaction)
    self.assertIsNone(first.execute("SELECT 1 FROM sqlite_master WHERE name='revenue_document_locations'").fetchone())
 def test_nonempty_rollback_retains_events_even_after_restore(self):
  self.authorize();first=self.insert(self.event()).lastrowid;self.insert(self.event(operation_id='op2',sequence=2,previous_event_id=first,old_path='/secure/one',new_path='/legacy/one',kind='restore',reverses_event_id=first));self.c.commit()
  with self.assertRaisesRegex(sqlite3.IntegrityError,'rollback blocked'):self.c.executescript(ROLLBACK.read_text())
  self.assertEqual(2,self.c.execute('SELECT COUNT(*) FROM revenue_document_locations').fetchone()[0]);self.assertEqual(self.original,self.trigger_sql())

if __name__=='__main__':unittest.main()
