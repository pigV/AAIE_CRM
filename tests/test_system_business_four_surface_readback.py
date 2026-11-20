"""Independent final expectations and HTTP reads for retained synthetic students."""
from collections import Counter
import json
from pathlib import Path
import unittest
from html.parser import HTMLParser
from tests import test_business_change_http as fixture
from tests import test_system_business_multipage_http as load
from tests import test_system_business_new_student_journey as journey

class RenderedRows(HTMLParser):
    def __init__(self):super().__init__();self.rows=[];self.parts=None
    def handle_starttag(self,tag,attrs):
        if tag=='tr':self.parts=[]
    def handle_data(self,text):
        if self.parts is not None:self.parts.append(text)
    def handle_endtag(self,tag):
        if tag=='tr' and self.parts is not None:self.rows.append(' '.join(self.parts));self.parts=None

class FourSurfaceFinalReadback(unittest.TestCase):
    packet=journey.NewStudentGrantJourney.packet
    def test_final_sources_lists_home_and_evidence(self):
        fixture.BusinessChangeHTTPTests.setUpClass();http=fixture.BusinessChangeHTTPTests();self.http=http;db=http.app.db
        from staging_environment import ACTIVE
        from student_application_stage_distribution import load_application_stage_distribution,load_application_stage_result_map
        from student_management_summary import load_student_management_summary_map
        from dashboard_visa_metrics import load_student_visa_grant_records
        from datetime import date
        root=ACTIVE.root/'reports'
        main=Path(json.loads((root/'four-surface-new-student-control.json').read_text())['resume_report'])
        branch=Path(json.loads((root/'four-surface-branch-control.json').read_text())['report'])
        material=Path(json.loads((root/'four-surface-material-control.json').read_text())['report'])
        m=json.loads(main.read_text());b=json.loads(branch.read_text())
        expected={m['student_id']:'已下签',b['context']['refusal']['student_id']:'等待建立合同',
            b['context']['withdrawal']['student_id']:'等待家长填写表单',
            b['context']['transfer_retest']['student_id']:'转学完成 · Balwyn High School',
            b['context']['progression_retest']['student_id']:'已下签'}
        checked=0;images=0
        for source,kind in ((main,'main'),(material,'branch'),(branch,'branch')):
            r=json.loads(source.read_text());self.assertEqual('passed',r['status'])
            for n,s in enumerate(r['steps'],1):
                folder=source.parent/('browser-observations' if kind=='main' else '')
                o=json.loads((folder/f'{n:02d}-four-surfaces.json').read_text());self.assertTrue(o['verified']);checked+=1
                for key in (('home',) if kind=='main' and n==1 else ('student','list','home')):
                    self.assertGreater((folder/f'{n:02d}-{key}.jpg').stat().st_size,1000);images+=1
        with db.read_snapshot() as c:
            self.assertEqual('ok',c.execute('PRAGMA integrity_check').fetchone()[0]);self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())
            stage=load_application_stage_result_map(c,expected);summaries=load_student_management_summary_map(c,expected)
            rows=load_application_stage_distribution(c,cycles_only=True);counts=dict(Counter(x.stage.key for x in rows if x.stage.key))
            week,month=load_student_visa_grant_records(c,today=date.today())
            for sid,label in expected.items():
                self.assertEqual(label,stage[sid].label);self.assertEqual(label,summaries[sid].stage_label)
                self.assertEqual(label=='已下签',sid in {x['student_id'] for x in week})
                self.assertEqual(label=='已下签',sid in {x['student_id'] for x in month})
        for sid,label in expected.items():
            packet=self.packet('student_list',q=sid,sort='stage')
            html=packet['data']['fragments']['student-list']
            parser=RenderedRows();parser.feed(html)
            matching=[row for row in parser.rows if sid in row]
            self.assertEqual(1,len(matching));self.assertIn(label,matching[0])
        for surface in ('dashboard_overview','dashboard_workflow'):
            packet=self.packet(surface)
            self.assertEqual(counts,packet['data']['counts']);self.assertEqual(len(rows),packet['data']['total'])
        result={'status':'passed','expected_student_states':expected,'checkpoints':checked,'phase_images':images,
            'distribution_total':len(rows),'counts':counts,'integrity':'ok','foreign_key_violations':0}
        (root/'four-surface-final-readback.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps(result,ensure_ascii=False),flush=True)

class FourSurfaceMultiPageHTTP(load.MultiPageHTTPTests):
    SURFACES=['dashboard_overview','dashboard_workflow','student_list&q=20260024&sort=stage','student_summary&student_id=20260024']
