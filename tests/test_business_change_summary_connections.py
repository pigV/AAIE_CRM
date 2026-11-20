import sqlite3
import unittest
from unittest.mock import patch
from student_case_feature_flags import StudentCaseFeatureFlags
from tests import test_student_lifecycle_case_integration as fixture
from student_transfer_summary import build_student_transfer_summary
from student_progression_summary import build_student_progression_summary


class SharedSummarySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.StudentLifecycleCaseIntegrationTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.viewer={'id':1,'permissions':['student_cases.view','student_cases.create','progression_cases.view','progression_cases.create']}
        self.flags=StudentCaseFeatureFlags(read_enabled=True,transfer_write_enabled=True,progression_write_enabled=True)

    def snapshot(self,builder):
        c=sqlite3.connect(self.f.database_path);c.row_factory=sqlite3.Row;self.addCleanup(c.close)
        c.execute('BEGIN');c.execute('PRAGMA query_only=ON')
        with patch('sqlite3.connect',side_effect=AssertionError('independent snapshot forbidden')):
            result=builder('student-1',self.viewer,database_path=self.f.database_path,flags=self.flags,connection=c)
        self.assertTrue(c.in_transaction)
        self.assertIsNotNone(result)
        return result

    def test_transfer_case_applications_completion_and_projection_share_connection(self):
        self.f.create_transfer_draft()
        self.snapshot(build_student_transfer_summary)

    def test_progression_workspace_readiness_and_projection_share_connection(self):
        self.f.create_progression_draft()
        self.snapshot(build_student_progression_summary)

    def test_transfer_tab_selectors_staff_and_case_rows_share_snapshot(self):
        import transfer_case_views
        case=self.f.create_transfer_draft()
        def render(student_id,user,*,database_path,flags,connection):
            return transfer_case_views.render_transfer_case_tab(database_path,student_id,user,flags,
                csrf='synthetic',connection=connection)
        html=self.snapshot(render)
        self.assertIn('/transfer-cases/'+str(case.case_id),html)

    def test_progression_tab_contract_selector_and_cases_share_snapshot(self):
        import progression_case_views
        case=self.f.create_progression_draft()
        def render(student_id,user,*,database_path,flags,connection):
            return progression_case_views.render_progression_case_tab(database_path,student_id,user,flags,
                csrf='synthetic',staff_users=(),connection=connection)
        html=self.snapshot(render)
        self.assertIn('/progression-cases/'+str(case.case_id),html)
