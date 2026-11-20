"""Retained real native/HTTP journeys gated on three browser pages (four areas)."""
import json
from pathlib import Path
from tests import test_system_business_observed_student_journey as observed
from tests import test_system_business_complete_branches as branches

class FourSurfaceNewStudentJourney(observed.ObservedStudentJourney):
    def setUp(self):
        root=Path(__file__).resolve().parents[1]/'STAGING_DATA/reports'
        own=root/'four-surface-new-student-control.json'
        shared=root/'new-student-journey-control.json'
        if not own.exists():
            previous=shared.read_text() if shared.exists() else None
            own.write_text(json.dumps({'previous_control':previous,'started':True,'new_run':True}))
        state=json.loads(own.read_text())
        shared.write_text(json.dumps({'observe_all_stages':True,**({'resume_report':state['resume_report']} if state.get('resume_report') else {'new_run':True})}))
        super().setUp()
        state['resume_report']=str(self.report_path);own.write_text(json.dumps(state))

class FourSurfaceBranchJourneys(branches.CompleteBranchJourneys):
    CONTROL_NAME='four-surface-branch-control.json'
    RUN_PREFIX='four-surface-branches-'
    BRANCHES=('refusal','withdrawal','transfer_retest','progression_retest')

import time
import unittest
from tests import test_business_change_http as http_fixture
from tests import test_system_business_branch_scenarios as support

class FourSurfaceMaterialCorrections(unittest.TestCase):
    packet=branches.CompleteBranchJourneys.packet
    post=branches.CompleteBranchJourneys.post
    save=branches.CompleteBranchJourneys.save
    once=branches.CompleteBranchJourneys.once
    checkpoint=branches.CompleteBranchJourneys.checkpoint

    def test_confirmed_grant_archive_restore(self):
        from staging_environment import ACTIVE
        http_fixture.BusinessChangeHTTPTests.setUpClass()
        self.http=http_fixture.BusinessChangeHTTPTests();self.app=self.http.app;self.db=self.app.db
        control=ACTIVE.root/'reports/four-surface-material-control.json'
        if control.exists():
            self.root=Path(json.loads(control.read_text())['report']).parent
            self.report=json.loads((self.root/'result.json').read_text())
        else:
            main=json.loads((ACTIVE.root/'reports/four-surface-new-student-control.json').read_text())
            source=json.loads(Path(main['resume_report']).read_text());self.assertEqual('passed',source['status'])
            self.root=ACTIVE.root/'reports'/('four-surface-material-'+time.strftime('%Y%m%d-%H%M%S'));self.root.mkdir()
            self.report={'status':'running','mode':'SYNTHETIC ONLY','student_id':source['student_id'],
                'cycle_id':source['cycle_id'],'visa_case_id':source['visa_case_id'],'steps':[],'actions':{},'issues':[]}
            control.write_text(json.dumps({'report':str(self.root/'result.json')}))
        self.sid=self.report['student_id'];self.cycle=self.report['cycle_id'];self.http.sid=self.sid
        self.branch='material_correction';self.ctx={}
        self.root.joinpath('watch-browser').touch()
        case=self.report['visa_case_id']
        document=self.db.one('SELECT id FROM visa_case_documents WHERE visa_case_id=? ORDER BY id DESC LIMIT 1',(case,))[0]
        try:
            self.once('archive',lambda:self.post(f'/visa-management/cases/{case}/workspace',{'action':'archive','document_id':document})[0])
            self.expect_grant=False;self.checkpoint('下签材料作废后转为需核实',0,'签证材料需核实')
            self.once('restore',lambda:self.post(f'/visa-management/cases/{case}/workspace',{'action':'restore','document_id':document})[0])
            self.expect_grant=True;self.checkpoint('恢复有效下签材料',0,'已下签')
            self.assertFalse(self.report['issues'],self.report['issues']);self.report['status']='passed'
        except BaseException as exc:self.report['status']='failed';self.report['error']=repr(exc);raise
        finally:self.save()
