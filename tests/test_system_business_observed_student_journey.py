"""A fresh staging journey, paused until each browser surface is verified."""
import json
from pathlib import Path
import re
import time
from tests import test_system_business_new_student_journey as journey


class ObservedStudentJourney(journey.NewStudentGrantJourney):
    def setUp(self):
        control=Path(__file__).resolve().parents[1]/'STAGING_DATA/reports/new-student-journey-control.json'
        if not control.exists() or not json.loads(control.read_text()).get('observe_all_stages'):
            self.skipTest('Explicit observe_all_stages control is required for browser-gated integration.')
        super().setUp()
        self.last_overview=None

    def packet(self,surface,**query):
        packet=super().packet(surface,**query)
        if surface=='dashboard_overview':self.last_overview=packet
        return packet

    def checkpoint(self,name,expected=None):
        super().checkpoint(name,expected)
        step=self.report['steps'][-1]
        html=self.last_overview['data']['fragments']['dashboard-overview']
        metrics=dict(re.findall(r'<div class="metric">\s*<span>([^<]+)</span>\s*<strong>([^<]+)</strong>',html))
        self.assertEqual(6,len(metrics),metrics)
        baseline=self.report.setdefault('observation_baseline_metrics',metrics)
        number=int(name[:2])
        for label in ('本周签约学生','本月签约学生'):
            self.assertEqual(int(baseline[label])+int(number>=5),int(metrics[label]),label)
        for label in ('本周下签','本月下签'):
            self.assertEqual(int(baseline[label])+int(number==16),int(metrics[label]),label)
        # Existing policy counts formal receipt events, including SignedOffer.
        self.assertEqual(int(baseline['本周 OFFER'])+int(number>=9)+int(number>=10),int(metrics['本周 OFFER']))
        self.assertEqual(baseline['累计终止申请'],metrics['累计终止申请'])
        ready={**step,'run_id':self.run_id,'student_id':self.sid,'index':number,
            'metrics':metrics,'counts':self.last_overview['data']['counts'],
            'evidence_directory':str(self.root/'browser-observations')}
        (self.root/'browser-observations').mkdir(exist_ok=True)
        (self.staging_root/'reports/observed-journey-ready.json').write_text(json.dumps(ready,ensure_ascii=False,indent=2))
        print('BROWSER CHECKPOINT READY',number,flush=True)
        deadline=time.monotonic()+240
        ack_path=self.staging_root/'reports/observed-journey-ack.json'
        while time.monotonic()<deadline:
            if ack_path.exists():
                ack=json.loads(ack_path.read_text())
                if ack.get('run_id')==self.run_id and ack.get('index')==number:
                    self.assertTrue(ack.get('passed'),ack)
                    step['browser_observed']=True
                    step['home_all_metrics']=metrics
                    self.save()
                    print('BROWSER CHECKPOINT VERIFIED',number,flush=True)
                    return
            time.sleep(.3)
        self.fail('Browser observation was not verified before the checkpoint deadline.')
