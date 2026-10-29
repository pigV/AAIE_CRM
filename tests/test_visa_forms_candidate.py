"""Exercises actual Handler method bodies without importing the application/runtime."""
import ast
import copy
import datetime
import html
import json
from pathlib import Path
import re
import unittest
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
METHODS = {'visa_material_profile','visa_information_collection_default_form_types_for_case','visa_aic_500_repeat_groups','visa_aic_500_scalar_fields','visa_aic_500_sections','visa_aic_500_section_labels','visa_aic_500_date_field_names','visa_aic_500_strict_date_ok','visa_aic_500_normalize_date_value','visa_aic_500_field_value','visa_aic_500_collect_repeat_rows','visa_aic_500_admin_edit_payload','visa_aic_500_unconfirmed_sections','visa_aic_500_section_is_confirmed','visa_aic_500_payload_has_value','visa_aic_500_review_errors','visa_aic_500_admin_edit_html','visa_aic_500_admin_edit_input_html'}
def load_handler():
    tree = ast.parse((ROOT/'app.py').read_text())
    handler = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in {'CRMLegacyHandler', 'CRMHandler'} and any(isinstance(n, ast.FunctionDef) and n.name == 'visa_material_profile' for n in node.body))
    selected = [node for node in handler.body if isinstance(node,ast.FunctionDef) and node.name in METHODS]
    base = ast.ClassDef(name='Base',bases=[],keywords=[],body=selected,decorator_list=[])
    ns = dict(json=json,re=re,datetime=datetime.datetime,esc=html.escape,quote=quote,form_text_value=lambda f,k: (f.get(k,[''])[0] if isinstance(f.get(k),list) else f.get(k,'')))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[base],type_ignores=[])),'app.py','exec'),ns)
    try:
        from visa_forms_candidate import VisaFormsCandidateMixin
    except ImportError:
        VisaFormsCandidateMixin = type('MissingCandidate',(),{})
    return type('Candidate',(VisaFormsCandidateMixin,ns['Base']),{})()

class FormsCandidateTests(unittest.TestCase):
    def setUp(self): self.h=load_handler()
    def test_500_guardian_metadata_cannot_select_590_default_forms(self):
        case={'visa_type':'Student Visa (500)','case_type':'Student','visa_subclass':'500','application_type':'Relative + 590','guardian_applicant':'Mother guardian'}
        self.assertEqual(self.h.visa_material_profile(case),'student')
        self.assertIn('genuine_student_statement_raw',self.h.visa_information_collection_default_form_types_for_case(case))
        self.assertNotIn('gte_statement_guardian_raw',self.h.visa_information_collection_default_form_types_for_case(case))
    def test_actual_590_still_receives_guardian_forms(self):
        case={'visa_type':'Student Guardian Visa (590)','case_type':'Guardian','visa_subclass':'590'}
        self.assertIn('gte_statement_guardian_raw',self.h.visa_information_collection_default_form_types_for_case(case))
    def test_staff_delete_final_row_does_not_resurrect_existing_row(self):
        p={'sections':{'travel':{'travel_history':[{'country':'China'}]}}}
        result=self.h.visa_aic_500_admin_edit_payload(p,{'travel_history__present':['1']})
        self.assertEqual(result['sections']['travel']['travel_history'],[])
        self.assertEqual(p['sections']['travel']['travel_history'][0]['country'],'China')
    def test_staff_add_edit_and_delete_repeat_rows(self):
        p={'sections':{'study_work':{'study_work_history':[{'industry':'old'}]}}}
        form={'study_work_history__present':['1'],'study_work_history__0__industry':['new'],'study_work_history__1__industry':['added'],'study_work_history__0__delete':['1']}
        out=self.h.visa_aic_500_admin_edit_payload(p,form)
        self.assertEqual([r['industry'] for r in out['sections']['study_work']['study_work_history']],['added'])
    def test_date_validation_identifies_section_row_field_reason(self):
        payload={'sections':{'travel':{'travel_history':[{'from_date':'31/02/2025'}]}},'section_confirmed':{k:True for k,_,_ in self.h.visa_aic_500_sections()}}
        errors=self.h.visa_aic_500_review_errors(payload)
        self.assertTrue(any('travel_history__0__from_date' in str(e) and 'DD/MM/YYYY' in str(e) for e in errors),errors)
    def test_staff_ui_exposes_add_delete_even_for_empty_history(self):
        self.h.csrf='safe';self.h.visa_form_history_html=lambda assignment_id:''
        self.h.visa_information_collection_status_label=lambda value:value
        self.h.visa_information_collection_respondent_type_label=lambda value:value
        self.h.visa_case_return_path=lambda *args:'/case'
        out=self.h.visa_aic_500_admin_edit_html('S1',3,{'id':2,'respondent_name':'Test','respondent_type':'student'},{'id':1,'status':'Submitted'},{'sections':{}})
        self.assertIn('src="/assets/visa-forms-candidate.js"',out)
        self.assertNotIn('<script>document.querySelectorAll',out)
        self.assertEqual(out.count('data-add-row>'),2)
        self.assertIn('travel_history__present',out)
        self.assertIn('travel_history__INDEX__delete',out)
        self.assertIn('study_work_history__INDEX__industry',out)
    def test_error_link_targets_exact_field(self):
        self.assertTrue(hasattr(self.h,'visa_form_validation_summary_html'))
        out=self.h.visa_form_validation_summary_html('tok',[{'section':'travel','field':'travel_history__0__from_date','label':'From','reason':'Invalid date','section_label':'Travel'}])
        self.assertIn('focus=travel_history__0__from_date',out)
        self.assertIn('role="alert"',out)


class DraftAuditTests(unittest.TestCase):
    def setUp(self):
        import sqlite3
        from contextlib import contextmanager
        self.conn=sqlite3.connect(':memory:');self.conn.row_factory=sqlite3.Row
        self.conn.executescript('''CREATE TABLE visa_form_assignments(id INTEGER PRIMARY KEY,respondent_id,visa_case_id,status,progress_percent,last_saved_at,submitted_at,updated_at);
        CREATE TABLE visa_form_drafts(assignment_id INTEGER PRIMARY KEY,respondent_id,visa_case_id,draft_json,status,progress_percent,last_saved_at,submitted_at,created_at,updated_at);
        CREATE TABLE visa_form_submission_originals(assignment_id INTEGER PRIMARY KEY,respondent_id,visa_case_id,original_json,captured_at,source);
        CREATE TABLE visa_form_change_audit(id INTEGER PRIMARY KEY,assignment_id,visa_case_id,actor_id,action,before_json,after_json,created_at);
        INSERT INTO visa_form_assignments VALUES(1,2,3,'Draft',0,NULL,NULL,NULL);''')
        conn=self.conn
        class DB:
            def one(self,s,p=()): return conn.execute(s,p).fetchone()
            def all(self,s,p=()): return conn.execute(s,p).fetchall()
            @contextmanager
            def transaction(self):
                with conn: yield conn
        from visa_forms_candidate import VisaFormsCandidateMixin
        class Base:
            def visa_aic_500_upsert_draft(self,*a,**k): pass
            def visa_aic_500_progress(self,p): return 20
            def visa_information_collection_update_respondent_summary(self,r): pass
        class Handler(VisaFormsCandidateMixin,Base):
            user={'id':99}
            def visa_forms_runtime(self): return {'db':DB(),'now_iso':lambda:'2026-09-07T10:00:00','can_access_module':lambda u,m:True}
        self.h=Handler();self.respondent={'id':2,'visa_case_id':3}
    def tearDown(self): self.conn.close()
    def assignment(self): return self.conn.execute('SELECT * FROM visa_form_assignments WHERE id=1').fetchone()
    def test_student_original_survives_staff_edits_with_audit(self):
        original={'sections':{'travel':{'travel_history':[{'country':'China'}]}}}
        self.h.visa_aic_500_upsert_draft(self.assignment(),self.respondent,original,'Submitted',True)
        self.h._visa_staff_edit=True
        effective={'sections':{'travel':{'travel_history':[{'country':'Australia'}]}}}
        self.h.visa_aic_500_upsert_draft(self.assignment(),self.respondent,effective,'Submitted')
        row=self.conn.execute('SELECT * FROM visa_form_submission_originals').fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row['original_json']),original)
        self.assertEqual(json.loads(self.conn.execute('SELECT draft_json FROM visa_form_drafts').fetchone()[0]),effective)
        audit=self.conn.execute("SELECT * FROM visa_form_change_audit WHERE action='staff_edit'").fetchone()
        self.assertEqual(audit['actor_id'],99)
        self.assertEqual(json.loads(audit['before_json']),original)
        self.assertEqual(json.loads(audit['after_json']),effective)
    def test_failed_audit_rolls_back_entire_staff_change(self):
        original={'sections':{'travel':{'travel_history':[]}}}
        self.h.visa_aic_500_upsert_draft(self.assignment(),self.respondent,original,'Submitted',True)
        self.conn.execute("CREATE TRIGGER reject_staff BEFORE INSERT ON visa_form_change_audit WHEN NEW.action='staff_edit' BEGIN SELECT RAISE(ABORT,'test audit unavailable'); END")
        self.h._visa_staff_edit=True
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.h.visa_aic_500_upsert_draft(self.assignment(),self.respondent,{'sections':{'travel':{}}},'Submitted')
        self.assertEqual(json.loads(self.conn.execute('SELECT draft_json FROM visa_form_drafts').fetchone()[0]),original)
    def test_legacy_baseline_is_not_mislabelled_student_original(self):
        before={'sections':{'travel':{'travel_history':[{'country':'China'}]}}}
        self.h.visa_aic_500_upsert_draft(self.assignment(),self.respondent,before,'Submitted',False)
        self.h._visa_staff_edit=True
        self.h.visa_aic_500_upsert_draft(self.assignment(),self.respondent,{'sections':{}},'Submitted')
        row=self.conn.execute('SELECT * FROM visa_form_submission_originals').fetchone()
        self.assertEqual(row['source'],'legacy_effective_baseline')
        self.assertEqual(json.loads(row['original_json']),before)
        self.assertIn('历史学生原始提交无法还原',self.h.visa_form_history_html(1))
    def test_cancel_is_scoped_idempotent_and_audited(self):
        self.conn.execute("INSERT INTO visa_form_assignments VALUES(4,5,6,'Draft',0,NULL,NULL,NULL)")
        self.h.student_visa_case_information_assignment_row=lambda *args: ({'id':3},self.respondent,self.assignment())
        self.h.visa_case_return_path=lambda *args:'/case'
        self.h.toast_redirect=lambda *args:args
        self.h.error_page=lambda *args:args
        self.h.student_visa_case_information_assignment_cancel('POST','S1',3,2,1)
        self.h.student_visa_case_information_assignment_cancel('POST','S1',3,2,1)
        self.assertEqual(self.assignment()['status'],'Cancelled')
        self.assertEqual(self.conn.execute('SELECT status FROM visa_form_assignments WHERE id=4').fetchone()[0],'Draft')
        self.assertEqual(self.conn.execute("SELECT count(*) FROM visa_form_change_audit WHERE action='cancel_assignment'").fetchone()[0],1)
    def test_cancel_rejects_get(self):
        self.h.error_page=lambda message,status:status
        self.assertEqual(self.h.student_visa_case_information_assignment_cancel('GET','S1',3,2,1),405)
        self.assertEqual(self.assignment()['status'],'Draft')
    def test_cancelled_assignment_rejects_save(self):
        self.conn.execute("UPDATE visa_form_assignments SET status='Cancelled'")
        with self.assertRaises(ValueError): self.h.visa_aic_500_upsert_draft(self.assignment(),self.respondent,{'sections':{}},'Draft')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM visa_form_drafts').fetchone()[0],0)

if __name__=='__main__': unittest.main()
