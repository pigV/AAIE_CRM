import json
import sqlite3
import unittest
from tests import test_production_auth_preservation as fixtures
from tools.production_release_transaction import ReleaseSnapshot


class GatedSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.AuthPreservationTests();self.fixture.setUp()
        self.root=self.fixture.dir;self.db=self.fixture.c
        self.backups=self.root/'backups';self.backups.mkdir()
        self.base=ReleaseSnapshot.create(self.db,self.backups/'release',backup_root=self.backups,assert_quiescent=lambda:None)
        self.closed=True

    def gate(self):
        if not self.closed:raise RuntimeError('gate_not_closed')

    def test_restore_preserves_committed_logout_and_new_login(self):
        from tools.production_gated_snapshot import AuthPreservingSnapshot
        self.fixture.mutate('DELETE FROM sessions');self.fixture.audit('logout')
        snapshot=AuthPreservingSnapshot(self.base,assert_gate=self.gate,extra_tables=lambda:{})
        snapshot.restore(assert_quiescent=lambda:None)
        with sqlite3.connect(self.db) as c:
            self.assertEqual(0,c.execute('SELECT count(*) FROM sessions').fetchone()[0])
            self.assertEqual(2,c.execute('SELECT count(*) FROM audit_logs').fetchone()[0])
            self.assertEqual(1,c.execute('SELECT count(*) FROM business').fetchone()[0])
        self.assertTrue((self.backups/'release/failed-state/current.sqlite3').exists())
        with self.assertRaises(Exception):snapshot.restore(assert_quiescent=lambda:None)

    def test_open_gate_and_business_delta_never_replace_database(self):
        from tools.production_gated_snapshot import AuthPreservingSnapshot
        snapshot=AuthPreservingSnapshot(self.base,assert_gate=self.gate,extra_tables=lambda:{})
        self.closed=False
        with self.assertRaises(RuntimeError):snapshot.restore(assert_quiescent=lambda:None)
        self.closed=True;self.fixture.mutate("UPDATE business SET value='new'")
        with self.assertRaises(Exception):snapshot.restore(assert_quiescent=lambda:None)
        with sqlite3.connect(self.db) as c:self.assertEqual('new',c.execute('SELECT value FROM business').fetchone()[0])

    def test_no_application_attempt_can_reverse_failed_migration(self):
        from tools.production_gated_snapshot import AuthPreservingSnapshot
        self.fixture.mutate("UPDATE business SET value='failed-migration'")
        snapshot=AuthPreservingSnapshot(self.base,assert_gate=self.gate,extra_tables=lambda:{},application_started=lambda:False)
        snapshot.restore(assert_quiescent=lambda:None)
        with sqlite3.connect(self.db) as c:self.assertEqual('unchanged',c.execute('SELECT value FROM business').fetchone()[0])

    def test_unknown_application_state_never_uses_plain_restore(self):
        from tools.production_gated_snapshot import AuthPreservingSnapshot
        self.fixture.mutate("UPDATE business SET value='unexplained'")
        snapshot=AuthPreservingSnapshot(self.base,assert_gate=self.gate,extra_tables=lambda:{},application_started=lambda:None)
        with self.assertRaises(ValueError):snapshot.restore(assert_quiescent=lambda:None)
        with sqlite3.connect(self.db) as c:self.assertEqual('unexplained',c.execute('SELECT value FROM business').fetchone()[0])


if __name__=='__main__':unittest.main()
