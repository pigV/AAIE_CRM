import sqlite3
import unittest
from unittest.mock import patch

from tests import test_transfer_v2_projection as fixture
from business_changes.schema import install
from business_changes.contracts import Subject
from business_changes.versions import version_of


class TransferDocumentSyncTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.TransferV2ProjectionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.c=sqlite3.connect(self.f.database_path);self.c.row_factory=sqlite3.Row;self.addCleanup(self.c.close)
        self.c.execute('PRAGMA foreign_keys=ON');self.c.execute('BEGIN');install(self.c)
        self.c.execute("INSERT OR REPLACE INTO system_settings(key,value,updated_at) VALUES('business_change_sync_v1','enabled',?)",(fixture.NOW,));self.c.commit()

    def version(self):return version_of(self.c,Subject('dashboard','workflow'))
    def transition(self):
        return self.f.documents.transition_branch(cycle_id=self.f.overview.cycle_id,branch_id=self.f.branch.branch_id,
            new_state='application_in_progress',actor_user_id=1,allow_missing=False,note=None,idempotency_key='sync-branch')

    def test_branch_status_publishes_once_and_failed_publication_rolls_back(self):
        before=self.c.execute('SELECT count(*) FROM transfer_target_state_events').fetchone()[0]
        with patch('business_changes.transfer.execute_command',side_effect=RuntimeError('synthetic publication failure')):
            with self.assertRaises(RuntimeError):self.transition()
        self.assertEqual(before,self.c.execute('SELECT count(*) FROM transfer_target_state_events').fetchone()[0])
        self.transition();self.assertEqual(1,self.version())
        self.transition();self.assertEqual(1,self.version())

    def test_document_requirement_binding_adoption_and_invalidation_publish_once(self):
        cycle=self.f.overview.cycle_id;branch=self.f.branch.branch_id
        self.c.execute("INSERT INTO student_documents VALUES(901,'S1','Transcripts','/synthetic/transcript.pdf',?,'2026-08-27')",(fixture.NOW,));self.c.commit()
        service=self.f.documents
        requirement=service.add_requirement(cycle_id=cycle,branch_id=branch,document_type='Transcripts',label='Synthetic transcript',
            requires_current_cycle=True,actor_user_id=1,idempotency_key='sync-requirement')
        self.assertEqual(1,self.version())
        binding=service.bind_document(cycle_id=cycle,branch_id=branch,document_id=901,source_cycle_id=cycle,
            document_type='Transcripts',document_date='2026-08-27',actor_user_id=1,idempotency_key='sync-binding')
        self.assertEqual(2,self.version())
        service.adopt_document(requirement_id=requirement.requirement_id,binding_id=binding.binding_id,actor_user_id=1,idempotency_key='sync-adopt')
        self.assertEqual(3,self.version())
        service.adopt_document(requirement_id=requirement.requirement_id,binding_id=binding.binding_id,actor_user_id=1,idempotency_key='sync-adopt')
        self.assertEqual(3,self.version())
        service.invalidate_binding(binding_id=binding.binding_id,actor_user_id=1,reason='Synthetic updated transcript',idempotency_key='sync-invalid')
        self.assertEqual(4,self.version())
        service.invalidate_binding(binding_id=binding.binding_id,actor_user_id=1,reason='Synthetic updated transcript',idempotency_key='sync-invalid')
        self.assertEqual(4,self.version())
        self.assertIsNone(service.current_adopted(requirement.requirement_id))

    def test_transfer_completion_notifies_student_and_home_in_original_transaction(self):
        self.f.test_literal_main_stage_sequence_keeps_paid_before_waiting_coe()
        types={r[0] for r in self.c.execute('SELECT event_type FROM business_change_events')}
        self.assertIn('transfer.application_completed',types)
        self.assertIn('transfer.completed',types)
        self.assertEqual('closed',self.c.execute('SELECT status FROM application_cycles WHERE id=?',(self.f.overview.cycle_id,)).fetchone()[0])
        self.assertGreater(version_of(self.c,Subject('student','S1')),0)
