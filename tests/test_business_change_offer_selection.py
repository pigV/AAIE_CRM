import unittest
from unittest.mock import patch
from tests import test_offer_acceptance as fixture
from business_changes.schema import install
from offer_acceptance import assign_offer_school,set_accepted_offer,withdraw_accepted_offer


class OfferSelectionSyncTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.OfferAcceptanceFixture();self.f.setUp();self.addCleanup(self.f.tearDown);self.c=self.f.connection
        self.c.execute('PRAGMA foreign_keys=ON');self.c.execute('BEGIN');install(self.c)
        self.c.execute('CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT)')
        self.c.execute("INSERT INTO system_settings VALUES('business_change_sync_v1','enabled')")
    def count(self):return self.c.execute('SELECT count(*) FROM business_change_events').fetchone()[0]
    def test_classify_accept_retry_and_withdraw_publish_real_changes(self):
        assign_offer_school(self.c,101,11,self.f.actor)
        for _ in range(2):set_accepted_offer(self.c,'S001',101,self.f.actor,occurred_at='2026-11-19T10:00:00+10:00')
        self.assertEqual(2,self.count())
        withdraw_accepted_offer(self.c,'S001',self.f.actor,reason='Synthetic staff withdrawal',occurred_at='2026-11-19T11:00:00+10:00')
        self.assertEqual(3,self.count())
    def test_event_failure_rolls_back_legacy_selection_and_history(self):
        assign_offer_school(self.c,101,11,self.f.actor)
        with patch('business_changes.domain.execute_command',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):set_accepted_offer(self.c,'S001',101,self.f.actor)
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM student_offer_acceptances').fetchone()[0])
        self.assertEqual(0,self.c.execute('SELECT count(*) FROM offer_acceptance_history').fetchone()[0])
    def test_wrong_student_never_advances_other_student_scope(self):
        with self.assertRaises(ValueError):set_accepted_offer(self.c,'S001',201,self.f.actor)
        self.assertEqual(0,self.count())
