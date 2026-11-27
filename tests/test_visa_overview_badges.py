"""Exercise real overview HTML without importing app startup or a database."""
import ast
from html import escape
from html.parser import HTMLParser
from http import HTTPStatus
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Cells(HTMLParser):
    def __init__(self, html):
        super().__init__(); self.rows=[]; self.row=None; self.cell=None; self.feed(html)
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='tr': self.row=[]
        if tag=='td': self.cell={'text':'','classes':[]}
        if tag=='span' and self.cell is not None:
            self.cell['classes'].append(attrs.get('class',''))
    def handle_data(self, data):
        if self.cell is not None: self.cell['text']+=data
    def handle_endtag(self, tag):
        if tag=='td': self.row.append(self.cell); self.cell=None
        if tag=='tr' and self.row: self.rows.append(self.row)


class VisaOverviewBadgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        names={'visa_management','visa_management_text','visa_management_status_class','visa_management_student_name'}
        nodes=[n for n in ast.walk(ast.parse((ROOT/'app.py').read_text()))
               if isinstance(n,ast.FunctionDef) and n.name in names]
        env={'HTTPStatus':HTTPStatus,'esc':lambda v:escape(str(v or ''),quote=True),
             'can_access_module':lambda *a:True,'display_datetime':lambda v:v}
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'actual-visa-overview','exec'),env)
        class Harness:
            user={}; csrf='synthetic'; query={'q':['<test>']}
            def visa_management_case_rows(self): return self.rows
            def visa_management_kpis(self): return [('Visa Cases',len(self.rows),'synthetic')]
            def business_change_visa_consistency_html(self,**kwargs): return ''
        for name in names: setattr(Harness,name,env[name])
        cls.Harness=Harness

    def render(self, labels=('Granted','Refused','Withdrawn','Pending'), kind='Student Visa', subclass='500'):
        h=self.Harness()
        h.rows=[dict(id=i+1,passport_first_name='Demo <Chen>',passport_last_name='',student_id='TEST',
                     case_type=kind,visa_subclass=subclass,status=label,decision_status=label,
                     expiry_date='2027-01-01',updated_at='2026-11-28',created_at='2026-11-28')
                for i,label in enumerate(labels)]
        return h.visa_management('GET',fragment_only=True)

    def test_same_name_status_and_decision_use_the_same_badge(self):
        for row,tone in zip(Cells(self.render()).rows,('green','red','gray','amber')):
            self.assertEqual(row[4]['text'],row[5]['text'])
            self.assertEqual(row[4]['classes'],row[5]['classes'])
            self.assertIn('visa-overview-tag--'+tone,' '.join(row[4]['classes']))

    def test_type_and_subclass_share_a_family_color(self):
        for kind,subclass,tone in [('Student Visa','500','blue'),('Guardian Visa','590','purple'),
                                   ('Visitor Visa','600','cyan'),('485','485','indigo')]:
            row=Cells(self.render(labels=('Draft',),kind=kind,subclass=subclass)).rows[0]
            for column in (2,3): self.assertIn('visa-overview-tag--'+tone,' '.join(row[column]['classes']))

    def test_unknown_and_empty_values_are_neutral_and_html_is_escaped(self):
        html=self.render(labels=('Unrecognised <state>',),kind='<img src=x onerror=alert(1)>',subclass=None)
        row=Cells(html).rows[0]
        for column in (2,3,4,5): self.assertIn('visa-overview-tag--gray',' '.join(row[column]['classes']))
        self.assertEqual(row[3]['text'],'—')
        self.assertEqual(row[2]['text'],'<img src=x onerror=alert(1)>')
        self.assertNotIn('<img src=x',html)
        self.assertIn('Unrecognised &lt;state&gt;',html)

    def test_case_insensitive_color_preserves_the_displayed_label(self):
        row=Cells(self.render(labels=('refused',))).rows[0]
        self.assertEqual(row[4]['text'],'refused')
        self.assertIn('visa-overview-tag--red',' '.join(row[4]['classes']))

    def test_existing_columns_controls_links_and_empty_state_are_preserved(self):
        html=self.render()
        self.assertEqual(len(Cells(html).rows[0]),8)
        for value in ('action="/visa-management"','name="q"','value="&lt;test&gt;"',
                      'href="/visa-management/applicants/new"','href="/visa-management/cases/1"',
                      'Demo &lt;Chen&gt;','2027-01-01','2026-11-28','data-business-sync-fragment="visa-overview"'):
            self.assertIn(value,html)
        self.assertIn('暂无 Visa Case',self.render(labels=()))


if __name__=='__main__': unittest.main()
