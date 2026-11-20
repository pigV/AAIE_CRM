"""Real list reads must follow current facts while retaining filter semantics."""
import unittest
from html.parser import HTMLParser
from unittest.mock import patch
from tests import test_business_change_school_chain as fixture
from business_changes.contracts import Subject
from business_changes.versions import version_of

class StageSelection(HTMLParser):
    def __init__(self):super().__init__();self.in_stage=False;self.value=''
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='select':self.in_stage=attrs.get('name')=='stage'
        if tag=='option' and self.in_stage and 'selected' in attrs:self.value=attrs.get('value','')
    def handle_endtag(self,tag):
        if tag=='select':self.in_stage=False

class StudentListSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.SchoolMaterialChainTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db;self.h=self.f.h
        self.h.json_response=lambda value,status=200:(status,value)
        self.h.send_html=lambda value,*a,**kw:value
        p=patch.object(__import__('app').nas,'status',return_value={},create=True);p.start();self.addCleanup(p.stop)

    def read(self,**filters):
        self.h.query={'surface':['student_list'],'q':[self.f.sid],'sort':['stage'],**{k:[v] for k,v in filters.items()}}
        return self.h.business_change_snapshot('GET')

    def test_filtered_list_changes_from_preparation_to_formal_refusal_and_old_filter_excludes_student(self):
        status,before=self.read(stage='waiting_school_plan');self.assertEqual(200,status,before)
        html=before['data']['fragments']['student-list'];self.assertIn('/students/'+self.f.sid,html)
        self.assertIn('surface=student_list',html);self.assertNotIn('app-shell',html)
        self.assertIn('q=SCHOOL-CHAIN',html);self.assertIn('sort=stage',html)
        case=self.db.execute("""INSERT INTO visa_cases(student_id,application_cycle_id,visa_case_name,visa_type,
          applicant_name,application_type,status,result_status,created_by,created_at,updated_at)
          VALUES(?,?,'Synthetic list Visa','500 Student Visa','Synthetic','New Application','Lodged','Pending',1,?,?)""",
          (self.f.sid,self.f.cycle,self.f.stamp,self.f.stamp))
        from visa_result_status import record_manual_result
        with self.db.transaction() as c:record_manual_result(c,case,'Refused',actor_user_id=1,occurred_at=self.f.stamp,reason='Synthetic refusal')
        status,old=self.read(stage='waiting_school_plan');self.assertEqual(200,status,old)
        self.assertNotIn('/students/'+self.f.sid,old['data']['fragments']['student-list'])
        selected=StageSelection();selected.feed(old['data']['fragments']['student-list'])
        self.assertEqual('waiting_school_plan',selected.value,'A student leaving the filtered phase must not clear the selected filter')
        status,current=self.read(stage='visa_refused');self.assertEqual(200,status,current)
        self.assertIn('已拒签',current['data']['fragments']['student-list'])
        self.assertGreater(current['versions'][0]['version'],before['versions'][0]['version'])

    def test_readonly_list_versions_minimum_and_module_permissions(self):
        before=self.db.one('SELECT count(*) FROM business_change_events')[0]
        status,one=self.read();self.assertEqual(200,status,one)
        status,two=self.read();self.assertEqual(200,status,two);self.assertEqual(one['etag'],two['etag'])
        self.assertEqual(409,self.read(minimum_version='999999')[0])
        self.h.query={'surface':['student_list']}
        self.assertEqual(200,self.h.business_change_versions('GET')[0])
        with patch.object(__import__('app'),'allowed',return_value=False):
            self.assertEqual(403,self.read()[0]);self.assertEqual(403,self.h.business_change_versions('GET')[0])
        self.assertEqual(before,self.db.one('SELECT count(*) FROM business_change_events')[0])
