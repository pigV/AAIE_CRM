"""Local admission closes the initial application without inventing current study."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from student_lifecycle.enrolment import SchoolEnrolmentService, SchoolEnrolmentConflictError, SchoolEnrolmentValidationError
from tests.test_student_case_phase2_transfer import _service_fixture_template


class LocalStudentAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'admission.db'
        _service_fixture_template().copy_to(self.path)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("""INSERT INTO application_cycles(
                cycle_uid,student_id,cycle_type,sequence_no,status,opened_at,created_at,updated_at)
                VALUES('local-initial','student-1','initial',1,'active','2026-08-01','2026-08-01','2026-08-01')""")
            conn.commit()
        self.service = SchoolEnrolmentService(self.path, clock=lambda: '2026-11-19T10:00:00+11:00')

    def confirm(self, **overrides):
        args = dict(student_id='student-1', school_id=10, school_name_snapshot='Local School',
                    actor_user_id=1, reason='School confirmed local admission',
                    start_date='2027-02-01', school_confirmed=True)
        args.update(overrides)
        return self.service.confirm_local_admission(**args)

    def state(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return (conn.execute("SELECT status,outcome FROM application_cycles WHERE cycle_uid='local-initial'").fetchone(),
                    conn.execute('SELECT enrolment_role,start_date,application_cycle_id FROM student_school_enrolments').fetchall(),
                    conn.execute('SELECT event_type FROM school_enrolment_events ORDER BY id').fetchall())

    def test_future_school_is_upcoming_and_application_is_completed_atomically(self):
        result = self.confirm()
        cycle, rows, events = self.state()
        self.assertEqual('upcoming', result.role)
        self.assertEqual(('closed', 'completed'), cycle)
        self.assertEqual([('upcoming', '2027-02-01', rows[0][2])], rows)
        self.assertIsNotNone(rows[0][2])
        self.assertEqual([('upcoming_confirmed',)], events)
        replay = self.confirm()
        self.assertEqual(result.enrolment_id, replay.enrolment_id)
        self.assertEqual(1, len(self.state()[1]))

    def test_started_admission_is_current(self):
        self.confirm(start_date='2026-11-19')
        self.assertEqual('current', self.state()[1][0][0])
        self.assertEqual(('closed', 'completed'), self.state()[0])

    def test_confirmation_and_real_date_are_required_without_closing_cycle(self):
        for overrides in ({'school_confirmed': False}, {'start_date': ''}, {'start_date': '2027-02-30'}, {'reason': ''}):
            with self.subTest(overrides=overrides), self.assertRaises((SchoolEnrolmentValidationError, ValueError)):
                self.confirm(**overrides)
        self.assertEqual((('active', None), [], []), self.state())

    def test_active_transfer_cycle_cannot_be_closed_as_local_admission(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("""INSERT INTO student_cases(id,student_id,case_type,sequence_no,status,
                active_slot,created_by,created_at,updated_by,updated_at)
                VALUES(99,'student-1','transfer',1,'draft',0,1,'now',1,'now')""")
            conn.execute("UPDATE application_cycles SET cycle_type='transfer',source_student_case_id=99")
            conn.commit()
        with self.assertRaises(SchoolEnrolmentConflictError):
            self.confirm()
        self.assertEqual((('active', None), [], []), self.state())

    def test_due_initial_upcoming_can_be_explicitly_promoted_with_audit(self):
        result = self.confirm()
        later = SchoolEnrolmentService(self.path, clock=lambda: '2027-02-01T10:00:00+11:00')
        promoted = later.confirm_local_admission(student_id='student-1', school_id=10,
            school_name_snapshot='Local School', actor_user_id=1, reason='Start verified',
            start_date='2027-02-01', school_confirmed=True)
        self.assertEqual(result.enrolment_id, promoted.enrolment_id)
        self.assertEqual('current', self.state()[1][0][0])
        self.assertEqual([('upcoming_confirmed',), ('current_confirmed',)], self.state()[2])

    def test_same_existing_initial_school_is_bound_and_completed_without_duplicate(self):
        current = self.service.confirm_current(student_id='student-1', school_id=None,
            school_name_snapshot='  Local School ', actor_user_id=1,
            reason='Original current-school confirmation',start_date='2026-10-05')
        result = self.confirm(start_date='2026-10-05')
        self.assertEqual(current.enrolment_id,result.enrolment_id)
        self.assertEqual(('closed','completed'),self.state()[0])
        self.assertEqual(1,len(self.state()[1]))
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(10,conn.execute('SELECT school_id FROM student_school_enrolments').fetchone()[0])

    def test_existing_other_current_does_not_overwrite_relationship(self):
        self.service.confirm_current(student_id='student-1', school_id=20,
            school_name_snapshot='Other School', actor_user_id=1, reason='Current confirmed')
        with self.assertRaises(SchoolEnrolmentConflictError):
            self.confirm()
        self.assertEqual(('active', None), self.state()[0])

    def test_cycle_close_failure_rolls_back_school_and_audit(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("""CREATE TRIGGER reject_initial_close BEFORE UPDATE OF status ON application_cycles
                WHEN NEW.status='closed' BEGIN SELECT RAISE(ABORT,'injected close failure'); END""")
            conn.commit()
        with self.assertRaises(SchoolEnrolmentConflictError):
            self.confirm()
        self.assertEqual((('active', None), [], []), self.state())


class LocalStudentAdmissionRouteTests(unittest.TestCase):
    """Real handlers and isolated migrated database, without network services."""
    @classmethod
    def setUpClass(cls):
        import app
        cls.app = app
        from database import Database
        cls.folder = tempfile.TemporaryDirectory(dir=app.APP_DIR)
        cls.addClassCleanup(cls.folder.cleanup)
        cls.database = Database(Path(cls.folder.name) / 'routes.db')
        cls.database.initialize()
        from tests.test_core import prepare_visa_candidate_test_database
        prepare_visa_candidate_test_database(cls.database)
        cls.old_db = app.db
        app.db = cls.database
        cls.addClassCleanup(setattr, app, 'db', cls.old_db)
        cls.database.execute("""INSERT INTO students(student_id,passport_first_name,passport_last_name,
            created_date,updated_at,form_data_json) VALUES('20260112','Xiangrong','Liu','2026-11-19','2026-11-19','{}')""")
        cls.database.execute("""INSERT INTO students(student_id,passport_first_name,passport_last_name,
            created_date,updated_at,form_data_json) VALUES('20260087','Mika','Wang','2026-11-19','2026-11-19','{}')""")
        with cls.database.transaction() as conn:
            conn.execute("""INSERT INTO application_cycles(cycle_uid,student_id,cycle_type,sequence_no,
                status,opened_at,created_at,updated_at)
                VALUES('mika-local','20260087','initial',1,'active','2026-11-19','2026-11-19','2026-11-19')""")
            conn.execute("""INSERT INTO application_cycles(cycle_uid,student_id,cycle_type,sequence_no,
                status,opened_at,created_at,updated_at)
                VALUES('xiangrong-local','20260112','initial',1,'active','2026-11-19','2026-11-19','2026-11-19')""")
            conn.executemany("INSERT INTO schools(school_name,state,international_student_available,sector_key,school_type,created_at,updated_at) VALUES(?,'ACT',1,'Government','Primary','now','now')",
                             [(f'Before Queensland {i:04}',) for i in range(1005)])
            conn.execute("INSERT INTO schools(id,school_name,state,international_student_available,sector_key,school_type,created_at,updated_at) VALUES(1081,'MacGregor State School','QLD',0,'Government','Primary','now','now')")
        cls.current = SchoolEnrolmentService(cls.database.path).confirm_current(
            student_id='20260112',school_id=None,school_name_snapshot='MacGregor State School',
            actor_user_id=1,reason='Prior explicit current school registration',start_date='2026-10-05')

    def handler(self, *, form=None):
        handler = object.__new__(self.app.CRMHandler)
        handler.user = dict(self.database.one("SELECT * FROM users WHERE username='admin'"))
        handler.csrf = 'isolated-test'
        handler.query = {'student_id': ['20260112']}
        handler.form = form or {}
        handler.require = lambda permission: True
        handler._student_enrolment_write_allowed = lambda student_id: True
        handler.flash_redirect = lambda *args: args
        handler.captured = {}
        handler.send_html = lambda html, **kwargs: handler.captured.update(html=html)
        handler.error_page = lambda *args: self.fail(str(args))
        return handler

    def test_local_route_records_future_school_and_closes_initial(self):
        handler = self.handler(form=dict(school_id='1081', local_admission='yes', school_confirmed='yes',
            start_date='2035-01-29', reason='School acceptance explicitly verified'))
        response = handler.student_school_enrolment_current('POST', '20260087')
        self.assertIn('待入读学校', response[1])
        self.assertIn('本次入学申请已完成', response[1])
        row = self.database.one("SELECT enrolment_role,school_id FROM student_school_enrolments WHERE student_id='20260087'")
        self.assertEqual(('upcoming',1081), tuple(row))
        cycle = self.database.one("SELECT status,outcome FROM application_cycles WHERE cycle_uid='mika-local'")
        self.assertEqual(('closed','completed'), tuple(cycle))
        replay = handler.student_school_enrolment_current('POST', '20260087')
        self.assertIn('本次入学申请已完成', replay[1])

    def test_existing_xiangrong_current_can_finish_application_without_new_school_row(self):
        handler = self.handler(form=dict(school_id='1081',local_admission='yes',school_confirmed='yes',
            start_date='2026-10-05',reason='Local school confirmed admission'))
        response = handler.student_school_enrolment_current('POST','20260112')
        self.assertIn('本次入学申请已完成',response[1])
        row = self.database.one("SELECT id,school_id FROM student_school_enrolments WHERE student_id='20260112'")
        self.assertEqual((self.current.enrolment_id,1081),tuple(row))
        self.assertEqual(('closed','completed'),tuple(self.database.one(
            "SELECT status,outcome FROM application_cycles WHERE cycle_uid='xiangrong-local'")))

    def test_school_plan_search_includes_local_school_after_first_thousand(self):
        from unittest.mock import patch
        handler = self.handler()
        with patch.object(self.app, 'can_access_module', return_value=True), \
             patch.object(self.app, 'page', side_effect=lambda title, body, *args: body), \
             patch.object(self.app.nas, 'status', return_value={}):
            handler.school_plan_report_create('GET')
        html = handler.captured['html']
        self.assertIn('MacGregor State School', html)
        self.assertIn('"id": 1081', html)

    def test_homepage_keeps_local_admission_and_management_without_current_shortcut(self):
        from types import SimpleNamespace
        html = self.app.student_detail_business_operations_html(
            SimpleNamespace(student_id='20260112',current_school=None), (), can_edit_profile=True)
        self.assertNotIn('>确认当前学校<', html)
        self.assertIn('确认入学（本地生）', html)
        self.assertIn('管理入读学校', html)

    def test_management_page_explains_future_and_started_dates_and_initial_promotion(self):
        from student_lifecycle.workbench import EnrolmentWorkbenchContext, render_enrolment_workbench
        context = EnrolmentWorkbenchContext('20260112',None,None,1,None,'initial',None)
        html = render_enrolment_workbench(context,csrf='x',school_choices=(),local_admission=True)
        self.assertIn('开始日期在未来时登记为待入读学校', html)
        self.assertIn('name="school_confirmed"',html)
        future = dict(school_id=1081,school_name_snapshot='MacGregor State School',
                      start_date='2035-01-29',lifecycle_context='initial')
        context = EnrolmentWorkbenchContext('20260112',None,future,None,None,None,None)
        html = render_enrolment_workbench(context,csrf='x',school_choices=())
        self.assertIn('确认开始入读（本地生）',html)
        self.assertIn('name="local_admission" value="yes"',html)
        self.assertIn('name="school_id" value="1081"',html)
        self.assertNotIn('>确认当前学校<',html)
        existing = dict(school_id=None,school_name_snapshot='MacGregor State School',
                        start_date='2026-10-05',lifecycle_context='initial')
        context = EnrolmentWorkbenchContext('20260112',existing,None,1,None,'initial',None)
        html = render_enrolment_workbench(context,csrf='x',school_choices=())
        self.assertIn('name="local_admission" value="yes"',html)
        self.assertIn('value="2026-10-05"',html)
        self.assertIn('value="MacGregor State School"',html)
