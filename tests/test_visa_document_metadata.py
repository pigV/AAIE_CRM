import importlib,json,sqlite3,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
STAMP='2026-09-08T10:00:00+10:00'
class DocumentMetadataTests(unittest.TestCase):
 def setUp(self):
  self.m=importlib.import_module('visa_document_metadata');self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
  self.c.executescript('''PRAGMA foreign_keys=ON;
 CREATE TABLE users(id INTEGER PRIMARY KEY);INSERT INTO users VALUES(1);
 CREATE TABLE visa_cases(id INTEGER PRIMARY KEY,archived_at TEXT);INSERT INTO visa_cases VALUES(1,NULL);INSERT INTO visa_cases VALUES(2,NULL);
 CREATE TABLE student_documents(id INTEGER PRIMARY KEY,document_type TEXT);
 CREATE TABLE visa_case_documents(id INTEGER PRIMARY KEY,visa_case_id INTEGER,student_document_id INTEGER,file_path TEXT,original_name TEXT,version INTEGER,sha256 TEXT,archived_at TEXT);
 INSERT INTO visa_case_documents VALUES(1,1,NULL,'/synthetic/stable.pdf','2026_Father_Bank Account Transaction Details_Deposit_01.pdf',1,'fixed-hash',NULL);
 CREATE TABLE visa_document_audit(id INTEGER PRIMARY KEY,visa_case_id INTEGER,document_id INTEGER,action TEXT,before_json TEXT,after_json TEXT,actor_user_id INTEGER,occurred_at TEXT);
 CREATE TABLE audit_logs(id INTEGER PRIMARY KEY,user_id INTEGER,action_type TEXT,module_name TEXT,record_id TEXT,old_value TEXT,new_value TEXT,created_at TEXT);''')
  self.c.executescript((ROOT/'migrations/20260909_037_visa_document_metadata.sql').read_text())
  self.kw=dict(actor_user_id=1,occurred_at=STAMP)
 def tearDown(self):
  if hasattr(self,'c'):self.c.close()
 def classify(self):return self.m.classify_document(self.c,1,1,**self.kw)
 def test_fallback_read_has_no_write(self):
  result=self.m.effective_metadata(self.c,1,1);self.assertEqual(result['display_name'],'2026_Father_Bank Account Transaction Details_Deposit_01.pdf');self.assertEqual(result['effective_category'],'other');self.assertEqual(self.c.execute('SELECT count(*) FROM visa_document_metadata').fetchone()[0],0)
 def test_five_keywords_and_prefix_suffix_case_separators(self):
  pairs=[('Bank Account Transaction Details_Deposit','deposit'),('Bank Account Transaction Details_Salary','salary'),('Certificate of Assets_Fund','financial_asset'),('Health Exam Referraletter','health_examination'),('IMMI Grant Notification','grant_letter')]
  for keyword,expected in pairs:
   for filename in [keyword+'.pdf','2026_Father_'+keyword+'_01.PDF',keyword.lower().replace(' ','-').replace('_','.')+'.txt']:
    self.c.execute('UPDATE visa_case_documents SET original_name=? WHERE id=1',(filename,));self.assertEqual(self.classify()['effective_category'],expected)
 def test_no_match(self):
  self.c.execute("UPDATE visa_case_documents SET original_name='Unknown.pdf'");self.assertEqual(self.classify()['effective_category'],'other')
 def test_manual_override_survives_reclassification(self):
  self.assertEqual(self.classify()['auto_category'],'deposit');self.m.edit_document_metadata(self.c,1,1,display_name='Salary.pdf',category='salary',**self.kw)
  result=self.classify();self.assertEqual(result['auto_category'],'deposit');self.assertEqual(result['effective_category'],'salary');self.assertTrue(result['manual_override'])
 def test_edit_preserves_physical_and_original_and_audits(self):
  before=tuple(self.c.execute('SELECT * FROM visa_case_documents').fetchone());result=self.m.edit_document_metadata(self.c,1,1,display_name='Correct display.pdf',category='salary',**self.kw);self.assertEqual(result['display_name'],'Correct display.pdf');self.assertEqual(before,tuple(self.c.execute('SELECT * FROM visa_case_documents').fetchone()))
  a=self.c.execute('SELECT * FROM visa_document_audit').fetchone();self.assertEqual(a['actor_user_id'],1);self.assertEqual(a['occurred_at'],STAMP);self.assertEqual(json.loads(a['after_json'])['effective_category'],'salary')
 def test_rename_only_preserves_override(self):
  self.m.edit_document_metadata(self.c,1,1,category='salary',**self.kw);self.assertTrue(self.m.edit_document_metadata(self.c,1,1,display_name='new.pdf',**self.kw)['manual_override'])
 def test_bad_category_name_and_crosscase_rejected(self):
  for kwargs in [{'category':'not-real'},{'display_name':'../bad.pdf'},{'display_name':'bad\r\nheader'},{'display_name':''}]:
   with self.assertRaises(ValueError):self.m.edit_document_metadata(self.c,1,1,**kwargs,**self.kw)
  with self.assertRaises(ValueError):self.m.edit_document_metadata(self.c,2,1,category='salary',**self.kw)
 def test_archive_blocks_mutation_but_keeps_display(self):
  self.m.edit_document_metadata(self.c,1,1,category='salary',**self.kw);self.c.execute("UPDATE visa_case_documents SET archived_at='now'")
  with self.assertRaises(ValueError):self.classify()
  self.assertEqual(self.m.effective_metadata(self.c,1,1)['effective_category'],'salary')
  self.c.execute('UPDATE visa_case_documents SET archived_at=NULL');self.assertEqual(self.classify()['effective_category'],'salary')
 def test_rule_priority_and_inactive(self):
  rule=self.m.save_rule(self.c,keyword='Father',category='financial_asset',priority=1,active=True,**self.kw);self.assertEqual(self.classify()['effective_category'],'financial_asset');self.m.deactivate_rule(self.c,rule['id'],**self.kw);self.assertEqual(self.classify()['effective_category'],'deposit')
 def test_rule_duplicate_normalized_conflict_rejected(self):
  with self.assertRaises(ValueError):self.m.save_rule(self.c,keyword='bank-account-transaction-details-deposit',category='salary',priority=5,active=True,**self.kw)
 def test_rule_edit_audit_and_invalid_rule(self):
  rule=self.m.list_rules(self.c)[0];updated=self.m.save_rule(self.c,rule_id=rule['id'],keyword=rule['keyword'],category='salary',priority=2,active=False,**self.kw);self.assertFalse(updated['active']);self.assertEqual(self.c.execute('SELECT count(*) FROM audit_logs').fetchone()[0],1)
  for extra in [{'keyword':'!!!'},{'priority':True},{'active':'yes'},{'category':'unknown'}]:
   values=dict(keyword='new',category='salary',priority=1,active=True);values.update(extra)
   with self.assertRaises(ValueError):self.m.save_rule(self.c,**values,**self.kw)
 def test_same_priority_longest_match_then_stable_id(self):
  self.m.save_rule(self.c,keyword='Father',category='salary',priority=100,active=True,**self.kw);self.assertEqual(self.classify()['effective_category'],'deposit')
 def test_audit_failure_rolls_back_metadata(self):
  self.c.executescript("CREATE TRIGGER deny_audit BEFORE INSERT ON visa_document_audit BEGIN SELECT RAISE(ABORT,'deny'); END;")
  with self.assertRaises(sqlite3.IntegrityError):self.m.edit_document_metadata(self.c,1,1,category='salary',**self.kw)
  self.assertEqual(self.c.execute('SELECT count(*) FROM visa_document_metadata').fetchone()[0],0)
 def test_invalid_actor_time(self):
  for values in [{'actor_user_id':99,'occurred_at':STAMP},{'actor_user_id':True,'occurred_at':STAMP},{'actor_user_id':1,'occurred_at':'bad'}]:
   with self.assertRaises(ValueError):self.m.classify_document(self.c,1,1,**values)
 def test_schema_forward_check_and_empty_rollback(self):
  self.assertEqual(self.c.execute((ROOT/'migrations/checks/20260909_037_visa_document_metadata_forward_check.sql').read_text()).fetchone()[0],1)
  self.c.executescript((ROOT/'migrations/rollback/20260909_037_visa_document_metadata_rollback.sql').read_text());self.assertIsNone(self.c.execute("SELECT name FROM sqlite_master WHERE name='visa_document_metadata'").fetchone())
 def test_rollback_refuses_business_metadata(self):
  self.classify()
  with self.assertRaises(sqlite3.IntegrityError):self.c.executescript((ROOT/'migrations/rollback/20260909_037_visa_document_metadata_rollback.sql').read_text())
 def test_no_match_preserves_linked_student_category(self):
  self.c.execute("INSERT INTO student_documents VALUES(8,'VisaGrantLetter')")
  self.c.execute("UPDATE visa_case_documents SET student_document_id=8,original_name='original.pdf'")
  self.assertEqual(self.classify()['effective_category'],'grant_letter')
  self.assertIsNone(self.classify()['auto_category'])
 def test_rollback_refuses_tampered_seed_even_without_audit(self):
  self.c.execute("UPDATE visa_document_keyword_rules SET category='salary' WHERE id=1")
  with self.assertRaises(sqlite3.IntegrityError):self.c.executescript((ROOT/'migrations/rollback/20260909_037_visa_document_metadata_rollback.sql').read_text())
 def test_submitted_snapshot_is_unchanged_by_rename_override_and_rule_edit(self):
  self.c.executescript("CREATE TABLE visa_immi_submissions(id INTEGER PRIMARY KEY,document_id INTEGER,file_path TEXT,version INTEGER,sha256 TEXT,status TEXT); INSERT INTO visa_immi_submissions VALUES(10,1,'/synthetic/stable.pdf',1,'fixed-hash','submitted');")
  document=tuple(self.c.execute('SELECT * FROM visa_case_documents').fetchone());submission=tuple(self.c.execute('SELECT * FROM visa_immi_submissions').fetchone())
  self.classify();self.m.edit_document_metadata(self.c,1,1,display_name='Reviewed Salary.pdf',category='salary',**self.kw)
  self.m.save_rule(self.c,rule_id=1,keyword='Bank Account Transaction Details_Deposit',category='financial_asset',priority=3,active=True,**self.kw)
  result=self.classify();self.assertEqual(result['auto_category'],'financial_asset');self.assertEqual(result['effective_category'],'salary')
  self.assertEqual(document,tuple(self.c.execute('SELECT * FROM visa_case_documents').fetchone()));self.assertEqual(submission,tuple(self.c.execute('SELECT * FROM visa_immi_submissions').fetchone()))
  self.assertEqual(1,self.c.execute('SELECT count(*) FROM visa_case_documents').fetchone()[0]);self.assertEqual(1,self.c.execute('SELECT count(*) FROM visa_immi_submissions').fetchone()[0])
 def test_rule_change_does_not_silently_reclassify_documents(self):
  self.classify();self.m.save_rule(self.c,rule_id=1,keyword='Bank Account Transaction Details_Deposit',category='salary',priority=3,active=True,**self.kw)
  self.assertEqual('deposit',self.m.effective_metadata(self.c,1,1)['effective_category'])
  self.assertEqual('salary',self.classify()['effective_category'])
  actions=[r[0] for r in self.c.execute('SELECT action FROM visa_document_audit ORDER BY id')];self.assertEqual(['document_classified','document_classified'],actions)
 def test_classification_audit_failure_rolls_back_proposal(self):
  self.c.executescript("CREATE TRIGGER deny_audit BEFORE INSERT ON visa_document_audit BEGIN SELECT RAISE(ABORT,'deny'); END;")
  with self.assertRaises(sqlite3.IntegrityError):self.classify()
  self.assertEqual(0,self.c.execute('SELECT count(*) FROM visa_document_metadata').fetchone()[0])
 def test_rule_audit_failure_rolls_back_rule_edit(self):
  before=dict(self.m.list_rules(self.c)[0]);self.c.executescript("CREATE TRIGGER deny_rule_audit BEFORE INSERT ON audit_logs BEGIN SELECT RAISE(ABORT,'deny'); END;")
  with self.assertRaises(sqlite3.IntegrityError):self.m.save_rule(self.c,rule_id=before['id'],keyword=before['keyword'],category='other',priority=1,active=False,**self.kw)
  self.assertEqual(before,next(r for r in self.m.list_rules(self.c) if r['id']==before['id']))
 def test_manual_other_override_survives_no_match_and_active_match(self):
  self.m.edit_document_metadata(self.c,1,1,category='other',**self.kw);self.assertEqual('other',self.classify()['effective_category']);self.m.deactivate_rule(self.c,1,**self.kw)
  result=self.classify();self.assertTrue(result['manual_override']);self.assertEqual('other',result['effective_category']);self.assertIsNone(result['auto_category'])
 def test_formal_runner_applies_metadata_once_and_preserves_seed_identity(self):
  import tempfile,shutil
  import migration_runner
  base=ROOT/'SAFEBOX/visa-production-20260909/metadata/unit-fixtures';base.mkdir(parents=True,exist_ok=True)
  with tempfile.TemporaryDirectory(dir=base) as name:
   directory=Path(name);packages=directory/'packages';(packages/'checks').mkdir(parents=True)
   stem='20260909_037_visa_document_metadata'
   shutil.copyfile(ROOT/'migrations'/(stem+'.sql'),packages/(stem+'.sql'));shutil.copyfile(ROOT/'migrations/checks'/(stem+'_forward_check.sql'),packages/'checks'/(stem+'_forward_check.sql'))
   self.c.executescript((ROOT/'migrations/rollback'/(stem+'_rollback.sql')).read_text());self.c.commit()
   target=directory/'synthetic.sqlite3';connection=sqlite3.connect(target)
   try:self.c.backup(connection)
   finally:connection.close()
   first=migration_runner.apply_migrations(target,packages,applied_by='synthetic-test',source_commit='isolated-fixture');second=migration_runner.apply_migrations(target,packages,applied_by='synthetic-test',source_commit='isolated-fixture')
   self.assertEqual(['20260909_037'],first['applied']);self.assertEqual([],second['applied'])
   connection=sqlite3.connect(target)
   try:
    self.assertEqual(5,connection.execute('SELECT count(*) FROM visa_document_keyword_rules').fetchone()[0]);self.assertEqual(1,connection.execute('SELECT count(*) FROM schema_migrations').fetchone()[0]);self.assertEqual('fixed-hash',connection.execute('SELECT sha256 FROM visa_case_documents WHERE id=1').fetchone()[0])
   finally:connection.close()
 def test_full_version_discovery_places_metadata_after_existing036_before_dates(self):
  import migration_runner
  versions=[m.version for m in migration_runner.discover_migrations(ROOT/'migrations')]
  self.assertEqual(['20260908_036','20260909_037','20260909_038','20260914_039'],versions[-4:]);self.assertEqual(len(versions),len(set(versions)))
if __name__=='__main__':unittest.main()
