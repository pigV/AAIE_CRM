import unittest
import sqlite3
from unittest.mock import patch
from tests import test_dashboard_canonical_offer_acceptance as fixture_module
from business_changes.schema import install
from business_changes.versions import version_of
from business_changes.contracts import Subject
from workflow_offer_events import record_offer_received, record_offer_state, select_final_acceptance, change_final_acceptance, withdraw_final_acceptance, record_final_school_submission

NOW=fixture_module.NOW
LATER=fixture_module.LATER


class OfferBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture=fixture_module.DashboardCanonicalOfferAcceptanceTests();cls.fixture.setUp()
        cls.addClassCleanup(cls.fixture.tearDown)
        install(cls.fixture.conn)
        cls.fixture.conn.execute('''CREATE TABLE IF NOT EXISTS audit_logs(
            id INTEGER PRIMARY KEY,module_name TEXT,action_type TEXT,record_id TEXT,
            old_value TEXT,new_value TEXT,status TEXT,student_id TEXT)''')
        cls.fixture.conn.execute("INSERT OR REPLACE INTO system_settings(key,value) VALUES('business_change_sync_v1','enabled')")
        cls.fixture.conn.commit()

    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
        self.addCleanup(self.c.close);self.fixture.conn.backup(self.c)
        self.c.execute('PRAGMA foreign_keys=ON');self.c.execute('BEGIN')
        self.cycle=self.fixture.cycle.id;self.offers=self.fixture.offers

    def count(self):return self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0]

    def select(self,**kw):
        return select_final_acceptance(self.c,self.cycle,self.offers[0].id,selected_at=NOW,actor_user_id=1,**kw)

    def test_new_offer_and_retry_emit_once_and_preserve_return_type(self):
        kw=dict(private_application_id=self.fixture.private_ids[0],external_offer_key='extra',school_name='School A',received_at=NOW,actor_user_id=1)
        first=record_offer_received(self.c,self.cycle,**kw);second=record_offer_received(self.c,self.cycle,**kw)
        self.assertTrue(first.created);self.assertFalse(second.created);self.assertEqual(first.event,second.event)
        self.assertEqual(1,self.count())
        self.assertEqual(1,version_of(self.c,Subject('dashboard','overview')))

    def test_acceptance_noop_and_evidence_updates(self):
        a=self.select();self.select();self.assertEqual(1,self.count())
        self.select(signed_offer_completed_at=NOW,payment_completed_at=NOW)
        self.assertEqual(2,self.count())
        result=record_final_school_submission(self.c,a.id,submitted_at=LATER,submission_channel='email',submission_reference='SYNTHETIC',actor_user_id=1)
        again=record_final_school_submission(self.c,a.id,submitted_at=LATER,submission_channel='email',submission_reference='SYNTHETIC',actor_user_id=1)
        self.assertTrue(result.created);self.assertFalse(again.created);self.assertEqual(3,self.count())

    def test_change_and_withdraw_include_previous_acceptance(self):
        old=self.select()
        new=change_final_acceptance(self.c,self.cycle,self.offers[1].id,occurred_at=LATER,actor_user_id=1)
        impacts=self.c.execute("SELECT subject_id,relation FROM business_change_impacts WHERE event_seq=(SELECT max(seq) FROM business_change_events) AND subject_kind='acceptance'").fetchall()
        self.assertIn((str(old.id),'previous'),[tuple(x) for x in impacts]);self.assertIn((str(new.id),'source'),[tuple(x) for x in impacts])
        withdraw_final_acceptance(self.c,self.cycle,occurred_at=LATER,actor_user_id=1)
        self.assertEqual(3,self.count())

    def test_void_offer_and_duplicate_do_not_repeat_event(self):
        for _ in range(2):record_offer_state(self.c,self.offers[0].id,state='VOID',occurred_at=NOW,actor_user_id=1)
        self.assertEqual(1,self.count())

    def test_sync_failure_rolls_back_canonical_offer_even_if_caught(self):
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.select()
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM cycle_final_acceptances').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM cycle_final_acceptance_history').fetchone()[0])
