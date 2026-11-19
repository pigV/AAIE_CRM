"""Empty application shells must not conceal actual student Visa facts."""
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from student_workflow_projection import load_student_workflow_projection_map, _visa_fact_fallbacks


class StudentVisaFactFallbackTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE application_cycles(id INTEGER,student_id TEXT,cycle_type TEXT);
            CREATE TABLE visa_cases(id INTEGER,student_id TEXT,application_cycle_id INTEGER,
                case_type TEXT,status TEXT,result_status TEXT,actual_lodgement_date TEXT,
                archived_at TEXT);
            CREATE TABLE visa_result_status_current(visa_case_id INTEGER,effective_result TEXT);
            CREATE TABLE audit_logs(id INTEGER,record_id TEXT,student_id TEXT,module_name TEXT,
                action_type TEXT,status TEXT,old_value TEXT);
            INSERT INTO application_cycles VALUES(73,'S1','initial'),(19,'S2','initial'),(52,'S3','initial'),(89,'S3','initial');
        ''')

    def add(self, case_id=30, student='S1', cycle=73, status='Lodged', result='Pending',
            lodged='2026-09-14', kind='500 Student Visa', archived=None):
        self.conn.execute('INSERT INTO visa_cases VALUES(?,?,?,?,?,?,?,?)',
                          (case_id,student,cycle,kind,status,result,lodged,archived))

    def facts(self):
        return _visa_fact_fallbacks(self.conn, ('S1','S2','S3'))

    def test_lodged_old_cycle_is_preserved(self):
        self.add()
        fact=self.facts()['S1']
        self.assertEqual((fact.stage_key,fact.cycle_id),('11_visa_result',73))

    def test_granted_closed_cycle_is_preserved(self):
        self.add(student='S2',cycle=19,status='Granted',result='Granted')
        self.assertEqual(self.facts()['S2'].stage_key,'visa_granted')

    def test_unbound_grant_never_guesses_one_of_two_cycles(self):
        self.add(student='S3',cycle=None,status='Granted',result='Granted',lodged=None)
        fact=self.facts()['S3']
        self.assertEqual(fact.stage_label,'已下签')
        self.assertIsNone(fact.cycle_id)
        self.assertIn('VISA_CASE_CYCLE_UNASSIGNED',fact.issue_codes)

    def test_guardian_archived_and_wrong_student_cycle_are_excluded(self):
        for values in ({'kind':'590 Guardian Visa'},{'archived':'2026-01-01'},{'cycle':19}):
            with self.subTest(values=values):
                self.conn.execute('DELETE FROM visa_cases')
                self.add(**values)
                self.assertEqual(self.facts(),{})

    def test_authoritative_pending_suppresses_stale_granted(self):
        self.add(status='Granted',result='Granted',lodged=None)
        self.conn.execute("INSERT INTO visa_result_status_current VALUES(30,'Pending')")
        self.assertEqual(self.facts(),{})

    def test_newer_preparation_case_suppresses_old_grant(self):
        self.add(status='Granted',result='Granted')
        self.add(case_id=31,status='Information Collection',result='Pending',lodged=None)
        self.assertEqual(self.facts(),{})

    def test_legacy_pending_without_lodgement_is_not_lodged(self):
        self.add(status='Pending',lodged=None)
        self.assertEqual(self.facts(),{})

    def test_formal_refusal_is_not_displayed_as_grant(self):
        self.add(status='Granted',result='Granted')
        self.conn.execute("INSERT INTO visa_result_status_current VALUES(30,'Refused')")
        self.assertEqual(self.facts()['S1'].stage_label,'已拒签')

    def projection(self, stage="no_counted_stage", *, cycle_id=None, held=False):
        self.conn.execute("CREATE TABLE students(student_id TEXT)")
        self.conn.execute("INSERT INTO students VALUES('S1')")
        for definition in ("status TEXT DEFAULT 'active'", "outcome TEXT", "sequence_no INTEGER DEFAULT 1"):
            self.conn.execute(f"ALTER TABLE application_cycles ADD COLUMN {definition}")
        self.conn.execute("CREATE TABLE student_cases(id INTEGER,student_id TEXT,status TEXT)")
        self.conn.execute("ALTER TABLE application_cycles ADD COLUMN source_student_case_id INTEGER")
        self.conn.execute("CREATE VIEW final_school_optional_steps AS SELECT 1")
        row = SimpleNamespace(student_id='S1',cycle_id=73,stage_number=0,stage_key=stage,
                              stage_title='Current',blocker=None,issue_codes=())
        confirmed = {73:None if stage=='no_counted_stage' else (stage,'')}
        module = 'student_workflow_projection.'
        with patch(module+'load_current_workflow_projection',return_value=(row,)), \
             patch(module+'load_confirmed_cycle_stages',return_value=confirmed), \
             patch(module+'held_case_cycles',return_value={73:'paused'} if held else {}), \
             patch(module+'load_student_transfer_state_map',return_value={}):
            return load_student_workflow_projection_map(self.conn,('S1',),cycle_id=cycle_id)['S1']

    def test_empty_profile_uses_actual_case(self):
        self.add()
        self.assertEqual(self.projection().stage_key,'11_visa_result')

    def test_explicit_cycle_view_does_not_inherit_other_cycle_visa(self):
        self.add()
        self.assertEqual(self.projection(cycle_id=73).stage_key,'no_counted_stage')

    def test_active_application_work_is_not_hidden_by_history(self):
        self.add(status='Granted',result='Granted')
        self.assertEqual(self.projection('07_school_response').stage_key,'07_school_response')

    def test_paused_cycle_is_not_replaced_with_historical_grant(self):
        self.add(status='Granted',result='Granted')
        self.assertEqual(self.projection(held=True).stage_label,'已暂停')

    def test_homepage_summary_keeps_unbound_canonical_result(self):
        from student_management_summary import _management_stage
        for result,label in (("Granted","已下签"),("Refused","已拒签"),("Withdrawn","签证申请已撤回")):
            with self.subTest(result=result):
                self.conn.execute('DELETE FROM visa_cases')
                self.add(student='S3',cycle=None,status=result,result=result,lodged=None)
                projection=self.facts()['S3']
                stage=SimpleNamespace(key='',label='',index=0)
                shown=_management_stage(stage,projection,True,'',False)
                self.assertEqual(shown[0],projection.stage_key)
                self.assertEqual(shown[1],label)

    def test_local_admission_completion_is_not_an_old_visa_grant(self):
        from student_management_summary import _management_stage
        stage = SimpleNamespace(key=None, label='', index=0)
        local = SimpleNamespace(state='closed', cycle_type='initial', outcome='completed',
                                stage_key='closed', stage_label='申请已结案', stage_order=100)
        self.assertEqual(_management_stage(stage, local, True, 'completed', True),
                         ('closed', '申请已结案', 100, 'initial'))
