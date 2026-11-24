import json,sqlite3,unittest
from unittest.mock import patch
from pathlib import Path
from student_case_feature_flags import StudentCaseFeatureFlags
import student_transfer_summary as summaries

class ReviewedHistoricalTransferTests(unittest.TestCase):
 def setUp(self):
  self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
  self.c.executescript('''
   CREATE TABLE users(id INTEGER PRIMARY KEY,is_active,role_id);
   CREATE TABLE roles(id INTEGER PRIMARY KEY,name);
   INSERT INTO roles VALUES(1,'Admin'),(2,'View Only'); INSERT INTO users VALUES(1,1,1),(2,1,2);
   CREATE TABLE students(student_id PRIMARY KEY,deleted_at);
   INSERT INTO students VALUES('S',NULL),('OTHER',NULL);
   CREATE TABLE application_cycles(id PRIMARY KEY,student_id,cycle_type,status,outcome,sequence_no,source_student_case_id);
   INSERT INTO application_cycles VALUES(10,'S','initial','closed','completed',1,NULL);
   CREATE TABLE offers(id PRIMARY KEY,student_id,application_cycle_id,school_name,offer_status,offer_file_path);
   INSERT INTO offers VALUES(20,'S',10,'Target School','accepted','offer.pdf');
   CREATE TABLE coe_oshc(id PRIMARY KEY,student_id,application_cycle_id,school_name,document_kind,file_path);
   INSERT INTO coe_oshc VALUES(30,'S',10,'Target School','LetterOfConfirmation','letter.pdf');
   CREATE TABLE student_documents(id PRIMARY KEY,student_id,file_path,is_active,document_status,voided_at);
   INSERT INTO student_documents VALUES(40,'S','offer.pdf',1,'active',NULL),(41,'S','letter.pdf',1,'active',NULL);
   CREATE TABLE audit_logs(id INTEGER PRIMARY KEY,user_id,action_type,module_name,module_key,record_id,student_id,old_value,new_value,created_at);
  ''')
  self.c.commit()
 def confirm(self,**changes):
  from reviewed_historical_transfer import confirm_historical_transfer_ownership
  args=dict(student_id='S',cycle_id=10,offer_id=20,confirmation_id=30,actor_id=1,reason='User verified historical transfer',stamp='2026-11-24T20:00:00+11:00');args.update(changes)
  return confirm_historical_transfer_ownership(self.c,**args)
 def test_reviewed_history_has_badge_without_changing_original_business(self):
  before={t:[tuple(r) for r in self.c.execute('SELECT * FROM '+t)] for t in ['application_cycles','offers','coe_oshc','student_documents','students']}
  self.c.execute('BEGIN');self.assertTrue(self.confirm());self.c.commit()
  with patch.object(summaries,'TransferCaseService') as service,patch.object(summaries,'load_student_transfer_state_map',return_value={}):
   service.return_value.list_transfer_cases.return_value=[]
   summary=summaries.build_student_transfer_summary('S',{'permissions':['student_cases.view']},database_path=Path('unused.db'),connection=self.c,flags=StudentCaseFeatureFlags(read_enabled=True))
  self.assertEqual(summary.state,'history')
  self.assertIn('转学',summaries.render_student_transfer_badge(summary))
  html=summaries.render_student_transfer_summary(summary)
  self.assertIn('历史转学已完成',html);self.assertIn('Target School',html);self.assertIn('application_cycle_id=10',html);self.assertIn('tab=coe',html);self.assertNotIn('暂无转学记录',html);self.assertNotIn('/transfer-cases/0',html)
  for t,v in before.items():self.assertEqual(v,[tuple(r) for r in self.c.execute('SELECT * FROM '+t)])
 def test_existing_audited_closed_cycle_is_not_shown_as_no_transfer(self):
  payload={'version':1,'decision':'confirmed','cycle_id':10,'cycle_sequence':1,'offer_id':20,'confirmation_id':30,'offer_document_id':40,'confirmation_document_id':41,'target_school':'Target School','reason':'User verified historical transfer'}
  self.c.execute("INSERT INTO audit_logs(user_id,action_type,module_name,module_key,record_id,student_id,new_value,created_at) VALUES(1,'historical_transfer_ownership_confirmed','application_cycles','students','10','S',?,'2026-11-24T20:00:00+11:00')",(json.dumps(payload),));self.c.commit()
  with patch.object(summaries,'TransferCaseService') as service,patch.object(summaries,'load_student_transfer_state_map',return_value={}):
   service.return_value.list_transfer_cases.return_value=[]
   summary=summaries.build_student_transfer_summary('S',{'permissions':['student_cases.view']},database_path='unused',connection=self.c,flags=StudentCaseFeatureFlags(read_enabled=True))
  self.assertEqual('history',summary.state)
  self.assertIn('转学',summaries.render_student_transfer_badge(summary))
 def test_only_reviewed_materials_show_historical_transfer_ownership(self):
  from student_lifecycle.presentation import load_record_contexts,render_record_context
  self.c.execute('ALTER TABLE application_cycles ADD COLUMN cycle_uid')
  self.c.execute("UPDATE application_cycles SET cycle_uid='original-cycle'")
  self.c.execute("INSERT INTO coe_oshc VALUES(31,'S',10,'Original School','LetterOfConfirmation','other.pdf')")
  self.c.commit();self.c.execute('BEGIN');self.confirm();self.c.commit()
  rows=load_record_contexts(self.c,'S','coe',[30,31])
  self.assertIn('历史转学',render_record_context(rows[30]))
  self.assertEqual('initial',rows[30].lifecycle_type)
  self.assertIn('首次申请',render_record_context(rows[31]))
  docs=load_record_contexts(self.c,'S','document',[40])
  self.assertEqual(10,docs[40].cycle_id)
  self.assertIn('历史转学',render_record_context(docs[40]))
 def test_explicit_invalid_document_cycle_is_never_replaced_by_audit(self):
  from student_lifecycle.presentation import load_record_contexts,render_record_context
  self.c.execute('ALTER TABLE application_cycles ADD COLUMN cycle_uid')
  self.c.execute('ALTER TABLE student_documents ADD COLUMN application_cycle_id')
  self.c.commit();self.c.execute('BEGIN');self.confirm();self.c.commit()
  for bad_cycle in [999,11]:
   with self.subTest(cycle=bad_cycle):
    self.c.execute("INSERT OR IGNORE INTO application_cycles VALUES(11,'OTHER','initial','closed','completed',1,NULL,'other-cycle')")
    self.c.execute('UPDATE student_documents SET application_cycle_id=? WHERE id=40',(bad_cycle,))
    result=load_record_contexts(self.c,'S','document',[40])
    self.assertNotIn('历史转学',render_record_context(result[40]))
    with self.assertRaises(ValueError):self.confirm(reason='must not confirm wrong document ownership')
  self.c.rollback()
 def test_explicit_invalid_case_link_is_not_treated_as_unlinked(self):
  from student_lifecycle.presentation import load_record_contexts,render_record_context
  self.c.execute('ALTER TABLE application_cycles ADD COLUMN cycle_uid')
  self.c.executescript("CREATE TABLE student_cases(id,student_id,case_type); CREATE TABLE transfer_school_applications(id,school_name_snapshot); CREATE TABLE case_record_links(id,record_type,record_id,case_id,transfer_application_id,link_role); INSERT INTO case_record_links VALUES(1,'student_document',40,999,NULL,'historical');")
  self.c.execute('BEGIN');self.confirm();self.c.commit()
  self.assertNotIn('历史转学',render_record_context(load_record_contexts(self.c,'S','document',[40])[40]))
 def test_replay_is_zero_write_and_ordinary_students_are_not_tagged(self):
  from reviewed_historical_transfer import load_historical_transfer_ownership
  self.c.execute('BEGIN');self.confirm();self.c.commit();n=self.c.total_changes
  self.c.execute('BEGIN');self.assertFalse(self.confirm());self.c.commit();self.assertEqual(n,self.c.total_changes)
  self.assertEqual({},load_historical_transfer_ownership(self.c,['OTHER']))
 def test_invalid_or_cross_cycle_evidence_and_non_admin_are_rejected(self):
  changes=[("UPDATE application_cycles SET status='active'",{}),("UPDATE offers SET student_id='OTHER'",{}),("UPDATE coe_oshc SET application_cycle_id=99",{}),("UPDATE coe_oshc SET school_name='Different School'",{}),("UPDATE student_documents SET is_active=0 WHERE id=41",{}),("UPDATE users SET is_active=0 WHERE id=1",{}),('SELECT 1',{'actor_id':2})]
  for sql,args in changes:
   with self.subTest(sql=sql,args=args):
    self.c.execute('SAVEPOINT invalid')
    self.c.execute(sql)
    with self.assertRaises((ValueError,PermissionError)):self.confirm(**args)
    self.assertEqual(0,self.c.execute('SELECT count(*) FROM audit_logs').fetchone()[0])
    self.c.execute('ROLLBACK TO invalid');self.c.execute('RELEASE invalid')
 def test_invalidated_evidence_removes_authority_and_permissions_still_apply(self):
  from reviewed_historical_transfer import load_historical_transfer_ownership
  self.c.execute('BEGIN');self.confirm();self.c.commit()
  self.assertIn('S',load_historical_transfer_ownership(self.c,['S']))
  self.c.execute("UPDATE student_documents SET document_status='voided' WHERE id=41")
  self.assertEqual({},load_historical_transfer_ownership(self.c,['S']))
  self.assertIsNone(summaries.build_student_transfer_summary('S',{'permissions':[]},database_path='unused',connection=self.c,flags=StudentCaseFeatureFlags(read_enabled=True)))

class HistoricalTransferListRenderingTests(unittest.TestCase):
 def test_historical_badge_does_not_reclassify_original_cycle_or_change_name(self):
  import ast
  from types import SimpleNamespace
  from html import escape
  root=Path(__file__).resolve().parents[1]
  tree=ast.parse((root/'app.py').read_text());fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='student_management_application_row_html')
  ns={'student_management_actions_html':lambda *a,**k:'','LIFECYCLE_LABELS':{'initial':'首次申请'},'esc':lambda x:escape(str(x)),'student_type_badge_html':lambda s:'','display_datetime':lambda s:s,'display_date_value':lambda s:s}
  exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'student-list','exec'),ns)
  summary=SimpleNamespace(lifecycle_type='initial',stage_key='closed',stage_label='申请已结案',student_name='Example Student',final_school_name='Target School',applying_year_level='',created_at='',offer_course_start_date='',visa_grant_date='')
  student={'student_id':'S'}
  html=ns[fn.name](student,summary,'csrf',False,historical_transfer=True)
  self.assertIn('转学</span>',html);self.assertIn('Example Student',html);self.assertIn('申请已结案',html);self.assertEqual('initial',summary.lifecycle_type)
  self.assertNotIn('转学</span>',ns[fn.name](student,summary,'csrf',False))

if __name__=='__main__':unittest.main()
