from pathlib import Path
import unittest
from tests import test_business_change_school_chain as fixture


class CoursePanelSnapshotTests(unittest.TestCase):
    def test_course_panel_reuses_current_student_tab_snapshot(self):
        f = fixture.SchoolMaterialChainTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        with f.db.transaction() as connection:
            connection.executescript((Path(__file__).resolve().parents[1]/'migrations/20261120_052_school_course_tuition.sql').read_text())
        f.h._can_correct_student_tuition=lambda:True
        with f.db.request_scope():
            with f.db.read_snapshot():
                self.assertEqual('', f.h.student_course_tuition_panel(f.sid))
