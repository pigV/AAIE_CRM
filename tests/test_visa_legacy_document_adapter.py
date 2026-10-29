import ast,hashlib,sqlite3,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class LegacyTests(unittest.TestCase):
 def setUp(self):
  base=ROOT/'SAFEBOX/visa-corrective-20260910/legacy-tests';base.mkdir(parents=True,exist_ok=True)
  self.temp=tempfile.TemporaryDirectory(dir=base);self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.path=self.root/'stored-uuid.txt';self.path.write_text('synthetic')
  self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row;self.addCleanup(self.c.close)
  self.c.executescript('''CREATE TABLE users(id INTEGER PRIMARY KEY);INSERT INTO users VALUES(1);
 CREATE TABLE visa_cases(id INTEGER PRIMARY KEY, student_id TEXT, archived_at TEXT);INSERT INTO visa_cases VALUES(1,'S1',NULL);
 CREATE TABLE student_documents(id INTEGER PRIMARY KEY,student_id TEXT,is_active INTEGER,document_status TEXT,file_path TEXT,original_name TEXT,version INTEGER,document_type TEXT);
 CREATE TABLE visa_case_document_links(visa_case_id INTEGER,student_document_id INTEGER,document_role TEXT,status TEXT);
 CREATE TABLE visa_case_documents(id INTEGER PRIMARY KEY,visa_case_id INTEGER,student_document_id INTEGER,file_path TEXT,original_name TEXT,version INTEGER,sha256 TEXT,archived_at TEXT,created_by INTEGER,created_at TEXT);
 CREATE TABLE visa_document_audit(id INTEGER PRIMARY KEY,visa_case_id INTEGER,document_id INTEGER,action TEXT,before_json TEXT,after_json TEXT,actor_user_id INTEGER,occurred_at TEXT);''')
  self.c.execute('INSERT INTO student_documents VALUES(1,\'S1\',1,\'active\',?,\'Bank Account Transaction Details_Deposit.txt\',1,\'VisaOther\')',(str(self.path),));self.c.execute("INSERT INTO visa_case_document_links VALUES(1,1,'material_item:1','Linked')")
  self.c.executescript((ROOT/'migrations/20260909_037_visa_document_metadata.sql').read_text())
 def test_explicit_register_original_name_identity_and_idempotence(self):
  from visa_legacy_document_adapter import register_linked_document,legacy_document_view
  physical_before={p.name for p in self.root.iterdir()};original=tuple(self.c.execute('SELECT * FROM student_documents').fetchone());before=self.c.total_changes
  view=legacy_document_view(self.c,1,1);self.assertEqual(view['effective_category'],'deposit');self.assertEqual(self.c.total_changes,before);self.assertFalse(view['manual_override'])
  kwargs=dict(storage_root=self.root,actor_user_id=1,occurred_at='2026-09-10T10:00:00')
  doc=register_linked_document(self.c,1,1,**kwargs);again=register_linked_document(self.c,1,1,**kwargs)
  self.assertEqual(doc['id'],again['id']);self.assertEqual(doc['original_name'],original[5]);self.assertEqual(tuple(self.c.execute('SELECT * FROM student_documents').fetchone()),original)
  self.assertEqual({p.name for p in self.root.iterdir()},physical_before)
 def test_inferred_crossstudent_removed_and_path_escape_rejected(self):
  from visa_legacy_document_adapter import register_linked_document
  kw=dict(storage_root=self.root,actor_user_id=1,occurred_at='2026-09-10T10:00:00')
  self.c.execute('DELETE FROM visa_case_document_links')
  with self.assertRaises(ValueError):register_linked_document(self.c,1,1,**kw)
  self.c.execute("INSERT INTO visa_case_document_links VALUES(1,1,'material_item:1','Linked')")
  self.c.execute("UPDATE student_documents SET student_id='S2'")
  with self.assertRaises(ValueError):register_linked_document(self.c,1,1,**kw)
 def test_normal_detail_and_file_rows_integrate_without_losing_tabs(self):
  tree=ast.parse((ROOT/'app.py').read_text());functions={n.name:n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef)}
  detail=ast.unparse(functions['student_visa_case_detail']);rows=ast.unparse(functions['visa_material_file_area_html'])
  self.assertIn('visa_processing_panel_html',detail)
  self.assertIn('visa_legacy_document_controls_html',rows)
  for tab in ['info_collection','material_checklist','visa_result','evidence_ai_review']:self.assertIn(tab,detail)

 def test_confirm_medical_updates_metric_same_transaction(self):
  import json
  from visa_legacy_document_adapter import register_linked_document
  from visa_documents_candidate import confirm_letter,propose_letter
  from visa_processing_dates import correct_processing_dates,processing_summary
  self.c.executescript((ROOT/'migrations/20260909_038_visa_processing_dates.sql').read_text())
  self.c.executescript('CREATE TABLE visa_letter_extractions(id INTEGER PRIMARY KEY,visa_case_id INTEGER,document_id INTEGER,proposed_json TEXT); CREATE TABLE visa_letter_reviews(id INTEGER PRIMARY KEY,extraction_id INTEGER,visa_case_id INTEGER,confirmed_json TEXT,confirmed_by INTEGER,confirmed_at TEXT); CREATE TABLE visa_case_confirmed_decisions(id INTEGER PRIMARY KEY,visa_case_id INTEGER);')
  kw=dict(actor_user_id=1,occurred_at='2026-09-10T10:00:00')
  d=register_linked_document(self.c,1,1,storage_root=self.root,**kw)
  values=propose_letter('Health Examination');values['medical_completed_date']='2026-09-02'
  self.c.execute('INSERT INTO visa_letter_extractions VALUES(1,1,?,?)',(d['id'],json.dumps(values)))
  correct_processing_dates(self.c,1,{'grant_sent_on':'2026-09-08'},**kw)
  confirm_letter(self.c,1,1,values=values,**kw)
  self.assertEqual(processing_summary(self.c,1)['medical_completed_to_grant_days'],6)
  reviews=self.c.execute('SELECT count(*) FROM visa_letter_reviews').fetchone()[0]
  values['medical_completed_date']='2026-09-09'
  with self.assertRaises(ValueError):confirm_letter(self.c,1,1,values=values,**kw)
  self.assertEqual(self.c.execute('SELECT count(*) FROM visa_letter_reviews').fetchone()[0],reviews)
 def test_actual_legacy_row_controls_project_edit_without_global_rename(self):
  from contextlib import contextmanager
  from visa_workspace_candidate import VisaWorkspaceCandidateMixin
  from visa_legacy_document_adapter import register_linked_document
  from visa_document_metadata import edit_document_metadata
  class DB:
   def one(this,sql,args=()):return self.c.execute(sql,args).fetchone()
   @contextmanager
   def transaction(this):yield self.c
  class Handler(VisaWorkspaceCandidateMixin):
   csrf='safe'
   def _visa_runtime(this):return {'db':DB()}
  h=Handler();file=dict(self.c.execute('SELECT * FROM student_documents').fetchone(),stored_name='stored-uuid.txt')
  before=self.c.total_changes;html=h.visa_legacy_document_controls_html(1,file)
  self.assertNotIn('Filename classification preview',html);self.assertIn('Deposit',html);self.assertIn('Auto / 自动',html);self.assertIn('legacy-edit',html);self.assertEqual(before,self.c.total_changes)
  kw=dict(actor_user_id=1,occurred_at='2026-09-10T10:00:00')
  doc=register_linked_document(self.c,1,1,storage_root=self.root,**kw)
  edit_document_metadata(self.c,1,doc['id'],display_name='Staff display',category='salary',**kw)
  self.assertEqual(h.visa_legacy_document_display_name(1,file),'Staff display');self.assertIn('Manual / 人工',h.visa_legacy_document_controls_html(1,file))
  self.assertEqual(self.c.execute('SELECT original_name FROM student_documents').fetchone()[0],'Bank Account Transaction Details_Deposit.txt')
  self.assertNotIn('legacy-edit',h.visa_legacy_document_controls_html(1,dict(file,is_auto_reference=True)))
 def test_actual_default_detail_renders_processing_and_preserves_tabs(self):
  from contextlib import contextmanager
  from html import escape
  from http import HTTPStatus
  from types import SimpleNamespace
  from urllib.parse import quote
  from visa_workspace_candidate import VisaWorkspaceCandidateMixin
  self.c.executescript((ROOT/'migrations/20260909_038_visa_processing_dates.sql').read_text())
  self.c.executescript('CREATE TABLE visa_letter_extractions(id INTEGER PRIMARY KEY,visa_case_id INTEGER,document_id INTEGER,proposed_json TEXT); CREATE TABLE visa_letter_reviews(id INTEGER,extraction_id INTEGER,visa_case_id INTEGER,confirmed_json TEXT);')
  class DB:
   def one(this,sql,args=()):return self.c.execute(sql,args).fetchone()
   def all(this,sql,args=()):return self.c.execute(sql,args).fetchall()
   @contextmanager
   def transaction(this):yield self.c
  db=DB();tree=ast.parse((ROOT/'app.py').read_text());node=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='student_visa_case_detail')
  env=dict(db=db,can_access_module=lambda *a:True,HTTPStatus=HTTPStatus,quote=quote,esc=escape,page=lambda title,body,*a:body,nas=SimpleNamespace(status=lambda:None),preview_dialog_html=lambda:'')
  exec(compile(ast.Module(body=[node],type_ignores=[]),'actual-default-detail','exec'),env)
  class DetailBase:
   def student_visa_case_foundation_fields(this,case):return []
  class Handler(VisaWorkspaceCandidateMixin,DetailBase):
   csrf='safe';user={};query={}
   student_visa_case_detail=env['student_visa_case_detail']
   def _visa_runtime(this):return {'db':db}
   def _visa_read_text(this,case,document):return ''
   def student_detail_row(this,sid):return {'passport_first_name':'Synthetic','passport_last_name':'Student'}
   def visa_case_tab_link(this,*a):return a[-1]
   def visa_case_tab_panel(this,key,active,html):return html
   def send_html(this,html):this.result=html
   def __getattr__(this,name):return lambda *a,**kw:''
  h=Handler();h.student_visa_case_detail('GET','S1',1)
  for text in ['Date of Grant','Length of Stay','Medical Completed Date','Total Processing Days','Medical Completed to Grant Days','Info Collection','Material Checklist','Visa Result','Evidence / AI Review']:self.assertIn(text,h.result)
 def test_official_projection_exact_cycle_never_latest_student_or_sent_on(self):
  from visa_legacy_document_adapter import official_case_projection
  self.c.executescript("ALTER TABLE visa_cases ADD COLUMN application_cycle_id INTEGER; UPDATE visa_cases SET application_cycle_id=7; CREATE TABLE visas(id INTEGER PRIMARY KEY,student_id TEXT,visa_grant_date TEXT,visa_expiry_date TEXT,visa_conditions TEXT); INSERT INTO visas VALUES(5,'S1','2026-09-01','2029-09-01','Not a length field'); CREATE TABLE visa_decision_events(visa_case_id INTEGER,student_id TEXT,application_cycle_id INTEGER,visa_record_id INTEGER,decision_type TEXT,decision_date TEXT); INSERT INTO visa_decision_events VALUES(1,'S1',7,5,'grant','2026-09-01');")
  case=dict(self.c.execute('SELECT * FROM visa_cases').fetchone());r=official_case_projection(self.c,case)
  self.assertEqual(r['grant_date'],'2026-09-01');self.assertEqual(r['length_of_stay'],'');self.assertEqual(r['expiry_date'],'2029-09-01')
  self.c.execute('UPDATE visa_decision_events SET application_cycle_id=99')
  self.assertEqual(official_case_projection(self.c,case)['grant_date'],'')
  self.c.execute('UPDATE visa_decision_events SET application_cycle_id=7,visa_case_id=2')
  self.assertEqual(official_case_projection(self.c,case)['grant_date'],'')
 def test_retained_grant_fields_are_separate_from_processing_sent_on(self):
  from visa_legacy_document_adapter import official_case_projection
  import json
  self.c.executescript('CREATE TABLE visa_case_confirmed_decisions(visa_case_id INTEGER,decision_type TEXT,decision_date TEXT,confirmed_json TEXT);')
  self.c.execute('INSERT INTO visa_case_confirmed_decisions VALUES(1,\'grant\',\'2026-09-01\',?)',(json.dumps({'length_of_stay':'Until 1 September 2029'}),))
  r=official_case_projection(self.c,dict(self.c.execute('SELECT * FROM visa_cases').fetchone()))
  self.assertEqual(r['grant_date'],'2026-09-01');self.assertEqual(r['length_of_stay'],'Until 1 September 2029')
  self.c.execute("UPDATE visa_case_confirmed_decisions SET decision_type='refusal'")
  self.assertEqual(official_case_projection(self.c,dict(self.c.execute('SELECT * FROM visa_cases').fetchone()))['grant_date'],'')
 def test_corrupt_pdf_read_rolls_back_explicit_registration(self):
  from pypdf import PdfReader
  from visa_legacy_document_adapter import register_linked_document,read_legacy_letter_text
  from visa_documents_candidate import _atomic
  bad=self.root/'corrupt.pdf';bad.write_bytes(b'%PDF-1.7\ninvalid synthetic')
  self.c.execute('UPDATE student_documents SET file_path=?',(str(bad),));self.c.commit()
  with self.assertRaisesRegex(ValueError,'source letter cannot be read'):
   with _atomic(self.c):
    register_linked_document(self.c,1,1,storage_root=self.root,actor_user_id=1,occurred_at='2026-09-10T10:00:00')
    read_legacy_letter_text(bad,PdfReader)
  for table in ['visa_case_documents','visa_document_metadata','visa_document_audit']:self.assertEqual(self.c.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
 def test_source_read_permission_error_is_controlled_no_partial_rows(self):
  from unittest.mock import patch
  from visa_legacy_document_adapter import register_linked_document
  with patch.object(Path,'read_bytes',side_effect=PermissionError('synthetic')):
   with self.assertRaisesRegex(ValueError,'unavailable for registration'):register_linked_document(self.c,1,1,storage_root=self.root,actor_user_id=1,occurred_at='2026-09-10T10:00:00')
  self.assertEqual(self.c.execute('SELECT count(*) FROM visa_case_documents').fetchone()[0],0)
 def test_invalid_utf8_is_controlled_but_programming_errors_not_swallowed(self):
  from visa_legacy_document_adapter import read_legacy_letter_text
  self.path.write_bytes(b'\xff')
  with self.assertRaises(ValueError):read_legacy_letter_text(self.path,lambda p:None)
  pdf=self.root/'error.pdf';pdf.write_bytes(b'synthetic')
  def bug(p):raise RuntimeError('programming error')
  with self.assertRaises(RuntimeError):read_legacy_letter_text(pdf,bug)
