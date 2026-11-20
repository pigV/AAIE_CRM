import unittest
from unittest.mock import patch

import app
from tests import test_business_change_case_snapshots as case_fixture


class MobileHomeSyncTests(unittest.TestCase):
    setUp=case_fixture.VisaCaseSnapshotTests.setUp

    def snapshot(self):
        self.h.query={'surface':['mobile_dashboard_overview']}
        return self.h.business_change_snapshot('GET')

    def test_mobile_initial_page_and_snapshot_exclude_review_stage_without_source_writes(self):
        self.db.execute("UPDATE visa_cases SET status='Granted',result_status='Granted' WHERE id=?",(self.cid,))
        before=self.db.one('SELECT count(*) FROM business_change_events')[0]
        html=self.h.mobile_dashboard('GET')
        self.assertTrue('data-business-sync-initial-snapshot=' in html,'mobile initial source snapshot missing')
        status,first=self.snapshot()
        self.assertEqual(200,status)
        self.assertNotIn('visa_result_needs_review',first['data']['counts'])
        self.assertEqual(0,first['data']['total'])
        body=first['data']['fragments']['mobile-dashboard-overview']
        self.assertNotIn('签证材料需核实',body)
        self.assertNotIn('stage=visa_result_needs_review',body)
        status,second=self.snapshot()
        self.assertEqual(200,status)
        self.assertEqual(first['etag'],second['etag'])
        self.assertEqual(before,self.db.one('SELECT count(*) FROM business_change_events')[0])
        self.db.execute("UPDATE visa_cases SET status='Information Collection',result_status='Pending' WHERE id=?",(self.cid,))
        status,changed=self.snapshot()
        self.assertEqual(200,status)
        self.assertEqual(0,changed['data']['counts'].get('visa_result_needs_review',0))
        self.assertNotEqual(first['etag'],changed['etag'])

    def test_mobile_snapshot_requires_dashboard_access(self):
        with patch.object(app,'can_access_module',return_value=False):
            status,packet=self.snapshot()
        self.assertEqual(403,status)
        self.assertNotIn('data',packet)
