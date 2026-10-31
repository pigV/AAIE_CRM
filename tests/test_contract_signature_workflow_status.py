"""Signed parents must never be asked to sign again after an archive failure."""
import sqlite3
import unittest

from application_cycle import create_student_cycle
from tests.test_workflow_application_cycles_migration import (
    NOW, apply_workflow_migration, build_legacy_fixture,
)
from student_workflow_projection import load_student_workflow_projection_map


class ContractSignatureWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.conn = build_legacy_fixture(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.row_factory = sqlite3.Row
        apply_workflow_migration(self.conn)
        self.conn.execute('''CREATE TABLE contract_signatures(
            id INTEGER PRIMARY KEY, contract_id INTEGER, student_id TEXT,
            status TEXT, archive_status TEXT)''')
        self.cycle = create_student_cycle(self.conn, 'S1', 'initial', NOW, 1)
        self.conn.execute("UPDATE contracts SET application_cycle_id=?,contract_status='Sent' WHERE id=10", (self.cycle.id,))

    def title(self):
        return load_student_workflow_projection_map(self.conn, ['S1'])['S1'].stage_label

    def sign(self, contract_id=10, student_id='S1', status='signed'):
        self.conn.execute("INSERT INTO contract_signatures(contract_id,student_id,status,archive_status) VALUES(?,?,?,'failed')", (contract_id, student_id, status))

    def test_saved_signature_with_stale_sent_status_waits_for_aaie(self):
        self.sign()
        self.assertEqual(self.title(), '等待 AAIE 完成签章／归档')
        self.assertEqual(self.conn.execute('SELECT contract_status FROM contracts WHERE id=10').fetchone()[0], 'Sent')

    def test_parent_signed_waits_for_aaie_even_without_online_signature(self):
        self.conn.execute("UPDATE contracts SET contract_status='ParentSigned',signed_at=? WHERE id=10", (NOW,))
        self.assertEqual(self.title(), '等待 AAIE 完成签章／归档')

    def test_completed_archive_advances_to_school_plan(self):
        self.sign()
        self.conn.execute("UPDATE contracts SET contract_status='FullyExecuted',fully_executed_at=? WHERE id=10", (NOW,))
        self.conn.execute("UPDATE contract_signatures SET archive_status='archived'")
        self.assertEqual(self.title(), '等待制作学校方案')

    def test_failed_archive_is_not_complete_even_with_fully_executed_status(self):
        self.sign()
        self.conn.execute("UPDATE contracts SET contract_status='FullyExecuted',fully_executed_at=? WHERE id=10", (NOW,))
        self.assertEqual(self.title(), '等待 AAIE 完成签章／归档')

    def test_missing_archive_status_does_not_claim_completion(self):
        self.sign()
        self.conn.execute("UPDATE contracts SET contract_status='FullyExecuted' WHERE id=10")
        self.conn.execute('UPDATE contract_signatures SET archive_status=NULL')
        self.assertEqual(self.title(), '等待 AAIE 完成签章／归档')

    def test_latest_archived_signature_supersedes_old_failed_archive(self):
        self.sign()
        self.sign()
        self.conn.execute("UPDATE contracts SET contract_status='FullyExecuted' WHERE id=10")
        self.conn.execute("UPDATE contract_signatures SET archive_status='archived' WHERE id=2")
        self.assertEqual(self.title(), '等待制作学校方案')

    def test_unsigned_and_unrelated_signature_still_waits_for_parent(self):
        self.sign(student_id='S2')
        self.sign(contract_id=999)
        self.sign(status='invalid')
        self.assertEqual(self.title(), '等待家长签合同')

    def test_complete_contract_in_same_cycle_takes_precedence_over_pending_one(self):
        self.sign()
        self.conn.execute("INSERT INTO contracts(id,student_id,contract_status,application_cycle_id) VALUES(99,'S1','FullyExecuted',?)", (self.cycle.id,))
        self.assertEqual(self.title(), '等待制作学校方案')

    def test_completed_contract_outside_cycle_does_not_advance(self):
        self.sign()
        self.conn.execute("INSERT INTO contracts(id,student_id,contract_status) VALUES(99,'S1','FullyExecuted')")
        self.assertEqual(self.title(), '等待 AAIE 完成签章／归档')

    def test_completed_contract_uses_existing_later_plan_stage(self):
        self.conn.execute("UPDATE contracts SET contract_status='FullyExecuted' WHERE id=10")
        self.conn.execute('UPDATE school_plan_reports SET application_cycle_id=?,cycle_is_current=1 WHERE id=20', (self.cycle.id,))
        self.conn.execute("INSERT INTO school_plan_report_links(id,report_id,status,created_at) VALUES(99,20,'active',?)", (NOW,))
        self.assertEqual(self.title(), '等待家长确认方案')
