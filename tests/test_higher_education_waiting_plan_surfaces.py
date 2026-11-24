"""University intake stays visible while its school plan remains optional."""
import unittest
from higher_education_repository import HigherEducationRepository
from tests import test_business_change_student_list as list_fixture


class HigherEducationWaitingPlanSurfaces(unittest.TestCase):
    setUp = list_fixture.StudentListSnapshotTests.setUp

    def prepare(self):
        self.db.execute('UPDATE contracts SET voided_at=? WHERE student_id=?', (self.f.stamp, self.f.sid))
        self.db.execute("UPDATE students SET application_stream='higher_education' WHERE student_id=?", (self.f.sid,))
        self.db.execute("""INSERT INTO parent_form_tokens(token,expires_at,submitted_at,student_id,created_by,created_at,intake_type)
            VALUES('synthetic-he-intake','2099-01-01',?,?,1,?,'ordinary_new_application')""",
            (self.f.stamp,self.f.sid,self.f.stamp))
        self.db.execute("UPDATE application_cycles SET education_stream='higher_education',parent_form_token='synthetic-he-intake' WHERE id=?", (self.f.cycle,))

    def read(self, surface):
        self.h.query={'surface':[surface],'student_id':[self.f.sid],'q':[self.f.sid]}
        status, packet=self.h.business_change_snapshot('GET')
        self.assertEqual(200,status,packet)
        return packet['data']

    def assert_surfaces(self, key, label):
        events=self.db.one('SELECT count(*) FROM business_change_events')[0]
        for surface in ('student_summary','student_list','dashboard_overview','dashboard_workflow'):
            with self.subTest(surface=surface):
                data=self.read(surface)
                html=''.join(data['fragments'].values())
                self.assertIn(label,html)
                self.assertNotIn('暂无符合统计条件的申请阶段',html)
                if surface.startswith('dashboard_'):
                    self.assertEqual(1,data['total'])
                    self.assertEqual({key:1},data['counts'])
        self.assertEqual(events,self.db.one('SELECT count(*) FROM business_change_events')[0])

    def test_submitted_intake_and_native_plan_submission_update_all_four_surfaces(self):
        self.prepare()
        self.assert_surfaces('06_school_application_submission','准备学校申请')
        with self.db.transaction() as connection:
            repo=HigherEducationRepository(connection)
            provider=repo.create_provider('SYNTHETIC UNIVERSITY','University',1,self.f.stamp,review_status='Approved')
            plan=repo.create_plan(self.f.cycle,1,self.f.stamp)
            item=repo.add_plan_item(plan,provider,'Bachelor','SYNTHETIC DEGREE','2027-02',1,self.f.stamp)
            repo.confirm_plan(plan,1,self.f.stamp)
        self.assert_surfaces('06_school_application_submission','准备学校申请')
        with self.db.transaction() as connection:
            HigherEducationRepository(connection).submit_plan_item(item,'Direct','2027-02',1,self.f.stamp)
        self.assert_surfaces('07_school_response','等待学校发 Offer')

