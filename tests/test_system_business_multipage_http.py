"""Concurrent real HTTP reads against the exact task staging listener."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time
import unittest
from tests import test_business_change_http as fixture


class MultiPageHTTPTests(unittest.TestCase):
    def test_concurrent_mixed_snapshots_retry_busy_and_drain(self):
        fixture.BusinessChangeHTTPTests.setUpClass()
        from staging_environment import ACTIVE
        report=[]
        surfaces=getattr(self,'SURFACES',['dashboard_overview','dashboard_workflow','mobile_dashboard_overview',
                  'student_summary&student_id=20260006'])*2
        for wave in range(3):
            barrier=threading.Barrier(len(surfaces))
            def read(surface):
                barrier.wait(timeout=5);started=time.monotonic()
                status,_,body=fixture.BusinessChangeHTTPTests.request_static('/api/business-changes/snapshot?surface='+surface)
                return dict(surface=surface,status=status,seconds=time.monotonic()-started,packet=json.loads(body))
            with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(read,surfaces))
            self.assertTrue(any(row['status']==200 for row in results))
            for row in results:
                self.assertIn(row['status'],(200,503),row)
                diagnostic={key:value for key,value in row.items() if key!='packet'}
                self.assertLess(row['seconds'],15,diagnostic)
                if row['status']==503:
                    self.assertEqual('snapshot_busy',row['packet']['code'])
                    self.assertLess(row['seconds'],3,diagnostic)
                    # A bounded busy response intentionally does not queue.
                    # Match browser backoff instead of immediately hammering the
                    # occupied slot and assuming the very next read must win.
                    retry_started=time.monotonic();deadline=retry_started+15;attempts=0
                    packet=row['packet'];status=503
                    while status==503:
                        self.assertEqual('snapshot_busy',packet.get('code'),packet)
                        delay=min(float(packet.get('retry_after_seconds',2)),3)
                        self.assertGreater(delay,0);self.assertLess(time.monotonic()+delay,deadline,diagnostic)
                        time.sleep(delay);attempts+=1
                        status,_,body=fixture.BusinessChangeHTTPTests.request_static('/api/business-changes/snapshot?surface='+row['surface'])
                        packet=json.loads(body)
                    self.assertEqual(200,status,body[:500]);self.assertIn('versions',packet)
                    row.update(retry_status=status,retry_attempts=attempts,retry_seconds=time.monotonic()-retry_started)
                    self.assertLess(row['retry_seconds'],20,diagnostic)
                else:self.assertIn('versions',row['packet'])
            report.append({'wave':wave+1,'requests':[{key:value for key,value in row.items() if key!='packet'} for row in results]})
        started=time.monotonic()
        status,_,_=fixture.BusinessChangeHTTPTests.request_static('/api/business-changes/snapshot?surface=dashboard_overview')
        drain_seconds=time.monotonic()-started
        self.assertEqual(200,status);self.assertLess(drain_seconds,8,'Backlog did not drain')
        report[-1]['drain_seconds']=drain_seconds
        (ACTIVE.root/'reports/multipage-http-load-20261120.json').write_text(json.dumps(report,indent=2))
        print('MULTIPAGE_HTTP='+json.dumps(report),flush=True)
