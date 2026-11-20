"""Use the real HTTP handler boundary on an isolated full-schema database."""
import unittest
from unittest.mock import patch
from tests import test_business_change_school_chain as fixture
from business_changes.contracts import Subject
from business_changes.versions import version_of

class StageEvidenceSyncTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.SchoolMaterialChainTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db
        self.f.h.form={'kind':'contract','decision':'optional','reason':'SYNTHETIC confirmed contract policy'}

    def versions(self):
        with self.db.read_snapshot() as c:
            return [version_of(c,s) for s in (Subject('student',self.f.sid),Subject('application_cycle',str(self.f.cycle)),
                Subject('dashboard','workflow'),Subject('dashboard','overview'))]

    def write(self):return self.f.h.student_cycle_stage_evidence('POST',self.f.sid,self.f.cycle)

    def test_native_stage_evidence_notifies_student_and_both_home_regions(self):
        before=self.versions();result=self.write()
        self.assertEqual('success',result[2],result)
        self.assertEqual([v+1 for v in before],self.versions())

    def test_failed_publication_rolls_back_native_evidence(self):
        before=self.db.one("SELECT count(*) FROM audit_logs WHERE action_type='cycle_contract_requirement_confirmed'")[0]
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('SYNTHETIC publication failure')):
            with self.assertRaises(RuntimeError):self.write()
        self.assertEqual(before,self.db.one("SELECT count(*) FROM audit_logs WHERE action_type='cycle_contract_requirement_confirmed'")[0])
