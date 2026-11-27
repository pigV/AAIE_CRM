"""Exercise the actual detail renderer without app startup or production I/O."""
import ast
from html import escape
from html.parser import HTMLParser
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
import unittest
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]


class Structure(HTMLParser):
    def __init__(self, html):
        super().__init__(); self.stack=[]; self.nodes=[]; self.feed(html)
    def handle_starttag(self, tag, attrs):
        node=(tag,dict(attrs),list(self.stack)); self.nodes.append(node)
        if tag not in {'input','link','br','hr','img','meta'}: self.stack.append(node[:2])
    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0]==tag: del self.stack[i:]; break


class SchemeCLayoutTests(unittest.TestCase):
    def render(self, active='material_checklist', applicant_name=None):
        names={'student_visa_case_detail','visa_case_tab_link','visa_case_tab_panel'}
        nodes=[n for n in ast.walk(ast.parse((ROOT/'app.py').read_text())) if isinstance(n,ast.FunctionDef) and n.name in names]
        case={'id':7,'visa_type':'500 Student Visa','case_owner':'Visa Staff','status':'Preparing'}
        if applicant_name is not None: case.update(applicant_name=applicant_name, visa_type='590 Guardian Visa')
        env=dict(db=SimpleNamespace(one=lambda *args:case),can_access_module=lambda *args:True,
                 HTTPStatus=HTTPStatus,quote=quote,esc=lambda v:escape(str(v)),preview_dialog_html=lambda:'')
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'actual-detail-renderer','exec'),env)
        class Handler:
            user={}; csrf='token'; query={}
            def student_detail_row(self,sid): return {'passport_first_name':'Alex <Chen>','passport_last_name':'Test'}
            def student_visa_case_foundation_fields(self,case): return [('Visa','500')]
            def student_visa_case_foundation_field_card_html(self,*args): return '<form action="/existing-foundation"><input name="original-field"></form>'
            def visa_case_active_tab(self): return active
            def visa_case_return_path(self,sid,cid,key,anchor=''): return f'/students/{sid}/visa-cases/{cid}?visa_tab={key}#{anchor}'
            def visa_case_tab_anchor(self,key): return key
            def business_changes_enabled(self): return False
            def __getattr__(self,name): return lambda *a,**kw:'<div data-existing-workspace="'+name+'"></div>'
        for name in names: setattr(Handler,name,env[name])
        return Handler().student_visa_case_detail('GET','S1',7,fragment_only=True)

    def test_each_workspace_keeps_navigation_and_edit_forms_inside_compact_shell(self):
        for active in ('info_collection','material_checklist','visa_result','evidence_ai_review'):
            with self.subTest(active=active):
                html=self.render(active); nodes=Structure(html).nodes
                self.assertTrue(any('visa-case-scheme-c' in a.get('class','') for _,a,_ in nodes), 'detail needs scheme C shell')
                nav=next(n for n in nodes if n[0]=='nav')
                self.assertFalse(any(t=='aside' for t,a in nav[2]))
                self.assertIn('visa-case-summary', html)
                links=[a for t,a,_ in nodes if t=='a' and 'visa-case-tab-link' in a.get('class','')]
                self.assertEqual(len(links),4)
                self.assertEqual(sum('active' in a.get('class','').split() for a in links),1)
                overview=next(n for n in nodes if n[1].get('id')=='case-overview')
                self.assertTrue(any(t=='details' and 'open' not in a for t,a in overview[2]))
                disclosure=next(n for n in nodes if n[0]=='details' and 'visa-case-overview-disclosure' in n[1].get('class',''))
                self.assertNotIn('open',disclosure[1])
                summary=next(n for n in nodes if n[0]=='summary')
                self.assertFalse(any(t=='form' for t,a in summary[2]), 'overview toggle must remain available outside edit forms')
                self.assertIn('name="original-field"',html)
                self.assertIn('/students/S1/visa-cases/7/materials-zip',html)
                self.assertIn('Alex &lt;Chen&gt; Test',html)
                self.assertIn('/assets/visa-case-scheme-c.css',html)

    def test_guardian_case_title_uses_the_applicant_and_keeps_student_context(self):
        html=self.render(applicant_name='Guardian <Chen>')
        self.assertIn('<h2>Guardian &lt;Chen&gt;</h2>',html)
        self.assertIn('Alex &lt;Chen&gt; Test',html)
        self.assertIn('学生编号 S1',html)


if __name__=='__main__': unittest.main()
