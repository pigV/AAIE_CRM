import unittest
from business_changes.schema import install
from revenue.operational_facts import RevenueOperationalFacts
from revenue.operational_orchestrator import RevenueOperationalOrchestrator
from tests.revenue_test_support import RevenueDatabaseFixture


class RevenueCoeVersionTests(RevenueDatabaseFixture,unittest.TestCase):
    def prepare(self):
        conn=self.connect();self.seed_component_authorities(conn)
        conn.execute('ALTER TABLE coe_oshc ADD COLUMN coe_course TEXT')
        conn.execute('ALTER TABLE coe_oshc ADD COLUMN created_at TEXT')
        conn.execute("UPDATE coe_oshc SET coe_course='Synthetic first',created_at='2026-11-19' WHERE id=202")
        conn.execute('CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT)')
        conn.execute("INSERT INTO system_settings VALUES('business_change_sync_v1','enabled')")
        install(conn)
        return conn

    def test_same_created_date_does_not_freeze_updated_authoritative_coe_fact(self):
        conn=self.prepare();facts=RevenueOperationalFacts(conn)
        before=facts.coe(202)
        conn.execute("UPDATE coe_oshc SET coe_course='Synthetic second' WHERE id=202")
        after=facts.coe(202)
        self.assertNotEqual(before.version,after.version)
        self.assertEqual('Synthetic second',after.course)
        self.assertEqual(after.version,facts.coe(202).version)

    def test_finance_replay_keeps_original_receipt_and_new_fact_gets_new_receipt(self):
        conn=self.prepare();orchestrator=RevenueOperationalOrchestrator(conn,1,'Admin')
        before=orchestrator.coe_changed(202)
        conn.execute("UPDATE coe_oshc SET coe_course='Synthetic second' WHERE id=202")
        after=orchestrator.coe_changed(202)
        self.assertEqual('Synthetic first',before['authoritative_fact']['course'])
        self.assertEqual('Synthetic second',after['authoritative_fact']['course'])
        self.assertEqual(after,orchestrator.coe_changed(202))
        self.assertEqual(2,conn.execute("SELECT count(*) FROM revenue_idempotency_receipts WHERE idempotency_key LIKE 'revenue-operational:coe:%'").fetchone()[0])
