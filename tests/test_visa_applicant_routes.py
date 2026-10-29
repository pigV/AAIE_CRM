import sqlite3
from contextlib import closing
import unittest
from visa_workspace_candidate import VisaWorkspaceCandidateMixin

class Base:
    def send_html(self,content,*args,**kwargs):return content

class Harness(VisaWorkspaceCandidateMixin,Base):
    def _visa_allowed(self):return True
    def visa_material_case_or_404(self,sid,cid):return {'id':int(cid),'student_id':None,'applicant_id':7}
    def error_page(self,message,status):return ('error',status)
    def __getattr__(self,name):
        if name.startswith('student_visa_case_'):return lambda *args:(name,args)
        raise AttributeError(name)

class ApplicantRouteTests(unittest.TestCase):
    def test_child_html_rewrites_only_this_case_url_attributes(self):
        h=Harness();h._visa_independent_case_id=9
        source='''<form action="/students//visa-cases/9/information-collection/respondents/2/assignments/3/edit"></form><a href='/students//visa-cases/9?visa_tab=info_collection#x'>Back</a><a href="/students//visa-cases/90/evidence">Other</a>literal /students//visa-cases/9/evidence'''
        result=h.send_html(source)
        self.assertIn('action="/visa-management/cases/9/applicant/information-collection/respondents/2/assignments/3/edit"',result)
        self.assertIn("href='/visa-management/cases/9/workspace?visa_tab=info_collection#x'",result)
        self.assertIn('/students//visa-cases/90/evidence',result)
        self.assertIn('literal /students//visa-cases/9/evidence',result)
    def test_student_html_is_unchanged(self):
        h=Harness();source='<a href="/students/123/visa-cases/9">Student</a>'
        self.assertEqual(h.send_html(source),source)
    def test_visible_token_and_download_archive_aliases_dispatch(self):
        h=Harness()
        pairs=[('information-collection/respondents/2/expire','student_visa_case_information_respondent_expire'),('information-collection/respondents/2/regenerate','student_visa_case_information_respondent_regenerate'),('information-collection/respondents/2/reset-wechat','student_visa_case_information_respondent_reset_wechat'),('information-collection/respondents/2/assignments/3/download','student_visa_case_information_assignment_download'),('evidence/financial-items/4/archive','student_visa_case_financial_item_archive'),('evidence/asset-items/4/archive','student_visa_case_asset_item_archive')]
        for tail,target in pairs:
            result=h.visa_applicant_action('POST','9',tail)
            self.assertEqual(result[0],target,tail)
            self.assertEqual(result[1][1:3],('', '9'))
    def test_standalone_application_type_persists_without_student_identity(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.execute('CREATE TABLE visa_cases(id INTEGER PRIMARY KEY,student_id,applicant_id,application_type,updated_at)')
            conn.execute("INSERT INTO visa_cases VALUES(9,NULL,7,'New Application','before')")
            class DB:
                def execute(self,sql,params):return conn.execute(sql,params)
            h=Harness();h.form={'application_type':'Renewal Application'}
            h._visa_runtime=lambda:{'db':DB(),'now_iso':lambda:'now','form_text_value':lambda form,key:form.get(key,'')}
            h.redirect=lambda location:location
            h.visa_applicant_action('POST','9','information-collection/application-type')
            self.assertEqual(conn.execute('SELECT application_type FROM visa_cases WHERE id=9').fetchone()[0],'Renewal Application')
            self.assertIsNone(conn.execute('SELECT student_id FROM visa_cases WHERE id=9').fetchone()[0])
    def test_permission_gate_precedes_alias_dispatch(self):
        h=Harness();h._visa_allowed=lambda:False
        self.assertIsNone(h.visa_applicant_action('POST','9','information-collection/respondents/2/expire'))
    def test_unknown_alias_denied(self):self.assertEqual(Harness().visa_applicant_action('POST','9','delete-student'),('error',404))

if __name__=='__main__':unittest.main()
