import unittest
from unittest.mock import patch

from tests import test_business_change_sync_ui as ui_fixture
from tests import test_business_change_case_snapshots as case_fixture


class LightweightVersionAPITests(unittest.TestCase):
    setUp=ui_fixture.BusinessSyncUITests.setUp

    def test_versions_read_never_loads_workflow_or_html(self):
        status,packet=self.h.business_change_versions('GET')
        self.assertEqual(200,status,packet)
        self.assertEqual([{'kind':'dashboard','id':'workflow','version':0}],packet['versions'])
        self.assertNotIn('data',packet)
        self.assertEqual(0,self.reads)

    def test_versions_keep_auth_scope_and_minimum_revision_guards(self):
        self.h.user=None;self.assertEqual(401,self.h.business_change_versions('GET')[0])
        self.h.user={'id':1};self.allowed=False
        self.assertEqual(403,self.h.business_change_versions('GET')[0])
        self.allowed=True
        self.assertEqual(405,self.h.business_change_versions('POST')[0])
        self.h.query['surface']=['arbitrary_database_entity']
        self.assertEqual(400,self.h.business_change_versions('GET')[0])
        self.h.query['surface']=['dashboard_workflow'];self.h.query['minimum_version']=['99']
        self.assertEqual(409,self.h.business_change_versions('GET')[0])
        self.assertEqual(0,self.reads)


class OwnedVersionAPITests(unittest.TestCase):
    setUp=case_fixture.VisaCaseSnapshotTests.setUp

    def test_case_versions_require_exact_student_and_unarchived_case(self):
        self.h.query={'surface':['student_visa_case'],'case_id':[str(self.cid)],'student_id':[self.f.sid]}
        with patch.object(self.h,'student_visa_case_detail',side_effect=AssertionError('version API rendered case')):
            self.assertEqual(200,self.h.business_change_versions('GET')[0])
        self.h.query['student_id']=['FOREIGN']
        self.assertEqual(404,self.h.business_change_versions('GET')[0])
        self.h.query['student_id']=[self.f.sid]
        self.db.execute('UPDATE visa_cases SET archived_at=? WHERE id=?',(self.f.stamp,self.cid))
        self.assertEqual(404,self.h.business_change_versions('GET')[0])

    def test_student_scope_requires_current_cycle_owner_and_students_module(self):
        self.h.query={'surface':['student_summary'],'student_id':[self.f.sid],'application_cycle_id':[str(self.f.cycle)]}
        self.assertEqual(200,self.h.business_change_versions('GET')[0])
        self.h.query['application_cycle_id']=['999999999']
        self.assertEqual(404,self.h.business_change_versions('GET')[0])
        with patch('app.can_access_module',return_value=False):
            self.assertEqual(403,self.h.business_change_versions('GET')[0])
