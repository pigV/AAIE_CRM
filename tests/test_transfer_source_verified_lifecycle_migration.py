"""Append-only Migration 042 verified lifecycle contract."""
from pathlib import Path
import sqlite3
import unittest

from tests.test_transfer_source_authority_append_only_migration import fixture, MIGRATION as MIGRATION_041

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_042 = ROOT / "migrations/20260920_042_transfer_source_verified_lifecycle.sql"


class TransferSourceVerifiedLifecycleMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = fixture()
        self.addCleanup(self.db.close)
        self.db.executescript(MIGRATION_041.read_text(encoding="utf-8"))

    def test_upgrade_preserves_pending_and_consumed_without_backfill(self):
        self.db.execute("""INSERT INTO transfer_source_authorities(
            id,student_id,transfer_contract_id,parent_form_token,rule_version,created_by,created_at
        ) VALUES(1,'new',100,'new-intake','transfer-source-v1',1,'2026-09-06')""")
        self.db.executescript(MIGRATION_042.read_text(encoding="utf-8"))
        row = self.db.execute("""SELECT lifecycle_state,source_classification,
            application_cycle_id,verified_by,verified_at FROM transfer_source_authorities WHERE id=1""").fetchone()
        self.assertEqual(('pending', None, None, None, None), row)

    def test_upgrade_preserves_consumed_authority_fields_exactly(self):
        self.db.execute("""INSERT INTO transfer_source_authorities(
            id,student_id,transfer_contract_id,parent_form_token,rule_version,created_by,created_at
        ) VALUES(1,'new',100,'new-intake','transfer-source-v1',1,'2026-09-06')""")
        self.db.execute("INSERT INTO student_cases(id,student_id) VALUES(99,'new')")
        self.db.execute("""INSERT INTO application_cycles
            VALUES(99,'new','transfer','school_education','active',NULL,99)""")
        self.db.execute("""UPDATE contracts SET contract_status='FullyExecuted',application_cycle_id=99
            WHERE id=100""")
        self.db.execute("INSERT INTO transfer_cases VALUES(99,100,'new_transfer')")
        self.db.execute("""INSERT INTO transfer_v2_origin_events(id,application_cycle_id,new_source_label)
            VALUES(99,99,'new_transfer')""")
        self.db.execute("""UPDATE transfer_source_authorities
            SET lifecycle_state='consumed',source_classification='new_transfer',application_cycle_id=99,
                verified_by=1,verified_at='2026-09-07' WHERE id=1""")
        before = self.db.execute("SELECT * FROM transfer_source_authorities WHERE id=1").fetchone()
        self.db.executescript(MIGRATION_042.read_text(encoding="utf-8"))
        after = self.db.execute("SELECT * FROM transfer_source_authorities WHERE id=1").fetchone()
        self.assertEqual(before, after)

    def test_verified_shape_is_supported_but_direct_verified_insert_is_rejected(self):
        self.db.executescript(MIGRATION_042.read_text(encoding="utf-8"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("""INSERT INTO transfer_source_authorities(
                student_id,transfer_contract_id,parent_form_token,source_classification,
                lifecycle_state,rule_version,created_by,created_at,verified_by,verified_at
            ) VALUES('new',100,'new-intake','new_transfer','verified',
                     'transfer-source-v1',1,'2026-09-07',1,'2026-09-07')""")

    def test_pending_to_verified_requires_matching_persisted_classification(self):
        self.db.executescript(MIGRATION_042.read_text(encoding="utf-8"))
        self.db.execute("""INSERT INTO transfer_source_authorities(
            id,student_id,transfer_contract_id,parent_form_token,rule_version,created_by,created_at
        ) VALUES(1,'new',100,'new-intake','transfer-source-v1',1,'2026-09-06')""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("""UPDATE transfer_source_authorities
                SET lifecycle_state='verified',source_classification='aaie_existing',
                    verified_by=1,verified_at='2026-09-07' WHERE id=1""")
        self.db.execute("""UPDATE transfer_source_authorities
            SET lifecycle_state='verified',source_classification='new_transfer',
                verified_by=1,verified_at='2026-09-07' WHERE id=1""")
        self.assertEqual(
            ('verified', 'new_transfer', 1, '2026-09-07'),
            self.db.execute("""SELECT lifecycle_state,source_classification,verified_by,verified_at
                FROM transfer_source_authorities WHERE id=1""").fetchone(),
        )


if __name__ == '__main__':
    unittest.main()
