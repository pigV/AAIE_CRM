import sqlite3
import unittest
from unittest.mock import patch
from tests import test_transfer_v2_admissions as fixture
from tests import test_transfer_v2_creation as creation_fixture
from business_changes.schema import install
from student_lifecycle.transfer_v2_admissions import TransferV2AdmissionsService
from student_lifecycle.transfer_v2_service import TransferV2Service
from student_lifecycle.transfer_v2_models import TransferV2TargetInput


def enable(path):
    c=sqlite3.connect(path);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');c.execute('BEGIN');install(c)
    c.execute("INSERT OR REPLACE INTO system_settings(key,value,updated_at) VALUES('business_change_sync_v1','enabled','2026-11-19T00:00:00+00:00')");c.commit();return c


class TransferAdmissionsSyncTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.TransferV2AdmissionsTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.c=enable(self.f.database_path);self.addCleanup(self.c.close)
        self.service=TransferV2AdmissionsService(self.f.database_path,clock=lambda:fixture.NOW)
    def kw(self):return dict(cycle_id=self.f.overview.cycle_id,branch_id=self.f.branch.branch_id,offer_event_id=self.f.offer_new,actor_user_id=1,idempotency_key='synthetic-shared-event')
    def test_admissions_receipt_and_shared_event_are_not_duplicated(self):
        first=self.service.confirm_offer_valid(**self.kw());self.assertEqual(first,self.service.confirm_offer_valid(**self.kw()))
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
    def test_event_failure_rolls_back_offer_state_and_receipt(self):
        before={t:self.c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('offer_received_event_state_events','transfer_v2_command_receipts','transfer_target_state_events')}
        with patch('business_changes.transfer.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.service.confirm_offer_valid(**self.kw())
        for table,count in before.items():self.assertEqual(count,self.c.execute('SELECT count(*) FROM '+table).fetchone()[0],table)
    def test_new_target_and_retry_publish_once(self):
        service=TransferV2Service(self.f.database_path,clock=lambda:fixture.NOW)
        kw=dict(target=TransferV2TargetInput(None,'Synthetic Added School'),actor_user_id=1,idempotency_key='shared-added-school')
        service.add_target_branch(self.f.overview.cycle_id,**kw);service.add_target_branch(self.f.overview.cycle_id,**kw)
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])


    def test_same_transfer_publication_replays_even_after_cycle_revision_advanced(self):
        from business_changes.transfer import record_transfer_change
        self.c.execute('BEGIN IMMEDIATE')
        kw=dict(event_type='transfer.branch_observed',cycle_id=self.f.overview.cycle_id,actor_user_id=1,
            idempotency_key='same-published-transfer-fact',stamp=fixture.NOW)
        first=record_transfer_change(self.c,**kw);second=record_transfer_change(self.c,**kw)
        self.assertFalse(first.replayed);self.assertTrue(second.replayed)
        self.assertEqual(1,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])


class TransferCreationSyncTests(unittest.TestCase):
    def setUp(self):
        self.f=creation_fixture.TransferV2CreationTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.c=enable(self.f.database_path);self.addCleanup(self.c.close)
    def test_new_transfer_cycle_and_replay_publish_once(self):
        first=self.f.service.create_cycle(self.f.request,actor_user_id=1,idempotency_key='synthetic-create-shared')
        self.f.service.create_cycle(self.f.request,actor_user_id=1,idempotency_key='synthetic-create-shared')
        self.assertEqual(1,self.c.execute("SELECT count(*) FROM business_change_events WHERE event_type='transfer.created'").fetchone()[0])
