"""Final read-only facts, API and retained browser evidence verification."""
from collections import Counter
import hashlib,json
from pathlib import Path
import unittest
from tests import test_business_change_http as fixture

class FinalBranchReadbackTests(unittest.TestCase):
    def test_complete_branches_and_retained_evidence(self):
        fixture.BusinessChangeHTTPTests.setUpClass();http=fixture.BusinessChangeHTTPTests();db=http.app.db
        from staging_environment import ACTIVE
        from student_application_stage_distribution import load_application_stage_distribution,load_application_stage_result_map
        from student_management_summary import load_student_management_summary_map
        from dashboard_visa_metrics import load_student_visa_grant_records
        from datetime import date
        source=Path(json.loads((ACTIVE.root/'reports/full-branch-journey-control.json').read_text())['report'])
        report=json.loads(source.read_text());self.assertEqual('passed',report['status']);self.assertFalse(report['issues'])
        for n,step in enumerate(report['steps'],1):
            browser=json.loads((source.parent/f'{n:02d}-browser.json').read_text())
            self.assertTrue(browser['verified']);self.assertEqual(step['name'],browser['name'])
            for key in ('student','home'):self.assertGreater((source.parent/f'{n:02d}-{key}.jpg').stat().st_size,1000)
        students=[v['student_id'] for v in report['context'].values()]
        with db.read_snapshot() as c:
            self.assertEqual('ok',c.execute('PRAGMA integrity_check').fetchone()[0]);self.assertEqual([],c.execute('PRAGMA foreign_key_check').fetchall())
            stage=load_application_stage_result_map(c,students);summary=load_student_management_summary_map(c,students)
            rows=load_application_stage_distribution(c,cycles_only=True);counts=dict(Counter(r.stage.key for r in rows if r.stage.key))
            week,month=load_student_visa_grant_records(c,today=date.today())
            grant=report['context']['progression_retest']['student_id']
            self.assertIn(grant,{r['student_id'] for r in week});self.assertIn(grant,{r['student_id'] for r in month})
            for name,key in report['acceptance_branches'].items():
                last=next(s for s in reversed(report['steps']) if s['name'].startswith(key+' · '));sid=last['student_id']
                self.assertEqual(last['student_label'],stage[sid].label);self.assertEqual(last['summary_label'],summary[sid].stage_label)
                self.assertEqual(last['membership'],sum(r.cycle_id==last['cycle_id'] for r in rows))
            transfer=report['context']['transfer_retest']['student_id']
            current=c.execute("SELECT school_name_snapshot FROM student_school_enrolments WHERE student_id=? AND enrolment_role='current' AND status='active'",(transfer,)).fetchall()
            self.assertEqual(['Balwyn High School'],[r[0] for r in current])
            documents=[]
            for row in c.execute("SELECT id,file_path,sha256 FROM visa_case_documents WHERE visa_case_id=? AND archived_at IS NULL",(report['context']['progression_retest']['visa_case_id'],)):
                data=Path(row['file_path']).read_bytes();self.assertEqual(row['sha256'],hashlib.sha256(data).hexdigest());documents.append({'id':row['id'],'bytes':len(data),'sha256':row['sha256']})
            self.assertTrue(documents)
        for surface in ('dashboard_workflow','mobile_dashboard_overview'):
            status,_,body=http.request_static('/api/business-changes/snapshot?surface='+surface)
            self.assertEqual(200,status,body[:300]);packet=json.loads(body)
            self.assertEqual({k:v for k,v in counts.items() if v},{k:v for k,v in packet['data']['counts'].items() if v})
            self.assertEqual(len(rows),packet['data']['total'])
        result={'status':'passed','students':students,'checks':len(report['steps']),'screenshots':len(report['steps'])*2,
            'integrity':'ok','foreign_key_violations':0,'grant_student':grant,'transfer_current_school':'Balwyn High School',
            'final_distribution_total':len(rows),'final_counts':counts,'visa_documents':documents}
        (ACTIVE.root/'reports/branch-final-readback-20261120.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print('FINAL_READBACK='+json.dumps(result,ensure_ascii=False),flush=True)
