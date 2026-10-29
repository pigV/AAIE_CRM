from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from tools.production_gated_release_plan import GatedReleasePlan


class AttemptSealTests(unittest.TestCase):
    def plan(self):
        root=Path(__file__).resolve().parents[1]/'SAFEBOX/current-gate-20260909/write-gate/plan-tests'
        root.mkdir(parents=True,exist_ok=True)
        plan=object.__new__(GatedReleasePlan)
        plan.delegate=SimpleNamespace(evidence_dir=Path(tempfile.mkdtemp(dir=root)))
        plan.assert_gate=lambda:None;plan._extra=lambda:{}
        plan.extra_hash='a'*64;plan.application_attempted=False
        return plan

    def test_parent_then_admin_share_sealed_start_boundary(self):
        plan=self.plan();plan.mark_application_attempt()
        path=plan.evidence_dir/'APPLICATION_START_ATTEMPT.json';before=path.read_bytes()
        plan.mark_application_attempt()
        self.assertEqual(before,path.read_bytes());self.assertTrue(plan.application_attempted)

    def test_corrupt_or_preexisting_marker_cannot_be_adopted(self):
        plan=self.plan();plan.mark_application_attempt()
        path=plan.evidence_dir/'APPLICATION_START_ATTEMPT.json';path.write_text('{}')
        with self.assertRaises(Exception):plan.mark_application_attempt()

    def test_only_exact_old_and_039_migration_contracts_are_accepted(self):
        target=SimpleNamespace()
        contracts=((('20260909_037','20260909_038'),(),False),
                   (('20260914_039',),('20260909_037','20260909_038'),True))
        with patch('tools.production_gated_release_plan.AuthPreservingSnapshot',side_effect=lambda snapshot,**kw:snapshot):
            for versions,predecessors,noop in contracts:
                delegate=SimpleNamespace(target=target,snapshot=object(),migration_step=SimpleNamespace(
                    versions=versions,required_predecessors=predecessors,allow_already_applied=noop))
                self.assertIsInstance(GatedReleasePlan(delegate,assert_gate=lambda:None),GatedReleasePlan)
            for step in (None,SimpleNamespace(versions=('20260914_039',),required_predecessors=(),allow_already_applied=True),
                         SimpleNamespace(versions=('20260915_040',),required_predecessors=('20260914_039',),allow_already_applied=True)):
                delegate=SimpleNamespace(target=target,snapshot=object(),migration_step=step)
                with self.assertRaisesRegex(ValueError,'gated_visa_versions_required'):GatedReleasePlan(delegate,assert_gate=lambda:None)

    def _039_cutover(self,*,already=False,missing=False):
        import sqlite3
        directory=self.plan().evidence_dir;backup=directory/'backup.sqlite3';database=directory/'current.sqlite3'
        with sqlite3.connect(backup) as c:c.execute('CREATE TABLE baseline(id INTEGER PRIMARY KEY)')
        database.write_bytes(backup.read_bytes())
        previous=('20260909_037','20260909_038')+((('20260914_039',) if already else ()))
        def cutover():
            if already:return
            with sqlite3.connect(database) as c:
                c.execute('CREATE TABLE visa_result_status_events(id INTEGER PRIMARY KEY)')
                if not missing:c.execute('CREATE TABLE visa_result_status_current(id INTEGER PRIMARY KEY)')
        step=SimpleNamespace(expected_registry=lambda before:before if already else before+('20260914_039',))
        delegate=SimpleNamespace(evidence_dir=directory,snapshot=SimpleNamespace(backup=backup,database=database),
            target=SimpleNamespace(previous_migrations=previous),migration_step=step,cutover=cutover)
        plan=object.__new__(GatedReleasePlan);plan.delegate=delegate;plan.assert_gate=lambda:None
        plan.allowed_new_tables=frozenset(('visa_result_status_events','visa_result_status_current'))
        plan.extra={};plan.extra_seal=None;plan.extra_hash=None
        return plan

    def test_039_cutover_seals_exact_two_new_tables(self):
        plan=self._039_cutover();plan.cutover()
        self.assertEqual(set(plan.extra),{'visa_result_status_events','visa_result_status_current'})

    def test_039_already_applied_cutover_seals_empty_extra_set(self):
        plan=self._039_cutover(already=True);plan.cutover();self.assertEqual(plan.extra,{})

    def test_039_partial_table_creation_fails_closed(self):
        plan=self._039_cutover(missing=True)
        with self.assertRaisesRegex(RuntimeError,'gated_migration_table_set_mismatch'):plan.cutover()
        plan.application_attempted=False
        with self.assertRaises(Exception):plan.mark_application_attempt()


if __name__=='__main__':unittest.main()
