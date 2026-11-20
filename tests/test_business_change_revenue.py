import unittest
from unittest.mock import patch
from tests.revenue_test_support import RevenueDatabaseFixture
from revenue.repository import RevenueRepository
from revenue.services import RevenueService
from business_changes.schema import install
from business_changes.contracts import Subject
from business_changes.versions import version_of


class RevenueSyncTests(RevenueDatabaseFixture,unittest.TestCase):
    def setUp(self):
        super().setUp();self.c=self.connect();self.c.execute('BEGIN');install(self.c)
        self.c.execute('CREATE TABLE IF NOT EXISTS system_settings(key TEXT PRIMARY KEY,value TEXT)')
        self.c.execute("INSERT OR REPLACE INTO system_settings(key,value) VALUES('business_change_sync_v1','enabled')")
        self.c.commit();self.service=RevenueService(RevenueRepository(self.c,now=lambda:'2026-11-19T00:00:00+00:00'))
    def open(self):return self.service.open_case(100,actor_user_id=1,idempotency_key='synthetic-open-100')
    def test_canonical_replay_has_one_finance_receipt_outbox_and_change(self):
        first=self.open();self.assertEqual(first,self.open())
        for table in ('revenue_idempotency_receipts','revenue_outbox_events','business_change_events'):
            self.assertEqual(1,self.c.execute('SELECT count(*) FROM '+table).fetchone()[0],table)
        self.assertEqual(1,version_of(self.c,Subject('module','revenue')))
        self.assertEqual(0,version_of(self.c,Subject('dashboard','workflow')))
    def test_publish_failure_rolls_back_finance_source_receipt_and_outbox(self):
        with patch('business_changes.revenue.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.open()
        self.c.commit()
        for table in ('revenue_commercial_cases','revenue_idempotency_receipts','revenue_outbox_events'):
            self.assertEqual(0,self.c.execute('SELECT count(*) FROM '+table).fetchone()[0],table)
    def test_outer_transaction_owns_finance_and_change_commit(self):
        self.c.execute('BEGIN IMMEDIATE');self.open();self.c.rollback()
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM revenue_commercial_cases').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])

    def test_revenue_command_can_nest_in_guarded_business_command(self):
        from business_changes.commands import Command,Mutation,Outcome,execute_command
        from business_changes.registry import Registry
        self.c.execute('BEGIN IMMEDIATE');source=Subject('application_cycle','100')
        def operation(view):
            RevenueService(RevenueRepository(view,now=lambda:'2026-11-19T00:00:00+00:00')).open_case(100,actor_user_id=1,idempotency_key='synthetic-nested-open')
            return Outcome({'changed':True},(Mutation('cycle.finance_observed',source),))
        execute_command(self.c,Command('nested_finance','user','1','synthetic',{}),registry=Registry([]),
            authorize=lambda c:None,operation=operation,expected_versions={source:0},occurred_at='2026-11-19T00:00:00+00:00')
        self.assertEqual(2,self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0])
        self.c.rollback();self.assertEqual(0,self.c.execute('SELECT count(*) FROM revenue_commercial_cases').fetchone()[0])
