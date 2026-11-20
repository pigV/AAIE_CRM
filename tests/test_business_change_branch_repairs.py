"""Regression tests for refused results and secure parent-link revisions."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_business_change_school_chain as fixture
from business_changes.contracts import Subject
from business_changes.versions import version_of


class BranchRepairTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.SchoolMaterialChainTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db;self.sid=self.f.sid;self.cycle=self.f.cycle

    def refusal(self):
        from visa_result_status import record_manual_result
        case=self.db.execute("""INSERT INTO visa_cases(student_id,application_cycle_id,visa_case_name,visa_type,
            applicant_name,application_type,status,result_status,created_by,created_at,updated_at)
            VALUES(?,?,'Synthetic refusal','500 Student Visa','Synthetic','New Application','Lodged','Pending',1,?,?)""",(self.sid,self.cycle,self.f.stamp,self.f.stamp))
        with self.db.transaction() as c:
            record_manual_result(c,case,'Refused',actor_user_id=1,occurred_at=self.f.stamp,reason='Synthetic reviewed refusal')
        return case

    def test_refused_active_flag_uses_exact_cycle_terminal_result_on_student_and_list(self):
        from student_workflow_projection import load_student_workflow_projection_map
        from student_application_stage_distribution import load_application_stage_result_map,load_application_stage_distribution
        from student_management_summary import load_student_management_summary_map
        self.refusal()
        with self.db.read_snapshot() as c:
            for cycle in (None,self.cycle):
                state=load_student_workflow_projection_map(c,[self.sid],cycle_id=cycle)[self.sid]
                self.assertEqual(('visa_refused','已拒签'),(state.stage_key,state.stage_label))
            self.assertEqual('已拒签',load_application_stage_result_map(c,[self.sid])[self.sid].label)
            self.assertEqual('已拒签',load_student_management_summary_map(c,[self.sid])[self.sid].stage_label)
            self.assertFalse([r for r in load_application_stage_distribution(c,cycles_only=True) if r.cycle_id==self.cycle])
        self.assertEqual('active',self.db.one('SELECT status FROM application_cycles WHERE id=?',(self.cycle,))[0],
            'Read adapters must not mutate the historical cycle flag')

    def test_refused_older_cycle_cannot_override_new_cycle(self):
        from application_cycle import create_student_cycle
        from student_workflow_projection import load_student_workflow_projection_map
        self.refusal()
        with self.db.transaction() as c:
            c.execute("UPDATE application_cycles SET status='closed',outcome='refused',closed_at=? WHERE id=?",(self.f.stamp,self.cycle))
            fresh=create_student_cycle(c,self.sid,'initial',self.f.stamp,1).id
        with self.db.read_snapshot() as c:
            state=load_student_workflow_projection_map(c,[self.sid])[self.sid]
        self.assertEqual(fresh,state.cycle_id)
        self.assertNotEqual('visa_refused',state.stage_key)

    def test_existing_visa_record_is_not_proof_of_grant(self):
        from student_management_summary import _management_stage
        stage=SimpleNamespace(key=None,label='',index=0)
        state=SimpleNamespace(state='error',cycle_type='initial')
        for outcome in ('','refused','withdrawn'):
            with self.subTest(outcome=outcome):
                self.assertNotEqual('granted',_management_stage(stage,state,True,outcome,True)[0])

    def test_current_projection_error_is_not_replaced_by_a_historical_grant(self):
        from student_management_summary import _management_stage
        stage=SimpleNamespace(key='workflow_error',label='申请阶段异常',index=999)
        current=SimpleNamespace(state='error',cycle_type='initial',stage_key='workflow_error',stage_label='申请阶段异常',stage_order=999)
        self.assertEqual(('workflow_error','申请阶段异常'),_management_stage(stage,current,True,'granted',True)[:2])

    def test_current_review_state_is_not_replaced_by_a_historical_grant(self):
        from student_management_summary import _management_stage
        stage=SimpleNamespace(key='visa_result_needs_review',label='签证材料需核实',index=99)
        current=SimpleNamespace(state='needs_review',cycle_type='initial',stage_key=stage.key,stage_label=stage.label,stage_order=99)
        self.assertEqual((stage.key,stage.label),_management_stage(stage,current,True,'granted',True)[:2])

    def test_later_entered_historical_case_does_not_hide_current_refusal(self):
        from application_cycle import create_student_cycle
        from student_workflow_projection import load_student_workflow_projection_map
        self.refusal()
        with self.db.transaction() as c:
            # Explicit historical-data fixture, not a simulated user transition.
            c.execute("UPDATE application_cycles SET status='closed',outcome='refused',closed_at=? WHERE id=?",(self.f.stamp,self.cycle))
            historic=create_student_cycle(c,self.sid,'initial',self.f.stamp,1).id
            c.execute("UPDATE application_cycles SET status='closed',outcome='completed',closed_at=? WHERE id=?",(self.f.stamp,historic))
            c.execute("UPDATE application_cycles SET status='active',outcome=NULL,closed_at=NULL WHERE id=?",(self.cycle,))
            c.execute("""INSERT INTO visa_cases(student_id,application_cycle_id,visa_case_name,visa_type,
                applicant_name,application_type,status,result_status,created_by,created_at,updated_at)
                VALUES(?,?,'Later historical import','500 Student Visa','Synthetic','New Application',
                       'Information Collection','Pending',1,?,?)""",(self.sid,historic,self.f.stamp,self.f.stamp))
        with self.db.read_snapshot() as c:
            for cycle in (None,self.cycle):
                result=load_student_workflow_projection_map(c,[self.sid],cycle_id=cycle)[self.sid]
                self.assertEqual((self.cycle,'visa_refused'),(result.cycle_id,result.stage_key))

    def links(self):
        from application_withdrawal import withdraw_application
        from parent_form_repository import ParentFormRepository
        with self.db.transaction() as c:
            withdraw_application(c,student_id=self.sid,cycle_id=self.cycle,actor_id=1,actor_name='Synthetic',
                reason='Synthetic withdrawal',stage='Contract',timestamp=self.f.stamp)
        return ParentFormRepository(self.db)

    def versions(self):
        with self.db.read_snapshot() as c:
            return {s:version_of(c,s) for s in (Subject('student',self.sid),Subject('dashboard','workflow'),Subject('dashboard','overview'))}

    def create(self,repository):
        return repository.create_link('synthetic-secret-branch-token',1,'Synthetic','2099-01-01T00:00:00+11:00',
            1,self.f.stamp,student_id=self.sid,intake_type='ordinary_new_application')

    def test_create_regenerate_and_revoke_publish_student_and_both_dashboard_versions(self):
        repository=self.links();before=self.versions()
        self.create(repository)
        after=self.versions();self.assertEqual({s:v+1 for s,v in before.items()},after)
        repository.regenerate_link('synthetic-secret-branch-token','synthetic-secret-replacement',
            '2099-01-01T00:00:00+11:00',1,self.f.stamp,intake_type='ordinary_new_application')
        newer=self.versions();self.assertEqual({s:v+1 for s,v in after.items()},newer)
        self.assertTrue(repository.invalidate_if_writable('synthetic-secret-replacement',self.f.stamp))
        final=self.versions();self.assertEqual({s:v+1 for s,v in newer.items()},final)
        self.assertFalse(repository.invalidate_if_writable('synthetic-secret-replacement',self.f.stamp))
        self.assertEqual(final,self.versions())
        with self.db.read_snapshot() as c:
            for table in ('business_change_commands','business_change_events','business_change_impacts'):
                self.assertNotIn('synthetic-secret-',repr([tuple(r) for r in c.execute('SELECT * FROM '+table)]))

    def test_publication_failure_rolls_back_link_and_cycle(self):
        repository=self.links();before=self.versions()
        with patch('business_changes.commands.execute_command',side_effect=RuntimeError('synthetic publisher failure')):
            with self.assertRaises(RuntimeError):self.create(repository)
        self.assertIsNone(repository.get_link('synthetic-secret-branch-token'))
        self.assertEqual(before,self.versions())
        self.assertEqual(1,self.db.one('SELECT count(*) FROM application_cycles WHERE student_id=?',(self.sid,))[0])
