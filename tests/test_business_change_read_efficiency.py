import unittest
from unittest.mock import patch

from tests import test_business_change_case_snapshots as fixture
from business_changes.runtime import enabled
from business_changes.schema import require_schema
from visa_business_state import resolve_case_state
from visa_document_lifecycle import case_projection


class ReadSnapshotEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.VisaCaseSnapshotTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db
        self.case=dict(self.db.one('SELECT * FROM visa_cases WHERE id=?',(self.f.cid,)))

    def test_rollout_schema_is_checked_once_within_one_owned_read_snapshot(self):
        with patch('business_changes.runtime.require_schema',wraps=require_schema) as check:
            with self.db.read_snapshot() as connection:
                for _ in range(8):self.assertTrue(enabled(connection))
            self.assertEqual(1,check.call_count)
            with self.db.read_snapshot() as connection:self.assertTrue(enabled(connection))
            self.assertEqual(2,check.call_count)

    def test_case_authority_is_reused_but_returned_values_cannot_poison_the_snapshot(self):
        with patch('visa_document_lifecycle.case_projection',wraps=case_projection) as loader:
            with self.db.read_snapshot() as connection:
                first=resolve_case_state(connection,self.case);expected=dict(first)
                first['status']='corrupted'
                for _ in range(8):self.assertEqual(expected,resolve_case_state(connection,self.case))
            self.assertEqual(1,loader.call_count)
            with self.db.read_snapshot() as connection:resolve_case_state(connection,self.case)
            self.assertEqual(2,loader.call_count)

    def test_next_snapshot_observes_changed_rollout_state(self):
        with self.db.read_snapshot() as connection:self.assertTrue(enabled(connection))
        self.db.execute("UPDATE system_settings SET value='disabled' WHERE key='business_change_sync_v1'")
        with self.db.read_snapshot() as connection:self.assertFalse(enabled(connection))
